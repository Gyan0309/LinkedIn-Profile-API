"""Logging behaviour.

Redaction is a security property, not a nicety: it is the last thing standing
between a session cookie and a log aggregator. It gets tested like one.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.logging_config import (
    RedactingFilter,
    RequestIdFilter,
    new_request_id,
    request_id_var,
    stage,
)
from app.main import app


def render(message: str, *args: object) -> str:
    """Push a message through the filter and return what a handler would print."""
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=args, exc_info=None,
    )
    RedactingFilter().filter(record)
    return record.getMessage()


# --- redaction --------------------------------------------------------------


def test_cookie_header_is_redacted() -> None:
    out = render('Cookie: li_at=AQEDATestCookieValue12345; JSESSIONID="ajax:9988776655443322110"')

    assert "AQEDATestCookieValue12345" not in out
    assert "9988776655443322110" not in out
    assert "[REDACTED]" in out


def test_bare_session_id_is_caught_anywhere() -> None:
    """Matched on value shape, so an id in free prose is caught too."""
    out = render("csrf mismatch: expected ajax:1234567890123456789 in the header")
    assert "1234567890123456789" not in out


def test_api_key_and_password_are_redacted() -> None:
    assert "supersecretkey123456" not in render("x-api-key=supersecretkey123456")
    assert "hunter2hunter2" not in render("password='hunter2hunter2'")
    assert "hunter2hunter2" not in render("session_password=hunter2hunter2")


def test_redaction_survives_lazy_percent_formatting() -> None:
    """The secret must not slip through via a deferred `%s` argument."""
    out = render("session cookie is li_at=%s", "AQEDASecretValueGoesHere0001")
    assert "AQEDASecretValueGoesHere0001" not in out


def test_ordinary_messages_pass_through_untouched() -> None:
    message = "chain done source=voyager-graphql served=12 ms=2140"
    assert render(message) == message


def test_a_record_with_broken_args_does_not_break_logging() -> None:
    """A logging bug must never take down the request that triggered it."""
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="bad format %d %d", args=("only-one",), exc_info=None,
    )
    assert RedactingFilter().filter(record) is True


# --- request correlation ----------------------------------------------------


def test_request_id_filter_attaches_the_current_id() -> None:
    token = request_id_var.set("abc123")
    try:
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        RequestIdFilter().filter(record)
        assert record.request_id == "abc123"
    finally:
        request_id_var.reset(token)


def test_request_id_defaults_outside_a_request() -> None:
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="startup", args=(), exc_info=None,
    )
    RequestIdFilter().filter(record)
    assert record.request_id == "-"


def test_generated_ids_are_short_and_distinct() -> None:
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(value) == 6 for value in ids)


def test_stage_renders_step_message_and_fields(caplog) -> None:
    logger = logging.getLogger("stage-test")
    with caplog.at_level(logging.INFO, logger="stage-test"):
        stage(logger, "cache", "MISS", identifier="someone", ms=12)

    rendered = caplog.records[0].getMessage()
    assert "cache" in rendered
    assert "MISS" in rendered
    assert "identifier=someone" in rendered
    assert "ms=12" in rendered


def test_stage_honours_the_requested_level(caplog) -> None:
    logger = logging.getLogger("stage-level")
    with caplog.at_level(logging.DEBUG, logger="stage-level"):
        stage(logger, "gaps", "missing", level=logging.WARNING)

    assert caplog.records[0].levelno == logging.WARNING


# --- end to end -------------------------------------------------------------


def test_response_carries_a_request_id_header() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert len(response.headers["X-Request-ID"]) == 6


def test_caller_supplied_request_id_is_echoed_back() -> None:
    """Lets a trace span a proxy without inventing a second id."""
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "trace-me-1"})

    assert response.headers["X-Request-ID"] == "trace-me-1"


def test_oversized_caller_request_id_is_truncated() -> None:
    """The id goes into every log line, so it must not become a payload."""
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "A" * 500})

    assert len(response.headers["X-Request-ID"]) == 32


def test_error_responses_also_carry_the_request_id() -> None:
    """The id is most useful precisely when something went wrong."""
    with TestClient(app) as client:
        response = client.get("/v1/profile?url=https://www.linkedin.com/company/x")

    assert response.status_code == 400
    assert "X-Request-ID" in response.headers
