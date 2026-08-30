"""Discover GraphQL queryId hashes at runtime.

Voyager only accepts pre-registered queries, named by a hash that changes when
LinkedIn ships. Rather than hardcode one, the ids are read out of LinkedIn's own
JS bundles. Only bundle URLs come from HTML; profile data never does.

Largely vestigial: linkedin.com no longer calls the section query this served,
so the dash collections in fetch.py do the real work.
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

# Seeded from a profile page, not the feed: LinkedIn code-splits per route, so
# feed bundles hold no profile queries.
FALLBACK_SOURCE_URL = "https://www.linkedin.com/feed/"


def profile_page_url(public_identifier: str) -> str:
    return f"https://www.linkedin.com/in/{public_identifier}/"


# LinkedIn serves its bundles from static.licdn.com under a content hash.
_BUNDLE_URL_RE = re.compile(r"https://static\.licdn\.com/[^\"'\s>]+?\.js")

# The hash length is not contractual, so it is matched as a range.
_QUERY_ID_RE = re.compile(r"\b(voyager[A-Za-z0-9]+)\.([0-9a-f]{16,64})\b")

# The same registration written as an explicit property, which some bundles use.
_QUERY_ID_ASSIGN_RE = re.compile(
    r"""queryId\s*[:=]\s*['"](voyager[A-Za-z0-9]+)\.([0-9a-f]{16,64})['"]"""
)

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
    """Holds the current queryId map and knows how to rediscover it.

    Owns no client: queryIds describe LinkedIn, not the caller, so one registry
    is shared and borrows a client for a discovery pass.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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
        """Bumped on every invalidation, so concurrent rejections rotate once."""
        return self._generation

    def is_pinned(self, name: str) -> bool:
        return bool(self.pinned.get(name))

    def _cache_is_fresh(self) -> bool:
        return bool(self._discovered) and (
            time.time() - self._discovered_at < CACHE_TTL_SECONDS
        )

    def invalidate(self, seen_generation: int | None = None) -> bool:
        """Force rediscovery. Returns whether this call actually did it.

        The generation check ignores a stale report; the cooldown stops section
        fetch waves each rotating again.
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

    async def get(
        self,
        name: str,
        client: VoyagerClient,
        seed_url: str | None = None,
    ) -> str:
        """Return the queryId for a query name, discovering it if necessary."""
        pinned = self.pinned.get(name)
        if pinned:
            return pinned

        if self._cache_is_fresh() and name in self._discovered:
            return self._discovered[name]

        await self._discover(client, seed_url)

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

    async def _discover(
        self, client: VoyagerClient, seed_url: str | None = None
    ) -> None:
        async with self._lock:
            # Another coroutine may have refreshed while we waited on the lock.
            if self._cache_is_fresh():
                return

            source_url = seed_url or FALLBACK_SOURCE_URL
            html = await client.get_asset(source_url)
            bundle_urls = self._bundle_urls(html)
            if not bundle_urls:
                raise QueryIdDiscoveryFailed(
                    "No JavaScript bundle URLs found in the LinkedIn page. The "
                    "session is most likely logged out or challenged."
                )

            stage(logger, "queryid", "scanning LinkedIn JS bundles",
                  bundles=len(bundle_urls), seed=source_url)
            found = await self._scan_bundles(client, bundle_urls)

            if not found:
                raise QueryIdDiscoveryFailed(
                    f"Scanned {len(bundle_urls)} bundles from {source_url} and "
                    f"found no queryId registrations. Run "
                    f"`python scripts/probe_queryids.py <profile>` to see what is "
                    f"actually in the bundles, or pin known-good ids via "
                    f"LINKEDIN_QUERY_IDS_JSON."
                )

            self._discovered = found
            self._discovered_at = time.time()
            stage(logger, "queryid", "discovered", count=len(found),
                  wanted=sum(1 for n in WANTED if n in found))

    @staticmethod
    def _bundle_urls(html: str) -> list[str]:
        """Bundle URLs, profile-related ones first so the scan can exit early."""
        seen: dict[str, None] = {}
        for match in _BUNDLE_URL_RE.finditer(html):
            seen.setdefault(match.group(0), None)

        urls = list(seen)
        urls.sort(key=lambda u: 0 if ("profile" in u or "identity" in u) else 1)
        return urls[:MAX_BUNDLES_SCANNED]

    async def _scan_bundles(
        self, client: VoyagerClient, urls: list[str]
    ) -> dict[str, str]:
        found: dict[str, str] = {}
        semaphore = asyncio.Semaphore(BUNDLE_CONCURRENCY)

        async def scan(url: str) -> None:
            if all(name in found for name in WANTED):
                return  # Everything we need is already in hand.
            async with semaphore:
                try:
                    source = await client.get_asset(url)
                except Exception as exc:  # noqa: BLE001 - one bad bundle is not fatal
                    logger.debug("bundle fetch failed for %s: %s", url, exc)
                    return
            matches = _QUERY_ID_RE.findall(source) + _QUERY_ID_ASSIGN_RE.findall(source)
            for name, query_id in matches:
                # First writer wins; bundles are scanned relevance-ordered.
                found.setdefault(name, query_id)

        # Chunked so the early exit above can actually short-circuit.
        for start in range(0, len(urls), BUNDLE_CONCURRENCY):
            chunk = urls[start : start + BUNDLE_CONCURRENCY]
            await asyncio.gather(*(scan(url) for url in chunk))
            if all(name in found for name in WANTED):
                break

        return {name: qid for name, qid in found.items() if name.startswith("voyager")}
