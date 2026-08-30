"""Shared helpers: image reconstruction, and date parsing.

The REST shapes carry numeric dates; the GraphQL cards carry rendered strings
like "Jan 2020 - Present - 3 yrs". Parsing those back is lossy, so the parser
returns None rather than guessing -- a wrong date is worse than a missing one.
"""

from __future__ import annotations

import re
from typing import Any

from app.schema import DatePart, Image, ImageSize

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Profile content can be multi-locale even with x-li-lang set.
PRESENT_TOKENS = {"present", "current", "now", "heute", "actuel", "actualidad"}

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_MONTH_YEAR_RE = re.compile(
    r"\b([A-Za-z]{3,9})\.?\s+(19\d{2}|20\d{2})\b", re.IGNORECASE
)
_DURATION_RE = re.compile(
    r"(?:(\d+)\s*(?:yrs?|years?))?\s*(?:(\d+)\s*(?:mos?|months?))?", re.IGNORECASE
)


def clean_text(value: Any) -> str | None:
    """Normalise a text value to a stripped string, or None if it is not one."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def text_of(node: Any) -> str | None:
    """The display string from a Voyager text node -- three shapes, same payload."""
    if isinstance(node, str):
        return clean_text(node)
    if not isinstance(node, dict):
        return None

    inner = node.get("text")
    if isinstance(inner, str):
        return clean_text(inner)
    if isinstance(inner, dict):
        return text_of(inner)

    for key in ("accessibilityText", "value", "displayText"):
        found = clean_text(node.get(key))
        if found:
            return found
    return None


def image_from_vector(node: Any) -> Image | None:
    """Rebuild image URLs from a vectorImage: `rootUrl` plus width-tagged paths.

    Every size is returned; the caller knows which one it wants.
    """
    vector = _find_vector_image(node)
    if not vector:
        return None

    root = clean_text(vector.get("rootUrl"))
    artifacts = vector.get("artifacts")
    if not root or not isinstance(artifacts, list):
        return None

    sizes: list[ImageSize] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        segment = clean_text(artifact.get("fileIdentifyingUrlPathSegment"))
        if not segment:
            continue
        sizes.append(
            ImageSize(
                width=_as_int(artifact.get("width")),
                height=_as_int(artifact.get("height")),
                url=root + segment,
            )
        )

    if not sizes:
        return None
    sizes.sort(key=lambda s: s.width or 0)
    return Image(sizes=sizes)


def _find_vector_image(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """Find a vectorImage at any depth; the nesting differs per component."""
    if depth > 8 or node is None:
        return None

    if isinstance(node, dict):
        if "rootUrl" in node and "artifacts" in node:
            return node
        vector = node.get("vectorImage")
        if isinstance(vector, dict):
            return _find_vector_image(vector, depth + 1)
        for value in node.values():
            found = _find_vector_image(value, depth + 1)
            if found:
                return found
        return None

    if isinstance(node, list):
        for item in node:
            found = _find_vector_image(item, depth + 1)
            if found:
                return found
    return None


def date_from_voyager(node: Any) -> DatePart | None:
    """Convert a legacy REST `{year, month, day}` date object."""
    if not isinstance(node, dict):
        return None
    part = DatePart(
        year=_as_int(node.get("year")),
        month=_as_int(node.get("month")),
        day=_as_int(node.get("day")),
    )
    return None if part.is_empty() else part


def parse_date_range(caption: str | None) -> tuple[DatePart | None, DatePart | None, bool]:
    """Parse a rendered caption into (start, end, is_current).

    Returns None for a side it cannot read rather than inventing a value.
    """
    if not caption:
        return None, None, False

    # Strip the duration so its numbers are not read as years.
    working = re.split(r"\s+[-·–]\s+(?=\d+\s*(?:yrs?|mos?|years?|months?))",
                       caption, maxsplit=1)[0]

    halves = re.split(r"\s*[-–—]\s*|\s+to\s+", working, maxsplit=1)
    start_text = halves[0] if halves else ""
    end_text = halves[1] if len(halves) > 1 else ""

    is_current = any(token in end_text.lower() for token in PRESENT_TOKENS)

    start = _parse_single_date(start_text)
    end = None if is_current else _parse_single_date(end_text)
    return start, end, is_current


def _parse_single_date(text: str) -> DatePart | None:
    if not text or not text.strip():
        return None

    month_year = _MONTH_YEAR_RE.search(text)
    if month_year:
        month = MONTHS.get(month_year.group(1)[:3].lower())
        return DatePart(year=int(month_year.group(2)), month=month)

    year = _YEAR_RE.search(text)
    if year:
        return DatePart(year=int(year.group(1)))
    return None


def months_between(start: DatePart | None, end: DatePart | None) -> int | None:
    """Whole months from `start` to `end`, or to now. January when no month."""
    if start is None or start.year is None:
        return None

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    end_year = end.year if end and end.year else now.year
    end_month = (end.month if end and end.month else (1 if end else now.month))

    months = (end_year - start.year) * 12 + (end_month - (start.month or 1))
    # A single-month role is one month, not zero.
    return max(1, months + 1) if months >= 0 else None


def parse_duration_months(caption: str | None) -> int | None:
    """Read "3 yrs 2 mos" out of a caption, if it carries one."""
    if not caption:
        return None
    tail = re.search(
        r"(\d+\s*(?:yrs?|years?))?\s*(\d+\s*(?:mos?|months?))?\s*$", caption.strip(),
        re.IGNORECASE,
    )
    if not tail or not tail.group(0).strip():
        return None
    match = _DURATION_RE.search(tail.group(0))
    if not match:
        return None
    years, months = match.group(1), match.group(2)
    if not years and not months:
        return None
    return (int(years) * 12 if years else 0) + (int(months) if months else 0)


def split_on_dot(value: str | None) -> list[str]:
    """Split a subtitle on its middle dot: "Acme Corp · Full-time"."""
    if not value:
        return []
    parts = re.split(r"\s*[·•]\s*", value)
    return [p.strip() for p in parts if p.strip()]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    if isinstance(value, float):
        return int(value)
    return None


def parse_count(value: Any) -> tuple[int | None, bool]:
    """Read a count, flagging LinkedIn's 500+ cap."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value, value >= 500
    text = clean_text(value)
    if not text:
        return None, False
    capped = "+" in text
    number = _as_int(text)
    return number, capped or (number is not None and number >= 500)
