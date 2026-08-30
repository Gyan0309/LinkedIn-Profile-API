"""How the client behaves when LinkedIn says no.

The central claim this file defends: a 429 is retried and a block is not. If
that inverts, the service turns a temporary throttle into a permanently banned
account, and no other test in the suite would notice.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import Settings
from app.errors import (
    LinkedInBlocked,
    LinkedInRateLimited,
    ProfileNotFound,
    QueryRejected,
    SessionRejected,
    SessionUnavailable,
    UpstreamUnavailable,
)
from app.linkedin.auth import SessionManager, parse_cookie_header
from app.linkedin.client import MAX_ATTEMPTS, VoyagerClient

VOYAGER = "https://www.linkedin.com/voyager/api"
PROFILE_URL = f"{VOYAGER}/identity/profiles/x/profileView"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> VoyagerClient:
    """A client with a fake cookie session and no real waiting."""

    async def no_wait(*_args, **_kwargs) -> None:
        return None

    # Backoff and pacing are correctness-relevant but not what these tests assert,
    # and sleeping through them would make the suite take a minute.
    monkeypatch.setattr(VoyagerClient, "_backoff", staticmethod(no_wait))
    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(_env_file=None)
    cookie = 'li_at=synthetic-cookie-value; JSESSIONID="ajax:1234567890123456789"'
    return VoyagerClient(settings, SessionManager(settings, cookie_override=cookie))


@respx.mock
async def test_999_is_never_retried(client: VoyagerClient) -> None:
    """A block must cost exactly one request. Retrying it escalates the block."""
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(999))

    with pytest.raises(LinkedInBlocked) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 1
    assert "999" in str(excinfo.value)
    assert excinfo.value.status_code == 503
    assert excinfo.value.reason == "linkedin_blocked"


@respx.mock
async def test_999_trips_the_breaker_and_stops_later_calls(
    client: VoyagerClient,
) -> None:
    """Once blocked, the next caller fails fast without touching the network."""
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(999))

    with pytest.raises(LinkedInBlocked):
        await client.get_voyager("identity/profiles/x/profileView")
    with pytest.raises(LinkedInBlocked) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 1  # the second call never left the process
    assert client.breaker.status()["open"] is True
    assert excinfo.value.retry_after > 0


@respx.mock
async def test_403_is_treated_as_a_block(client: VoyagerClient) -> None:
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(LinkedInBlocked):
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 1


@respx.mock
async def test_429_is_retried_to_the_attempt_limit(client: VoyagerClient) -> None:
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(LinkedInRateLimited):
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == MAX_ATTEMPTS
    # A throttle is transient, so it must not trip the breaker.
    assert client.breaker.status()["open"] is False


@respx.mock
async def test_429_then_success_recovers(client: VoyagerClient) -> None:
    respx.get(PROFILE_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"data": {"ok": True}, "included": []}),
        ]
    )

    body = await client.get_voyager("identity/profiles/x/profileView")

    assert body["data"]["ok"] is True


@respx.mock
async def test_5xx_is_retried(client: VoyagerClient) -> None:
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(UpstreamUnavailable):
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == MAX_ATTEMPTS


@respx.mock
async def test_401_reacquires_the_session_exactly_once(client: VoyagerClient) -> None:
    """A dead cookie is worth one re-acquisition, not a retry loop."""
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(401))

    with pytest.raises(SessionUnavailable) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 2
    assert "expired" in str(excinfo.value)


@respx.mock
async def test_401_then_success(client: VoyagerClient) -> None:
    respx.get(PROFILE_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"data": {}, "included": []}),
        ]
    )

    assert await client.get_voyager("identity/profiles/x/profileView") == {
        "data": {},
        "included": [],
    }


@respx.mock
async def test_404_is_a_missing_profile(client: VoyagerClient) -> None:
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(ProfileNotFound) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 1
    assert excinfo.value.status_code == 404


@respx.mock
async def test_400_surfaces_as_a_rejected_query(client: VoyagerClient) -> None:
    """Distinct from a transport error so the queryId can be rediscovered."""
    route = respx.get(PROFILE_URL).mock(return_value=httpx.Response(400))

    with pytest.raises(QueryRejected):
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 1


@respx.mock
async def test_non_json_body_is_reported_not_swallowed(client: VoyagerClient) -> None:
    """An HTML interstitial with a 200 is a failure, not an empty profile."""
    respx.get(PROFILE_URL).mock(
        return_value=httpx.Response(200, text="<html>sign in</html>")
    )

    with pytest.raises(UpstreamUnavailable) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert "non-JSON" in str(excinfo.value)


@respx.mock
async def test_transport_failure_is_retried_then_reported(
    client: VoyagerClient,
) -> None:
    route = respx.get(PROFILE_URL).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(UpstreamUnavailable):
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == MAX_ATTEMPTS


@respx.mock
async def test_restli_syntax_survives_url_encoding(client: VoyagerClient) -> None:
    """Voyager rejects percent-encoded parens and colons, so they must pass through."""
    route = respx.get("https://www.linkedin.com/voyager/api/graphql").mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )

    await client.get_voyager(
        "graphql",
        {"variables": "(vanityName:some-person)", "queryId": "voyagerX.abc123"},
    )

    requested = str(route.calls[0].request.url)
    assert "(vanityName:some-person)" in requested
    assert "%28" not in requested and "%3A" not in requested


async def test_session_manager_refuses_when_nothing_is_configured() -> None:
    with pytest.raises(SessionUnavailable) as excinfo:
        await SessionManager(Settings(_env_file=None)).get()

    assert "X-LinkedIn-Cookie" in str(excinfo.value)


async def test_redacted_session_never_exposes_the_cookie() -> None:
    manager = SessionManager(
        Settings(_env_file=None), cookie_override=f"li_at={'A' * 40}"
    )

    redacted = (await manager.get()).redacted()

    assert "A" * 40 not in str(redacted)
    assert redacted["li_at_fingerprint"] == "AAAA...AAAA"


# --- a rejected session -----------------------------------------------------


@respx.mock
async def test_redirect_to_login_is_diagnosed_not_called_unexpected(
    client: VoyagerClient,
) -> None:
    """A 3xx from Voyager always means the session was refused.

    Reporting it as a generic "unexpected response" sent the reader looking for
    a LinkedIn outage when the answer was their cookie.
    """
    respx.get(PROFILE_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://www.linkedin.com/uas/login?..."}
        )
    )

    with pytest.raises(SessionRejected) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert excinfo.value.reason == "linkedin_session_rejected"
    assert "X-LinkedIn-Cookie" in str(excinfo.value)


@respx.mock
async def test_redirect_to_checkpoint_says_verify_in_a_browser(
    client: VoyagerClient,
) -> None:
    respx.get(PROFILE_URL).mock(
        return_value=httpx.Response(
            303, headers={"location": "https://www.linkedin.com/checkpoint/challenge/"}
        )
    )

    with pytest.raises(SessionRejected) as excinfo:
        await client.get_voyager("identity/profiles/x/profileView")

    assert "browser" in str(excinfo.value)


@respx.mock
async def test_a_rejected_session_is_not_retried(client: VoyagerClient) -> None:
    """The cookie will not become valid on the second attempt."""
    route = respx.get(PROFILE_URL).mock(
        return_value=httpx.Response(302, headers={"location": "/uas/login"})
    )

    with pytest.raises(SessionRejected):
        await client.get_voyager("identity/profiles/x/profileView")

    assert route.call_count == 1


# --- the full cookie header -------------------------------------------------


def test_cookie_header_parsing_is_tolerant() -> None:
    """People paste with a `Cookie:` prefix, with quotes, and across lines."""
    jar = parse_cookie_header(
        '  Cookie: li_at=AQEDvalue;  JSESSIONID="ajax:99";  bcookie=v2  '
    )
    assert jar["li_at"] == "AQEDvalue"
    assert jar["JSESSIONID"] == '"ajax:99"'
    assert jar["bcookie"] == "v2"

    wrapped = parse_cookie_header("li_at=AQEDvalue;\n  lidc=b=tr1;\r\n bscookie=x")
    assert wrapped["li_at"] == "AQEDvalue"
    assert wrapped["bscookie"] == "x"

    assert parse_cookie_header("   ") == {}
    assert parse_cookie_header("nonsense-with-no-equals") == {}


async def test_full_cookie_header_is_sent_verbatim() -> None:
    """Reconstructing the cookie set from two values would drop the rest."""
    raw = 'li_at=AQEDvalue; JSESSIONID="ajax:4242424242424242424"; lidc=b=tr1; bcookie=v=2'
    session = await SessionManager(
        Settings(_env_file=None), cookie_override=raw
    ).get()

    assert session.source == "caller-header"
    assert session.cookie_header == raw
    assert session.li_at == "AQEDvalue"
    assert session.csrf_token == "ajax:4242424242424242424"


async def test_cookie_header_without_jsessionid_gets_one_appended() -> None:
    """The csrf-token header needs a JSESSIONID to agree with."""
    session = await SessionManager(
        Settings(_env_file=None), cookie_override="li_at=AQEDvalue; bcookie=v=2"
    ).get()

    assert session.csrf_token.startswith("ajax:")
    assert f'JSESSIONID="{session.csrf_token}"' in session.cookie_header
    assert "li_at=AQEDvalue" in session.cookie_header


async def test_cookie_header_missing_li_at_is_refused_with_a_reason() -> None:
    """A header without li_at is not a logged-in session, whatever else it holds."""
    manager = SessionManager(
        Settings(_env_file=None), cookie_override="bcookie=v=2; lidc=b=tr1"
    )

    with pytest.raises(SessionUnavailable) as excinfo:
        await manager.get()

    assert "no li_at" in str(excinfo.value)


async def test_a_pasted_curl_style_header_is_accepted() -> None:
    """People paste with the `Cookie:` prefix and wrapping quotes. Both are fine."""
    pasted = (
        '  Cookie: li_at=AQEDvalue; JSESSIONID="ajax:5555555555555555555";'
        " lidc=b=tr1  "
    )

    session = await SessionManager(
        Settings(_env_file=None), cookie_override=pasted
    ).get()

    assert session.li_at == "AQEDvalue"
    assert session.csrf_token == "ajax:5555555555555555555"
    # The `Cookie:` prefix must not survive into the outgoing header.
    assert not session.cookie_header.lower().startswith("cookie:")


# --- query encoding ---------------------------------------------------------


@respx.mock
async def test_a_urn_parameter_is_percent_encoded(client: VoyagerClient) -> None:
    """The bug that made every dash collection answer 400.

    One safe-characters list applied to every parameter sent `profileUrn` with
    literal colons. The same request with `%3A` answers 200.
    """
    route = respx.get(url__startswith=f"{VOYAGER}/identity/dash/profileSkills").mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )

    await client.get_voyager(
        "identity/dash/profileSkills",
        {"q": "viewee", "profileUrn": "urn:li:fsd_profile:ACoAAB", "count": "100"},
    )

    requested = str(route.calls[0].request.url)
    assert "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB" in requested
    assert "urn:li:" not in requested


@respx.mock
async def test_restli_tuple_syntax_still_survives_encoding(
    client: VoyagerClient,
) -> None:
    """The other half of the same rule: `variables` must NOT be encoded."""
    route = respx.get(url__startswith=f"{VOYAGER}/graphql").mock(
        return_value=httpx.Response(200, json={"data": {}, "included": []})
    )

    await client.get_voyager(
        "graphql",
        {"variables": "(vanityName:some-person)", "queryId": "voyagerX.abc123"},
    )

    requested = str(route.calls[0].request.url)
    assert "(vanityName:some-person)" in requested
    assert "%28" not in requested and "%3A" not in requested
