"""Discover LinkedIn's GraphQL queryId hashes at runtime.

Voyager's GraphQL endpoint does not accept arbitrary queries. It accepts a
`queryId` naming a query LinkedIn has pre-registered server-side, in the form
`voyagerIdentityDashProfileComponents.<32 hex chars>`. The hash changes whenever
LinkedIn ships the corresponding front-end module, which is often.

That makes a hardcoded queryId a time bomb: the service works the day it is
written and returns 400s a few weeks later, with nothing in the logs explaining
why. So we read the ids the same place the LinkedIn web app does -- out of its
own JavaScript bundles -- and refresh them when one stops working.

Scope note, because it is easy to misread: discovery fetches HTML only to list
the `<script src>` bundle URLs, and then fetches those `.js` files. No profile
data is ever parsed out of HTML. Profile data comes exclusively from Voyager
endpoints. This is `httpx.get` on a JavaScript file, not browser automation and
not page scraping.

Resolution order: an env pin beats the cache, the cache beats a network fetch.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

from app.config import Settings
from app.errors import QueryIdDiscoveryFailed
from app.logging_config import stage

if TYPE_CHECKING:
    from app.linkedin.client import VoyagerClient

logger = logging.getLogger(__name__)

# Seeded from a page the web app renders for any logged-in member. The feed
# loads the same shared query registry the profile view uses.
BUNDLE_SOURCE_URL = "https://www.linkedin.com/feed/"

# LinkedIn serves its bundles from static.licdn.com under a content hash.
_BUNDLE_URL_RE = re.compile(r"https://static\.licdn\.com/[^\"'\s>]+?\.js")

# The registry entries themselves, as they appear inline in the bundle source.
_QUERY_ID_RE = re.compile(r"\b(voyager[A-Za-z0-9]+)\.([0-9a-f]{32})\b")

# Queries this service needs. Names are stable even though the hashes are not.
QUERY_PROFILE_BY_VANITY = "voyagerIdentityDashProfiles"
QUERY_PROFILE_COMPONENTS = "voyagerIdentityDashProfileComponents"
QUERY_PROFILE_CARDS = "voyagerIdentityDashProfileCards"

WANTED = (QUERY_PROFILE_BY_VANITY, QUERY_PROFILE_COMPONENTS, QUERY_PROFILE_CARDS)

CACHE_TTL_SECONDS = 24 * 3600

# Minimum gap between two rediscoveries. LinkedIn rotating its queryIds is a rare
# event -- twice inside half a minute is not that, it is us thrashing. Section
# fetches run in waves, so without this bound each wave observes a fresh
# generation and rotates again, and one rotation becomes one per wave.
MIN_SECONDS_BETWEEN_ROTATIONS = 30.0
MAX_BUNDLES_SCANNED = 40
BUNDLE_CONCURRENCY = 4


class QueryIdRegistry:
    """Holds the current queryId map and knows how to rediscover it."""

    def __init__(self, settings: Settings, client: VoyagerClient) -> None:
        self._settings = settings
        self._client = client
        self._discovered: dict[str, str] = {}
        self._discovered_at: float = 0.0
        self._generation = 0
        self._last_rotation_at = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def pinned(self) -> dict[str, str]:
        return self._settings.pinned_query_ids

    @property
    def generation(self) -> int:
        """Bumped on every invalidation.

        A caller captures this before a request and hands it back if that request
        is rejected. That is what lets twelve concurrent section fetches, all
        rejected by the same stale id, trigger exactly one rediscovery between
        them instead of twelve.
        """
        return self._generation

    def is_pinned(self, name: str) -> bool:
        return bool(self.pinned.get(name))

    def _cache_is_fresh(self) -> bool:
        return bool(self._discovered) and (
            time.time() - self._discovered_at < CACHE_TTL_SECONDS
        )

    def invalidate(self, seen_generation: int | None = None) -> bool:
        """Force the next lookup to rediscover. Returns whether this call did it.

        Two guards, because they catch different shapes of the same problem.
        The generation check ignores a stale report from a request that failed
        before someone else already rotated. The cooldown bounds the rest: section
        fetches run in waves, so each wave sees a legitimately fresh generation
        and would otherwise rotate again.
        """
        if seen_generation is not None and seen_generation != self._generation:
            return False

        elapsed = time.monotonic() - self._last_rotation_at
        if elapsed < MIN_SECONDS_BETWEEN_ROTATIONS:
            # Already rotated moments ago. The caller will pick up the ids that
            # rotation installed rather than triggering another pass.
            return False

        if self._discovered:
            stage(logger, "queryid", "cache invalidated - will rediscover",
                  level=logging.WARNING)
        self._discovered = {}
        self._discovered_at = 0.0
        self._last_rotation_at = time.monotonic()
        self._generation += 1
        return True

    async def get(self, name: str) -> str:
        """Return the queryId for a query name, discovering it if necessary."""
        pinned = self.pinned.get(name)
        if pinned:
            return pinned

        if self._cache_is_fresh() and name in self._discovered:
            return self._discovered[name]

        await self._discover()

        value = self._discovered.get(name)
        if not value:
            raise QueryIdDiscoveryFailed(
                f"Could not discover a queryId for {name}. LinkedIn's bundles may "
                f"have changed shape. Pin a known-good value via "
                f"LINKEDIN_QUERY_IDS_JSON to bypass discovery."
            )
        return value

    def snapshot(self) -> dict[str, object]:
        """Operational view for the /v1/session diagnostic endpoint."""
        return {
            "pinned": sorted(self.pinned.keys()),
            "discovered": sorted(self._discovered.keys()),
            "discovered_age_seconds": (
                round(time.time() - self._discovered_at, 1) if self._discovered_at else None
            ),
        }

    # --- discovery ----------------------------------------------------------

    async def _discover(self) -> None:
        async with self._lock:
            # Another coroutine may have refreshed while we waited on the lock.
            if self._cache_is_fresh():
                return

            html = await self._client.get_asset(BUNDLE_SOURCE_URL)
            bundle_urls = self._bundle_urls(html)
            if not bundle_urls:
                raise QueryIdDiscoveryFailed(
                    "No JavaScript bundle URLs found in the LinkedIn page. The "
                    "session is most likely logged out or challenged."
                )

            stage(logger, "queryid", "scanning LinkedIn JS bundles",
                  bundles=len(bundle_urls))
            found = await self._scan_bundles(bundle_urls)

            if not found:
                raise QueryIdDiscoveryFailed(
                    f"Scanned {len(bundle_urls)} bundles and found no queryId "
                    f"registrations."
                )

            self._discovered = found
            self._discovered_at = time.time()
            stage(logger, "queryid", "discovered", count=len(found),
                  wanted=sum(1 for n in WANTED if n in found))

    @staticmethod
    def _bundle_urls(html: str) -> list[str]:
        """Bundle URLs from the page, most-likely-relevant first.

        Bundles whose filenames mention profile or identity are scanned before the
        rest, so the common case finds what it needs in the first few fetches
        instead of pulling forty files.
        """
        seen: dict[str, None] = {}
        for match in _BUNDLE_URL_RE.finditer(html):
            seen.setdefault(match.group(0), None)

        urls = list(seen)
        urls.sort(key=lambda u: 0 if ("profile" in u or "identity" in u) else 1)
        return urls[:MAX_BUNDLES_SCANNED]

    async def _scan_bundles(self, urls: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        semaphore = asyncio.Semaphore(BUNDLE_CONCURRENCY)

        async def scan(url: str) -> None:
            if all(name in found for name in WANTED):
                return  # Everything we need is already in hand.
            async with semaphore:
                try:
                    source = await self._client.get_asset(url)
                except Exception as exc:  # noqa: BLE001 - one bad bundle is not fatal
                    logger.debug("bundle fetch failed for %s: %s", url, exc)
                    return
            for name, query_id in _QUERY_ID_RE.findall(source):
                # First writer wins: bundles are scanned relevance-ordered, so an
                # id from a profile bundle should not be overwritten by a
                # coincidental match in an unrelated one.
                found.setdefault(name, query_id)

        # Scanned in relevance-ordered chunks so the early-exit check above can
        # actually short-circuit rather than every task launching at once.
        for start in range(0, len(urls), BUNDLE_CONCURRENCY):
            chunk = urls[start : start + BUNDLE_CONCURRENCY]
            await asyncio.gather(*(scan(url) for url in chunk))
            if all(name in found for name in WANTED):
                break

        return {name: qid for name, qid in found.items() if name.startswith("voyager")}
