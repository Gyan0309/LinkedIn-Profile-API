"""Capture a live Voyager payload as a test fixture, redacting as it goes.

The offline test suite is only as good as the shapes it runs against, and those
shapes change when LinkedIn ships. This is how you refresh them.

    python scripts/capture_fixture.py profileview ada-sundqvist
    python scripts/capture_fixture.py section experience ada-sundqvist
    python scripts/capture_fixture.py profile ada-sundqvist

Redaction runs before anything is written, and the session is never written at
all. It replaces member identifiers, real names, image hosts and URNs with
synthetic stand-ins, keeping the *structure* -- which is the only part a parser
test cares about.

Redaction is mechanical, so it is a first pass and not a guarantee. **Read the
output before committing it.** A profile containing something the pattern list
does not anticipate will pass straight through.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.linkedin.auth import SessionManager  # noqa: E402
from app.linkedin.client import VoyagerClient  # noqa: E402
from app.linkedin.fetch import SECTION_TYPES, _restli_variables  # noqa: E402
from app.linkedin.queryids import (  # noqa: E402
    QUERY_PROFILE_BY_VANITY,
    QUERY_PROFILE_COMPONENTS,
    QueryIdRegistry,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Keys whose values are free text that may name a real person or employer.
SENSITIVE_TEXT_KEYS = {
    "firstName",
    "lastName",
    "publicIdentifier",
    "headline",
    "summary",
    "occupation",
    "emailAddress",
    "phoneNumber",
    "address",
    "birthDate",
    "memberBadges",
}

_URN_ID_RE = re.compile(r"(urn:li:[a-zA-Z_]+:)([A-Za-z0-9_\-()]+)")
_MEDIA_HOST_RE = re.compile(r"https://media[-.a-z0-9]*\.licdn\.com/")
_STATIC_HOST_RE = re.compile(r"https://static[-.a-z0-9]*\.licdn\.com/")


def redact(node: Any, counter: dict[str, int]) -> Any:
    """Walk the payload replacing identifying values, preserving every shape."""
    if isinstance(node, dict):
        return {key: _redact_value(key, value, counter) for key, value in node.items()}
    if isinstance(node, list):
        return [redact(item, counter) for item in node]
    if isinstance(node, str):
        return _redact_string(node)
    return node


def _redact_value(key: str, value: Any, counter: dict[str, int]) -> Any:
    if key in SENSITIVE_TEXT_KEYS and isinstance(value, str):
        counter[key] = counter.get(key, 0) + 1
        return f"SYNTHETIC_{key.upper()}_{counter[key]}"
    return redact(value, counter)


def _redact_string(value: str) -> str:
    value = _URN_ID_RE.sub(lambda m: f"{m.group(1)}SYNTHETIC", value)
    value = _MEDIA_HOST_RE.sub("https://media.example-cdn.test/", value)
    value = _STATIC_HOST_RE.sub("https://static.example-cdn.test/", value)
    return value


async def capture(kind: str, slug: str, section: str | None) -> dict[str, Any]:
    settings = get_settings()
    sessions = SessionManager(settings)
    client = VoyagerClient(settings, sessions)
    registry = QueryIdRegistry(settings, client)

    try:
        if kind == "profileview":
            return await client.get_voyager(
                f"identity/profiles/{slug}/profileView"
            )

        if kind == "profile":
            return await client.get_voyager(
                "graphql",
                {
                    "includeWebMetadata": "true",
                    "variables": _restli_variables({"vanityName": slug}),
                    "queryId": await registry.get(QUERY_PROFILE_BY_VANITY),
                },
            )

        if kind == "section":
            if section not in SECTION_TYPES:
                raise SystemExit(
                    f"Unknown section {section!r}. "
                    f"Choose from: {', '.join(sorted(SECTION_TYPES))}"
                )
            profile = await client.get_voyager(
                "graphql",
                {
                    "includeWebMetadata": "true",
                    "variables": _restli_variables({"vanityName": slug}),
                    "queryId": await registry.get(QUERY_PROFILE_BY_VANITY),
                },
            )
            urn = _find_profile_urn(profile)
            if not urn:
                raise SystemExit("Could not resolve a profile URN for that identifier.")
            return await client.get_voyager(
                "graphql",
                {
                    "includeWebMetadata": "true",
                    "variables": _restli_variables(
                        {
                            "profileUrn": urn,
                            "sectionType": SECTION_TYPES[section],
                            "locale": "en_US",
                        }
                    ),
                    "queryId": await registry.get(QUERY_PROFILE_COMPONENTS),
                },
            )

        raise SystemExit(f"Unknown capture kind {kind!r}.")
    finally:
        await client.aclose()


def _find_profile_urn(payload: dict[str, Any]) -> str | None:
    for entity in payload.get("included") or []:
        if isinstance(entity, dict) and entity.get("firstName"):
            urn = entity.get("entityUrn")
            if isinstance(urn, str):
                return urn
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "kind", choices=["profileview", "profile", "section"], help="What to capture"
    )
    parser.add_argument("args", nargs="+", help="[section] <public-identifier>")
    parser.add_argument("--out", help="Output filename (written to tests/fixtures/)")
    parsed = parser.parse_args()

    if parsed.kind == "section":
        if len(parsed.args) != 2:
            parser.error("section capture takes: <section> <public-identifier>")
        section, slug = parsed.args
    else:
        if len(parsed.args) != 1:
            parser.error(f"{parsed.kind} capture takes: <public-identifier>")
        section, slug = None, parsed.args[0]

    payload = asyncio.run(capture(parsed.kind, slug, section))
    counter: dict[str, int] = {}
    redacted = redact(payload, counter)

    name = parsed.out or (
        f"{parsed.kind}_{section}_captured.json"
        if section
        else f"{parsed.kind}_captured.json"
    )
    destination = FIXTURE_DIR / name
    destination.write_text(json.dumps(redacted, indent=2), encoding="utf-8")

    print(f"Wrote {destination}")
    print(f"Redacted {sum(counter.values())} identifying values across {len(counter)} keys.")
    print(
        "Redaction is a mechanical first pass. Read the file before committing it -- "
        "anything the pattern list does not anticipate passes straight through."
    )


if __name__ == "__main__":
    main()
