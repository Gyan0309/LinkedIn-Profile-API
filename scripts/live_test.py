"""One live end-to-end fetch against the real Voyager API.

Deliberately does ONE profile per run. The point of this script is to tell you
whether the session works and what the data looks like, and that answer does not
get better by making twenty requests to find it.

    python scripts/live_test.py                       # a demo profile
    python scripts/live_test.py <linkedin-profile-url>

Reads .env directly -- no server needed. Every failure is explained in terms of
what to do about it, because the interesting failures here (challenge, 999,
expired cookie) all need a human and none of them are code bugs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.errors import (  # noqa: E402
    ChallengeRequired,
    LinkedInAPIError,
    LinkedInBlocked,
    SessionUnavailable,
)
from app.linkedin.auth import SessionManager  # noqa: E402
from app.linkedin.client import VoyagerClient  # noqa: E402
from app.linkedin.fetch import ProfileFetcher  # noqa: E402
from app.linkedin.queryids import QueryIdRegistry  # noqa: E402
from app.linkedin.urls import extract_public_identifier  # noqa: E402

RULE = "-" * 72

REMEDIES = {
    "linkedin_challenge_required": (
        "LinkedIn challenged the login. This is the expected outcome of the\n"
        "  email/password path more often than not, and the code is behaving\n"
        "  correctly by stopping. Use the cookie path instead: log in from a\n"
        "  browser, complete the challenge there, then copy li_at into .env."
    ),
    "linkedin_credentials_rejected": (
        "LinkedIn rejected that email/password pair. Check them, but do NOT keep\n"
        "  retrying -- repeated failed logins are themselves a risk signal, and\n"
        "  LinkedIn declines programmatic login for many accounts regardless of\n"
        "  whether the password is right. The cookie path is the reliable one."
    ),
    "linkedin_session_unavailable": (
        "No usable session. Either LINKEDIN_LI_AT is empty in .env, or the\n"
        "  cookie has expired. Copy a fresh one from the browser."
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

    target = sys.argv[1] if len(sys.argv) > 1 else settings.demo_profiles[0]
    slug = extract_public_identifier(target)

    if settings.has_cookie_session:
        path = "cookie (LINKEDIN_LI_AT)"
    elif settings.has_login_credentials:
        path = "programmatic login (LINKEDIN_EMAIL / LINKEDIN_PASSWORD)"
    else:
        print("No LinkedIn credentials in .env. Fill in LINKEDIN_LI_AT and re-run.")
        return 2

    print(f"\n  auth path           {path}")
    print(f"  target              {slug}")
    print(f"  outbound cap        {settings.outbound_max_per_minute}/min")
    print(f"  proxy               {'yes' if settings.outbound_proxy_url else 'no'}")
    print()

    sessions = SessionManager(settings)
    client = VoyagerClient(settings, sessions)
    registry = QueryIdRegistry(settings, client)
    fetcher = ProfileFetcher(client, registry)

    started = time.perf_counter()
    try:
        result = await fetcher.fetch(slug)
    except (ChallengeRequired, SessionUnavailable, LinkedInBlocked, LinkedInAPIError) as exc:
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
