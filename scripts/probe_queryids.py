"""Report what is actually inside LinkedIn's JS bundles.

For when queryId discovery finds nothing and the useful question is what *is*
in there. Read-only.

    python scripts/probe_queryids.py <public-identifier>
    python scripts/probe_queryids.py --feed
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.errors import LinkedInAPIError  # noqa: E402
from app.linkedin.auth import SessionManager  # noqa: E402
from app.linkedin.client import VoyagerClient  # noqa: E402
from app.linkedin.queryids import (  # noqa: E402
    FALLBACK_SOURCE_URL,
    WANTED,
    QueryIdRegistry,
    profile_page_url,
)
from app.logging_config import configure_logging  # noqa: E402

# Deliberately wider than the production pattern: anything that looks like a
# registered query at all, so we can see what shape they really take now.
BROAD = re.compile(r"\b([A-Za-z][A-Za-z0-9]{6,60})\.([0-9a-f]{12,80})\b")
RULE = "-" * 74


async def main() -> int:
    settings = get_settings()
    configure_logging("WARNING", settings.log_colour)

    args = [a for a in sys.argv[1:] if a != "--feed"]
    use_feed = "--feed" in sys.argv[1:]

    if use_feed:
        seed = FALLBACK_SOURCE_URL
    elif args:
        seed = profile_page_url(args[0])
    else:
        print("Usage: python scripts/probe_queryids.py <public-identifier> | --feed")
        return 2

    if not settings.has_session:
        print("No session in .env. Set LINKEDIN_COOKIE.")
        return 2

    client = VoyagerClient(settings, SessionManager(settings))
    registry = QueryIdRegistry(settings)

    print(f"\n  seed page   {seed}\n")

    try:
        html = await client.get_asset(seed)
    except LinkedInAPIError as exc:
        print(f"  Could not fetch the seed page: {exc.reason} - {exc.message}")
        await client.aclose()
        return 1

    bundles = registry._bundle_urls(html)  # noqa: SLF001 - a diagnostic, by design
    print(f"  page size   {len(html):,} bytes")
    print(f"  bundles     {len(bundles)} referenced\n{RULE}")

    voyager_hits: Counter[str] = Counter()
    other_hits: Counter[str] = Counter()
    scanned = 0
    failed = 0

    for url in bundles:
        try:
            source = await client.get_asset(url)
        except Exception as exc:  # noqa: BLE001 - one bad bundle is not the story
            failed += 1
            print(f"  [skip] {url.rsplit('/', 1)[-1][:52]:<54} {type(exc).__name__}")
            continue

        scanned += 1
        names = set()
        for name, _hash in BROAD.findall(source):
            if name.startswith("voyager"):
                voyager_hits[name] += 1
                names.add(name)
            else:
                other_hits[name] += 1

        marker = f"{len(names)} voyager*" if names else "-"
        print(f"  [{scanned:>2}]   {url.rsplit('/', 1)[-1][:52]:<54} {marker}")

    await client.aclose()

    print(RULE)
    print(f"  scanned {scanned} bundles, {failed} unreachable\n")

    if voyager_hits:
        print("  voyager* registrations found:")
        for name, count in voyager_hits.most_common(30):
            needed = "  <-- NEEDED" if name in WANTED else ""
            print(f"    {name:<52} x{count}{needed}")
        missing = [n for n in WANTED if n not in voyager_hits]
        if missing:
            print("\n  Still missing:")
            for name in missing:
                print(f"    {name}")
    else:
        print("  No voyager* registrations in any bundle.")
        if other_hits:
            print("\n  Other name.hash pairs seen, as a shape reference:")
            for name, count in other_hits.most_common(15):
                print(f"    {name:<52} x{count}")
            print(
                "\n  If none of these look like query registrations, LinkedIn is\n"
                "  no longer shipping them inline. Capture a real profile request\n"
                "  in DevTools, copy its queryId, and pin it via\n"
                "  LINKEDIN_QUERY_IDS_JSON."
            )
        else:
            print(
                "\n  Nothing matched at all, which usually means the bundles came\n"
                "  back as error pages. Check the cookie."
            )

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
