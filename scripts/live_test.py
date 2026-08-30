"""One live end-to-end fetch against the real Voyager API.

    python scripts/live_test.py <linkedin-profile-url>

One profile per run, no server needed. Failures print what to do about them,
since the interesting ones all need a human rather than a code change.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.errors import LinkedInAPIError  # noqa: E402
from app.linkedin.auth import SessionManager  # noqa: E402
from app.linkedin.client import VoyagerClient  # noqa: E402
from app.linkedin.fetch import ProfileFetcher  # noqa: E402
from app.linkedin.queryids import QueryIdRegistry  # noqa: E402
from app.linkedin.urls import extract_public_identifier  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402

RULE = "-" * 72

REMEDIES = {
    "linkedin_session_rejected": (
        "LinkedIn bounced the request to its login page, so it did not accept\n"
        "  the session. Copy the whole Cookie header again from a live request\n"
        "  (DevTools > Network > any www.linkedin.com request > Request Headers\n"
        "  > cookie) -- a partial one is the usual cause."
    ),
    "linkedin_session_unavailable": (
        "No usable session. LINKEDIN_COOKIE is empty in .env, or the cookie has\n"
        "  expired. Copy a fresh header from the browser."
    ),
    "endpoint_retired": (
        "LinkedIn has withdrawn that endpoint for this account. Expected, and\n"
        "  handled -- the chain falls through to the next strategy."
    ),
    "linkedin_blocked": (
        "LinkedIn refused this host (999 or 403). Your IP is flagged, or the\n"
        "  account is restricted. Wait, try from a different connection, or set\n"
        "  OUTBOUND_PROXY_URL to a RESIDENTIAL proxy. Do not retry in a loop."
    ),
    "query_id_discovery_failed": (
        "Could not read queryIds out of LinkedIn's JS bundles. Usually means the\n"
        "  session is logged out. Check the cookie first."
    ),
}


def summarise(profile, meta_source: str, unavailable: list[str], elapsed: float) -> None:
    print(RULE)
    print(f"  source              {meta_source}")
    print(f"  elapsed             {elapsed:.1f}s")
    print(RULE)
    print(f"  name                {profile.name.full or '-'}")
    print(f"  headline            {(profile.headline or '-')[:60]}")
    print(f"  location            {profile.location.raw or '-'}")
    print(f"  industry            {profile.industry or '-'}")
    print(f"  pronouns            {profile.pronouns or '-'}")
    print(f"  urn                 {profile.profile_urn or '-'}")
    connections = profile.connections.count
    suffix = "+" if profile.connections.is_capped else ""
    print(f"  connections         {connections if connections is not None else '-'}{suffix}")
    print(f"  followers           {profile.followers if profile.followers is not None else '-'}")
    print(f"  open_to_work        {profile.open_to_work}")
    print(f"  premium             {profile.premium}")

    picture = profile.profile_picture
    print(f"  profile picture     {len(picture.sizes) if picture else 0} size(s)")
    if picture and picture.largest:
        print(f"                      {picture.largest[:64]}")

    print(RULE)
    sections = [
        ("experience", profile.experience),
        ("education", profile.education),
        ("skills", profile.skills),
        ("certifications", profile.certifications),
        ("languages", profile.languages),
        ("projects", profile.projects),
        ("publications", profile.publications),
        ("honors", profile.honors),
        ("volunteering", profile.volunteering),
        ("courses", profile.courses),
        ("patents", profile.patents),
        ("organizations", profile.organizations),
    ]
    for name, values in sections:
        if name in unavailable:
            state = "NOT FETCHED  <- reported in sections_unavailable"
        elif values:
            state = f"{len(values)} item(s)"
        else:
            state = "0 items (fetched fine; genuinely empty)"
        print(f"  {name:<19} {state}")

    print(RULE)
    if profile.experience:
        print("  First position:")
        top = profile.experience[0]
        print(f"    title             {top.title or '-'}")
        print(f"    company           {top.company or '-'}")
        ended = _fmt(top.end) if top.end else "present"
        print(f"    dates             {_fmt(top.start)} -> {ended}")
        print(f"    is_current        {top.is_current}")
        if top.sub_positions:
            print(f"    sub_positions     {len(top.sub_positions)} (grouped multi-role stint)")
            for role in top.sub_positions:
                print(f"      - {role.title} ({_fmt(role.start)})")
        print(RULE)


def _fmt(date) -> str:
    if date is None:
        return "?"
    if date.month and date.year:
        return f"{date.year}-{date.month:02d}"
    return str(date.year or "?")


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_colour)

    target = sys.argv[1] if len(sys.argv) > 1 else settings.demo_profiles[0]
    slug = extract_public_identifier(target)

    if not settings.has_session:
        print(
            "No LinkedIn session in .env. Set LINKEDIN_COOKIE to the whole Cookie "
            "header from a logged-in browser request, then re-run."
        )
        return 2

    print("\n  auth path           LINKEDIN_COOKIE")
    print(f"  target              {slug}")
    print(f"  outbound cap        {settings.outbound_max_per_minute}/min")
    print(f"  proxy               {'yes' if settings.outbound_proxy_url else 'no'}")
    print()

    sessions = SessionManager(settings)
    client = VoyagerClient(settings, sessions)
    registry = QueryIdRegistry(settings)
    fetcher = ProfileFetcher(client, registry)

    started = time.perf_counter()
    try:
        result = await fetcher.fetch(slug)
    except LinkedInAPIError as exc:
        elapsed = time.perf_counter() - started
        print(RULE)
        print(f"  FAILED after {elapsed:.1f}s")
        print(f"  error               {exc.reason}")
        print(f"  message             {exc.message}")
        remedy = REMEDIES.get(exc.reason)
        if remedy:
            print(f"\n  What to do:\n  {remedy}")
        print(RULE)
        return 1
    finally:
        await client.aclose()

    elapsed = time.perf_counter() - started
    summarise(result.profile, result.source_label, result.sections_unavailable, elapsed)

    session = sessions.current
    if session:
        print(f"  session used        {session.redacted()['source']}")
    print(f"  queryIds            {registry.snapshot()}")

    out = Path("live_result.json")
    out.write_text(
        json.dumps(
            {
                "source": result.source_label,
                "sections_unavailable": result.sections_unavailable,
                "profile": result.profile.model_dump(mode="json"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  Full JSON written to {out.resolve()}")
    print("  (gitignored -- it contains a real person's profile data)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
