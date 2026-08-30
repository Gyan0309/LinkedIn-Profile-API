"""Turn captured LinkedIn requests into a LINKEDIN_QUERY_IDS_JSON value.

Makes no network requests. Pinning the ids means the service never fetches a
LinkedIn HTML page, which is the request that draws HTTP 999.

To capture: DevTools > Network, filter `graphql`, click any request to
/voyager/api/graphql, copy the URL. Scrolling the profile fires more.

    python scripts/pin_queryids.py            # paste, then Ctrl-Z (Windows)
    python scripts/pin_queryids.py < urls.txt
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.linkedin.queryids import WANTED  # noqa: E402

# `queryId=voyagerIdentityDashProfileComponents.abc123...`, in a URL or a cURL
# blob, percent-encoded or not.
QUERY_ID_RE = re.compile(
    r"queryId(?:=|%3D|['\"]?\s*[:=]\s*['\"])"
    r"(voyager[A-Za-z0-9]+)\.([0-9a-fA-F]{8,80})"
)

RULE = "-" * 74


def main() -> int:
    print(__doc__.split("    python")[0].rstrip())
    print(RULE)
    print("  Paste captured request URLs, then press Ctrl-Z + Enter (Windows)")
    print("  or Ctrl-D (macOS/Linux).")
    print(RULE)

    blob = sys.stdin.read()
    if not blob.strip():
        print("\n  Nothing pasted.\n")
        return 2

    found: dict[str, str] = {}
    for name, query_id in QUERY_ID_RE.findall(blob):
        # First occurrence wins; a later duplicate is the same id again.
        found.setdefault(name, query_id.lower())

    print()
    if not found:
        print("  No queryIds recognised in that input.\n")
        print("  Expected something containing:")
        print("    queryId=voyagerIdentityDashProfileComponents.<hex>")
        print()
        print("  Make sure you copied a request to /voyager/api/graphql, not the")
        print("  profile page itself. Filter the Network tab by 'graphql'.\n")
        return 1

    for name, query_id in sorted(found.items()):
        needed = "  <-- needed" if name in WANTED else ""
        print(f"  {name:<48} {query_id[:12]}...{needed}")

    missing = [name for name in WANTED if name not in found]
    if missing:
        print("\n  Not captured yet:")
        for name in missing:
            print(f"    {name}")
        print(
            "\n  Scroll the profile page with the Network tab open -- experience,"
            "\n  education and skills each fire their own query as they render."
        )

    print(f"\n{RULE}")
    print("  Paste this into LINKEDIN_QUERY_IDS_JSON (one line):\n")
    print(f"LINKEDIN_QUERY_IDS_JSON={json.dumps(found, separators=(',', ':'))}")
    print(f"\n{RULE}")
    print("  On Fly:  fly secrets set LINKEDIN_QUERY_IDS_JSON='<the JSON above>'")
    print(
        "\n  With these pinned the service never fetches a LinkedIn HTML page,"
        "\n  which is what draws HTTP 999. They rotate when LinkedIn ships, so"
        "\n  re-run this if you start seeing query_rejected.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
