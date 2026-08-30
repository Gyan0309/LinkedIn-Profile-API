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
    SessionUnavailable,
    UpstreamUnavailable,
)
from app.linkedin.auth import SessionManager
from app.linkedin.client import MAX_ATTEMPTS, VoyagerClient

PROFILE_URL = "https://www.linkedin.com/voyager/api/identity/profiles/x/profileView"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> VoyagerClient:
    """A client with a fake cookie session and no real waiting."""

    async def no_wait(*_args, **_kwargs) -> None:
        return None

    # Backoff and pacing are correctness-relevant but not what these tests assert,
    # and sleeping through them would make the suite take a minute.
    monkeypatch.setattr(VoyagerClient, "_backoff", staticmethod(no_wait))
    monkeypatch.setattr("app.linkedin.client.OutboundLimiter.acquire", no_wait)

    settings = Settings(
        _env_file=None,
        linkedin_li_at="synthetic-cookie-value",
        linkedin_jsessionid="ajax:1234567890123456789",
    )
    return VoyagerClient(settings, SessionManager(settings))


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
    settings = Settings(_env_file=None, linkedin_li_at="", linkedin_email="")

    with pytest.raises(SessionUnavailable) as excinfo:
        await SessionManager(settings).get()

    assert "LINKEDIN_LI_AT" in str(excinfo.value)


async def test_session_synthesises_a_jsessionid_when_only_the_cookie_is_given() -> None:
    """The CSRF header only has to match the cookie we send, not be server-issued."""
    settings = Settings(_env_file=None, linkedin_li_at="cookie-value")

    session = await SessionManager(settings).get()

    assert session.csrf_token.startswith("ajax:")
    assert f'JSESSIONID="{session.csrf_token}"' in session.cookie_header
    assert session.cookie_header.startswith("li_at=cookie-value;")


async def test_redacted_session_never_exposes_the_cookie() -> None:
    settings = Settings(_env_file=None, linkedin_li_at="A" * 40)

    redacted = (await SessionManager(settings).get()).redacted()

    assert "A" * 40 not in str(redacted)
    assert redacted["li_at_fingerprint"] == "AAAA...AAAA"
