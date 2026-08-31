"""The caller's browser identity, carried through to LinkedIn.

LinkedIn binds `li_at` to a device fingerprint whose largest component is the
User-Agent. Replaying a session under a different one looks like a stolen
cookie, and LinkedIn's answer is to invalidate the session everywhere -- which
logs the caller out of their own browser. These tests pin the plumbing that
stops that happening.
"""

from __future__ import annotations

import json

import pytest

from app.linkedin.auth import (
    DEFAULT_USER_AGENT,
    MAX_USER_AGENT_LENGTH,
    SessionManager,
    browser_label,
    clean_user_agent,
    parse_timezone_offset,
    platform_of,
)
from app.linkedin.client import VoyagerClient, client_hints

COOKIE = 'li_at=AAAAsession; JSESSIONID="ajax:1111111111111111111"'

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
EDGE = CHROME + " Edg/140.0.0.0"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0"
ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)


# --- sanitising -----------------------------------------------------------


def test_a_supplied_agent_is_used_as_given() -> None:
    assert clean_user_agent(CHROME) == CHROME


def test_an_absent_agent_falls_back_to_the_default() -> None:
    assert clean_user_agent("") == DEFAULT_USER_AGENT
    assert clean_user_agent("   ") == DEFAULT_USER_AGENT


@pytest.mark.parametrize("payload", ["Chrome\r\nX-Evil: 1", "Chrome\nSet-Cookie: a=b"])
def test_header_injection_is_stripped(payload: str) -> None:
    """This string goes straight into an outbound header."""
    cleaned = clean_user_agent(payload)

    assert "\r" not in cleaned
    assert "\n" not in cleaned


def test_an_absurdly_long_agent_is_capped() -> None:
    assert len(clean_user_agent("x" * 5000)) == MAX_USER_AGENT_LENGTH


def test_a_wholly_unprintable_agent_falls_back() -> None:
    assert clean_user_agent("\x00\x01\x02") == DEFAULT_USER_AGENT


# --- timezone -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("5.5", 5.5), ("0", 0.0), ("-8", -8.0), (" 1 ", 1.0)],
)
def test_a_real_offset_is_read(raw: str, expected: float) -> None:
    assert parse_timezone_offset(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "99", "-99", "NaN-ish"])
def test_a_nonsense_offset_becomes_utc(raw: str) -> None:
    assert parse_timezone_offset(raw) == 0.0


# --- fingerprint labels ---------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (CHROME, "Chrome-140-on-Windows"),
        (EDGE, "Edge-140-on-Windows"),
        (FIREFOX, "Firefox-129-on-Windows"),
        (ANDROID, "Chrome-140-on-Android"),
    ],
)
def test_browser_label_is_readable_in_a_log_line(agent: str, expected: str) -> None:
    assert browser_label(agent) == expected


def test_an_unrecognised_agent_says_so_rather_than_guessing() -> None:
    assert browser_label("curl/8.4.0") == "unrecognised"
    assert platform_of("curl/8.4.0") == "Unknown"


# --- client hints ---------------------------------------------------------


def test_chrome_hints_agree_with_the_agent() -> None:
    hints = client_hints(CHROME)

    assert '"Google Chrome";v="140"' in hints["sec-ch-ua"]
    assert hints["sec-ch-ua-platform"] == '"Windows"'
    assert hints["sec-ch-ua-mobile"] == "?0"


def test_edge_is_branded_as_edge_not_chrome() -> None:
    assert '"Microsoft Edge";v="140"' in client_hints(EDGE)["sec-ch-ua"]


def test_a_mobile_agent_sets_the_mobile_hint() -> None:
    assert client_hints(ANDROID)["sec-ch-ua-mobile"] == "?1"


def test_a_non_chromium_agent_sends_no_hints() -> None:
    """Firefox sends none, so neither should we -- absence is the correct value."""
    assert client_hints(FIREFOX) == {}


# --- the outbound request -------------------------------------------------


@pytest.fixture
def client(settings):
    return VoyagerClient(
        settings,
        SessionManager(
            settings, cookie_override=COOKIE, user_agent=EDGE, timezone_offset="5.5"
        ),
    )


async def test_the_callers_agent_reaches_the_wire(client) -> None:
    headers = await client._authenticated_headers()

    assert headers["User-Agent"] == EDGE
    assert '"Microsoft Edge"' in headers["sec-ch-ua"]


async def test_the_callers_clock_reaches_the_wire(client) -> None:
    headers = await client._authenticated_headers()

    assert json.loads(headers["x-li-track"])["timezoneOffset"] == 5.5


async def test_a_whole_offset_is_not_sent_as_a_float(settings) -> None:
    """LinkedIn sends `0`, not `0.0`; matching it costs nothing."""
    client = VoyagerClient(
        settings, SessionManager(settings, cookie_override=COOKIE, timezone_offset="2")
    )

    track = json.loads((await client._authenticated_headers())["x-li-track"])

    assert track["timezoneOffset"] == 2
    assert isinstance(track["timezoneOffset"], int)


async def test_an_api_call_is_marked_as_an_xhr(client) -> None:
    headers = await client._authenticated_headers()

    assert headers["sec-fetch-dest"] == "empty"
    assert headers["sec-fetch-mode"] == "cors"


async def test_a_session_with_no_agent_still_looks_like_a_browser(settings) -> None:
    client = VoyagerClient(settings, SessionManager(settings, cookie_override=COOKIE))

    headers = await client._authenticated_headers()

    assert headers["User-Agent"] == DEFAULT_USER_AGENT
    assert "sec-ch-ua" in headers


async def test_the_browser_is_reported_in_the_redacted_session(client) -> None:
    """The one field worth reading when a caller's session keeps dying."""
    session = await client._sessions.get()

    assert session.redacted()["browser"] == "Edge-140-on-Windows"
