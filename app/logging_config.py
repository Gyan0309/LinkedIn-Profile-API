"""Logging: request correlation, stage tracing, secret redaction.

The redaction filter matches on value shape as well as key name, so a bare
`ajax:` session id is caught wherever it appears.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar

# "-" outside a request: startup, shutdown, scripts.
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
    """Six hex characters, enough to tell concurrent requests apart."""
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
            # Collapse args in; re-formatting would restore the raw values.
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
    """One trace line: step name, message, then `key=value` pairs."""
    parts = [f"{step:<16}"]
    if message:
        parts.append(message)
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    logger.log(level, " ".join(parts))


class _Ansi:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GREY = "\033[90m"


def _enable_windows_ansi() -> bool:
    """Turn on virtual-terminal processing so ANSI codes render on Windows."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT
        # | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7))
    except Exception:  # noqa: BLE001 - colour is a nicety, never a failure
        return False


def _should_colour(mode: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        _enable_windows_ansi()
        return True
    # auto: colour a terminal, never a redirected file or a log collector, which
    # would otherwise receive escape sequences as literal garbage.
    if not sys.stdout.isatty():
        return False
    return _enable_windows_ansi()


def _is_success(rendered: str) -> bool:
    """Whether a line reports an outcome. `chain starting` is not one."""
    return (
        "status=2" in rendered
        or rendered.startswith("served")
        or " done source=" in rendered
    )


class ColourFormatter(logging.Formatter):
    """Tints by meaning, not just level -- a successful call is also INFO."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        rendered = record.getMessage()

        if record.levelno >= logging.ERROR:
            body = _Ansi.RED + _Ansi.BOLD
        elif record.levelno >= logging.WARNING:
            body = _Ansi.YELLOW
        elif _is_success(rendered):
            body = _Ansi.GREEN
        elif rendered.startswith(("-> request", "<- request")):
            body = _Ansi.CYAN
        else:
            body = ""

        timestamp, _, rest = message.partition(" ")
        tinted = f"{body}{rest}{_Ansi.RESET}" if body else rest
        return f"{_Ansi.GREY}{timestamp}{_Ansi.RESET} {tinted}"


def configure_logging(level: str = "INFO", colour: str = "auto") -> None:
    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s %(levelname)-5s [%(request_id)s] %(message)s"
    formatter_cls = ColourFormatter if _should_colour(colour) else logging.Formatter
    handler.setFormatter(formatter_cls(fmt, datefmt="%H:%M:%S"))
    # Order matters: the id is attached before redaction rewrites the message.
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx logs full URLs at INFO -- duplicated, and where secrets hide.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # uvicorn's access log has no request id and duplicates ours.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
