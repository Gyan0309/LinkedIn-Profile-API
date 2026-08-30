"""Logging setup: request correlation, stage tracing, and secret redaction.

Three jobs.

**Correlation.** Every log line carries a short request id, so when two callers
overlap you can still read one request's story end to end. The id lives in a
ContextVar rather than being threaded through every function signature, because
the interesting logs come from four layers down (the HTTP client) and plumbing an
id through the fetch chain to reach them would be noise in every signature.

**Tracing.** `stage()` emits a consistent, greppable line per step. A failed
request should be diagnosable from the log alone, without adding prints and
re-running -- by the time you are re-running against LinkedIn you are spending
the account's request budget to learn something the log should have told you the
first time.

**Redaction.** A service holding a session cookie will eventually log one by
accident -- in an exception repr, a retry warning, a header dump. The filter
rewrites formatted records, matching on value *shape* as well as key name, so a
bare `ajax:` session id is caught wherever it appears.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar

# "-" outside a request: startup, shutdown, and the live_test script.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cookie values, in a header string or a keyword argument.
    (re.compile(r"(li_at\s*[=:]\s*)[^\s;,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(r"(JSESSIONID\s*[=:]\s*\"?)[^\s;,'\"]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(csrf-token['\"]?\s*[=:]\s*['\"]?)ajax:[0-9]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    # A bare ajax: session id appearing anywhere at all.
    (re.compile(r"\bajax:[0-9]{10,}"), "[REDACTED]"),
    # Our own API keys, and anything shaped like a password.
    (
        re.compile(r"(x-api-key['\"]?\s*[=:]\s*['\"]?)[^\s,'\"]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(session_password['\"]?\s*[=:]\s*['\"]?)[^\s,'\"]+", re.IGNORECASE
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(password['\"]?\s*[=:]\s*['\"]?)[^\s,'\"]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
)


def new_request_id() -> str:
    """A short id. Six hex characters is plenty to disambiguate concurrent calls."""
    return uuid.uuid4().hex[:6]


def set_request_id(value: str) -> None:
    request_id_var.set(value)


def get_request_id() -> str:
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RedactingFilter(logging.Filter):
    """Scrub secrets from the rendered message before a handler sees it."""

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


def stage(
    logger: logging.Logger,
    step: str,
    message: str = "",
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit one trace line: a step name, a message, then `key=value` pairs.

    Uniform shape on purpose -- `grep 'S1'` or `grep 'cache'` should pull a
    coherent slice out of a busy log without needing a parser.
    """
    parts = [f"{step:<16}"]
    if message:
        parts.append(message)
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    logger.log(level, " ".join(parts))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(request_id)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    # Order matters: the id is attached before redaction rewrites the message.
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx logs the full request URL at INFO. We log our own, better-shaped line
    # for every upstream call, so this would only duplicate it -- and a query
    # string is exactly where a secret would hide.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # uvicorn's access log carries no request id and duplicates our own lines.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
