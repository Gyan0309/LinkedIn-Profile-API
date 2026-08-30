"""Turn whatever a caller pastes into a LinkedIn public identifier (the "vanity name").

Callers paste URLs copied from a browser address bar, a share sheet, or an email,
so the input is messier than the happy path suggests: tracking query strings,
locale subdomains, trailing section paths, percent-encoded non-ASCII slugs, and
the legacy /pub/ form that LinkedIn still redirects from.

Everything downstream works from the extracted identifier alone, so this module is
the only place that has to care about URL shape.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.errors import InvalidProfileURL

# LinkedIn allows unicode in vanity names (e.g. /in/andré-müller-1a2b3c). It does
# not allow spaces, slashes, or the reserved characters a path separator implies.
_SLUG_RE = re.compile(r"^[^\s/?#%]+$", re.UNICODE)

# Paths under linkedin.com that are emphatically not member profiles. Called out
# by name so the error tells the caller what they actually pasted.
_NON_PROFILE_PATHS = {
    "company": "a company page",
    "school": "a school page",
    "jobs": "a job listing",
    "posts": "a post",
    "feed": "the feed",
    "groups": "a group",
    "showcase": "a showcase page",
    "learning": "a LinkedIn Learning page",
    "newsletters": "a newsletter",
    "events": "an event",
    "pulse": "an article",
}

_LINKEDIN_HOST_RE = re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com$", re.IGNORECASE)


def extract_public_identifier(raw: str) -> str:
    """Extract the public identifier from a LinkedIn profile URL.

    Also accepts a bare identifier, since that is what our own `meta` block emits
    and round-tripping our own output should not require reconstructing a URL.

    Raises InvalidProfileURL with a message that names the actual problem.
    """
    if not raw or not raw.strip():
        raise InvalidProfileURL("No profile URL supplied.")

    candidate = raw.strip()

    # A bare identifier: no scheme, no host, no path separators.
    if "/" not in candidate and "." not in candidate:
        return _validate_slug(unquote(candidate))

    # urlparse needs a scheme to populate netloc rather than folding it into path.
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    parsed = urlparse(candidate)
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()

    if not _LINKEDIN_HOST_RE.match(host):
        raise InvalidProfileURL(
            f"Not a linkedin.com URL: host was {host or 'missing'!r}."
        )

    segments = [seg for seg in parsed.path.split("/") if seg]
    if not segments:
        raise InvalidProfileURL("URL has no path — expected /in/<identifier>.")

    head = segments[0].lower()

    if head in _NON_PROFILE_PATHS:
        raise InvalidProfileURL(
            f"That URL points at {_NON_PROFILE_PATHS[head]}, not a member profile. "
            "This API reads /in/<identifier> profile URLs only."
        )

    if head == "in":
        if len(segments) < 2:
            raise InvalidProfileURL("URL is /in/ with no identifier after it.")
        # Anything after the identifier is a profile subsection
        # (/detail/experience, /recent-activity) and is discarded.
        return _validate_slug(unquote(segments[1]))

    if head == "pub":
        # Legacy form: /pub/first-last/1/b2/3c4 — the identifier is the first segment
        # and the trailing groups are the old member-id encoding, which we drop.
        if len(segments) < 2:
            raise InvalidProfileURL("Legacy /pub/ URL has no identifier after it.")
        return _validate_slug(unquote(segments[1]))

    raise InvalidProfileURL(
        f"Unrecognised LinkedIn path /{head}/ — expected /in/<identifier>."
    )


def _validate_slug(slug: str) -> str:
    slug = slug.strip().rstrip("/")
    if not slug:
        raise InvalidProfileURL("Profile identifier is empty.")
    if not _SLUG_RE.match(slug):
        raise InvalidProfileURL(f"Profile identifier {slug!r} contains invalid characters.")
    return slug


def canonical_profile_url(public_identifier: str) -> str:
    """The canonical URL for an identifier, echoed back in the response meta block."""
    return f"https://www.linkedin.com/in/{public_identifier}"
