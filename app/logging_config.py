"""Logging setup with a redaction filter.

A service that holds a session cookie will eventually log one by accident -- in
an exception repr, a retry warning, a debug dump of request headers. The filter
below is the backstop for that: it rewrites the formatted record, so a secret has
to survive both the code review and the filter to reach a log aggregator.

Patterns are matched on value shape as well as key name, because the cookie
travels under several names (li_at, Cookie, csrf-token) and inside f-strings that
no key-based scrub would catch.
"""

from __future__ import annotations

import logging
import re
import sys

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cookie values, whether in a header string or a keyword argument.
    (re.compile(r"(li_at\s*[=:]\s*)[^\s;,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(JSESSIONID\s*[=:]\s*\"?)[^\s;,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(csrf-token['\"]?\s*[=:]\s*['\"]?)ajax:[0-9]+", re.IGNORECASE),
     r"\1[REDACTED]"),
    # A bare ajax: session id appearing anywhere at all.
    (re.compile(r"\bajax:[0-9]{10,}"), "[REDACTED]"),
    # Our own API keys, and anything shaped like a password kwarg.
    (re.compile(r"(x-api-key['\"]?\s*[=:]\s*['\"]?)[^\s,'\"]+", re.IGNORECASE),
     r"\1[REDACTED]"),
    (re.compile(r"(session_password['\"]?\s*[=:]\s*['\"]?)[^\s,'\"]+", re.IGNORECASE),
     r"\1[REDACTED]"),
    (re.compile(r"(password['\"]?\s*[=:]\s*['\"]?)[^\s,'\"]+", re.IGNORECASE),
     r"\1[REDACTED]"),
)


class RedactingFilter(logging.Filter):
    """Scrubs secrets from the rendered message before a handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not break logging
            return True

        redacted = message
        for pattern, replacement in _REDACTIONS:
            redacted = pattern.sub(replacement, redacted)

        if redacted != message:
            # Collapse args into the already-formatted message; re-formatting
            # would reintroduce the unredacted values.
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx logs the full request URL at INFO, which is noise here and a leak risk
    # if a query string ever carries something sensitive.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
