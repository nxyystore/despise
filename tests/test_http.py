"""HTTP tests: shared session, bucket keys, rate limits, typed errors."""

import asyncio
import base64
import json
import time

import pytest

from despise.exceptions import (
    AuthenticationHTTPError,
    Forbidden,
    HTTPError,
    NotFound,
    RateLimited,
    ServerError,
)
from despise.headers import EphemeralIds, build_profile
from despise.http import DespiseHttp, encode_context

EPH = EphemeralIds(client_launch_id="test-launch-id")


class FakeResp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeSession:
    """Stand-in for AsyncSession; records calls.

    Uses curl_cffi's real ``Headers`` type so session-header behaviour
    (case-insensitive replace, pop) is not faked away.
    """

    def __init__(self, responses, headers=None):
        from curl_cffi.requests.headers import Headers

        self._responses = list(responses)
        self.calls = []
        self.headers = Headers(headers or {})

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

    async def close(self):
        pass


def make_http(responses):
    profile = build_profile("web")
    http = DespiseHttp(profile, EPH)
    fake = FakeSession(responses)
    http.session = fake  # type: ignore[assignment]
    # mirror what __init__ does to the real session
    http._sync_headers()
    return http, fake


def run(coro):
    return asyncio.run(coro)


# -- session ownership ------------------------------------------------------
def test_single_session_owned():
    profile = build_profile("web")
    http = DespiseHttp(profile, EPH)
    assert http.session is not None
    assert http.headers["User-Agent"] == profile.user_agent
    run(http.close())


def test_bucket_key_scoping():
    profile = build_profile("web")
    http = DespiseHttp(profile, EPH)
    assert http._bucket_key("GET", "/channels/123/messages") == "GET:channels/123"
    assert http._bucket_key("GET", "/users/@me") == "GET:users/@me"
    run(http.close())


def test_absolute_urls_bypass_api_base():
    http, fake = make_http([FakeResp(200, "{}")])
    run(http.request("GET", "https://cdn.discordapp.com/attachments/x.png"))
    _, url, _ = fake.calls[0]
    assert url == "https://cdn.discordapp.com/attachments/x.png"
    run(http.close())


def test_api_base_is_configurable():
    profile = build_profile("web")
    http = DespiseHttp(profile, EPH, api_base="https://discord.com/api/canary")
    assert http.api_base == "https://discord.com/api/canary"
    run(http.close())


# -- retries ----------------------------------------------------------------
def test_429_then_success():
    ok = FakeResp(200, '{"id": "1"}')
    limited = FakeResp(429, '{"retry_after": 0.01}', {})
    http, fake = make_http([limited, ok])
    out = run(http.request("GET", "/users/@me"))
    assert out == {"id": "1"}
    assert len(fake.calls) == 2
    run(http.close())


def test_429_exhausted_raises_rate_limited():
    body = json.dumps({"retry_after": 0.001, "global": False, "code": 30003})
    http, fake = make_http([FakeResp(429, body, {})] * 6)
    with pytest.raises(RateLimited) as excinfo:
        run(http.request("GET", "/users/@me"))
    assert excinfo.value.status == 429
    assert excinfo.value.code == 30003
    assert excinfo.value.is_global is False
    assert len(fake.calls) == 6  # initial attempt + MAX_429_RETRIES
    run(http.close())


def test_global_429_flags_the_error():
    body = json.dumps({"retry_after": 0.001, "global": True})
    http, _ = make_http([FakeResp(429, body, {})] * 6)
    with pytest.raises(RateLimited) as excinfo:
        run(http.request("GET", "/users/@me"))
    assert excinfo.value.is_global is True
    run(http.close())


def test_5xx_retry_on_idempotent():
    ok = FakeResp(200, '{"ok": true}')
    bad = FakeResp(502, "bad gateway")
    http, _ = make_http([bad, ok])
    assert run(http.request("GET", "/gateway")) == {"ok": True}
    run(http.close())


def test_5xx_not_retried_on_post():
    """Re-POSTing a message after a 502 is how you send it twice."""
    http, fake = make_http([FakeResp(502, "bad gateway")])
    with pytest.raises(ServerError):
        run(http.request("POST", "/channels/1/messages", json_data={"content": "x"}))
    assert len(fake.calls) == 1
    run(http.close())


def test_5xx_exhausted_raises_server_error():
    http, fake = make_http([FakeResp(503, "nope")] * 4)
    with pytest.raises(ServerError):
        run(http.request("GET", "/gateway"))
    assert len(fake.calls) == 4
    run(http.close())


def test_521_is_not_retried():
    http, fake = make_http([FakeResp(521, "web server is down")])
    with pytest.raises(ServerError):
        run(http.request("GET", "/gateway"))
    assert len(fake.calls) == 1
    run(http.close())


# -- typed errors -----------------------------------------------------------
def test_401_raises_authentication_error():
    http, _ = make_http([FakeResp(401, "401: Unauthorized")])
    with pytest.raises(AuthenticationHTTPError) as excinfo:
        run(http.request("GET", "/users/@me"))
    assert excinfo.value.status == 401
    assert "401" in str(excinfo.value)
    run(http.close())


def test_401_with_a_token_trips_the_breaker():
    http, _ = make_http([FakeResp(401, "401: Unauthorized")])
    http.set_token("tok")
    seen = []
    http.on_auth_dead(seen.append)
    with pytest.raises(AuthenticationHTTPError):
        run(http.request("GET", "/users/@me"))
    assert http.auth_dead is True
    assert len(seen) == 1 and isinstance(seen[0], AuthenticationHTTPError)
    run(http.close())


def test_tripped_breaker_fails_fast_with_no_network_call():
    http, fake = make_http([FakeResp(401, "401: Unauthorized")])
    http.set_token("tok")
    with pytest.raises(AuthenticationHTTPError):
        run(http.request("GET", "/users/@me"))
    calls = len(fake.calls)
    with pytest.raises(AuthenticationHTTPError):
        run(http.request("GET", "/users/@me"))
    # the dead token is never sent again
    assert len(fake.calls) == calls
    run(http.close())


def test_unauthenticated_401_does_not_trip_the_breaker():
    http, _ = make_http([FakeResp(401, "401: Unauthorized")])
    with pytest.raises(AuthenticationHTTPError):
        run(http.request("GET", "/gateway", auth=False))
    assert http.auth_dead is False
    run(http.close())


def test_403_raises_forbidden_with_discord_code():
    body = json.dumps({"message": "Missing Permissions", "code": 50013})
    http, _ = make_http([FakeResp(403, body)])
    with pytest.raises(Forbidden) as excinfo:
        run(http.request("DELETE", "/channels/1/messages/2"))
    exc = excinfo.value
    assert exc.status == 403
    assert exc.code == 50013
    assert exc.message == "Missing Permissions"
    assert exc.method == "DELETE"
    assert exc.path == "/channels/1/messages/2"
    assert "50013" in str(exc)
    run(http.close())


def test_404_raises_not_found():
    http, _ = make_http([FakeResp(404, json.dumps({"code": 10003}))])
    with pytest.raises(NotFound) as excinfo:
        run(http.request("GET", "/channels/999/messages"))
    assert excinfo.value.code == 10003
    run(http.close())


def test_validation_errors_are_carried_through():
    body = json.dumps(
        {
            "code": 50035,
            "message": "Invalid Form Body",
            "errors": {"content": {"_errors": [{"code": "BASE_TYPE_MAX_LENGTH"}]}},
        }
    )
    http, _ = make_http([FakeResp(400, body)])
    with pytest.raises(HTTPError) as excinfo:
        run(http.request("POST", "/channels/1/messages", json_data={}))
    exc = excinfo.value
    assert type(exc) is HTTPError  # 4xx with no specific subclass
    assert exc.code == 50035
    assert "content" in exc.errors
    run(http.close())


def test_non_json_error_body_is_truncated():
    http, _ = make_http([FakeResp(400, "<html>blocked by proxy</html>")])
    with pytest.raises(HTTPError) as excinfo:
        run(http.request("GET", "/users/@me"))
    assert "blocked by proxy" in excinfo.value.message
    assert isinstance(excinfo.value.body, str)
    run(http.close())


# -- headers ----------------------------------------------------------------
def test_context_preset_header():
    http, fake = make_http([FakeResp(200, "{}")])
    run(http.request("POST", "/invites/abc", context="accept"))
    _, _, kwargs = fake.calls[0]
    assert "X-Context-Properties" in kwargs["headers"]
    run(http.close())


def test_audit_log_reason_is_uri_encoded():
    http, fake = make_http([FakeResp(200, "{}")])
    run(http.request("DELETE", "/channels/1/messages/2", reason="spam clean up"))
    assert fake.calls[0][2]["headers"]["X-Audit-Log-Reason"] == "spam%20clean%20up"
    run(http.close())


def test_frozen_headers_live_on_the_session():
    """Frozen headers go on the session so per-request merges cannot reorder them."""
    profile = build_profile("web")
    http = DespiseHttp(profile, EPH)
    on_session = set(http.session.headers)
    assert "user-agent" in on_session
    assert "x-super-properties" in on_session
    assert "x-discord-locale" in on_session
    assert "x-discord-timezone" in on_session
    assert "sec-ch-ua" in on_session
    assert "authorization" not in on_session
    run(http.close())


def test_token_is_injected_per_request_only():
    http, fake = make_http([FakeResp(200, "{}"), FakeResp(200, "{}")])
    http.set_token("tok")
    # never on the session: curl_cffi would stringify a None override as "None"
    assert "authorization" not in http.session.headers
    run(http.request("GET", "/users/@me"))
    assert fake.calls[0][2]["headers"]["Authorization"] == "tok"
    run(http.request("GET", "/gateway", auth=False))
    assert "Authorization" not in (fake.calls[1][2]["headers"] or {})
    run(http.close())


def test_super_properties_rotation_syncs_to_session():
    import base64
    import json as _json

    http, _ = make_http([])
    before = http.session.headers["x-super-properties"]
    http.eph.client_heartbeat_session_id = "rotated"
    http.refresh_super_properties()
    after = http.session.headers["x-super-properties"]
    assert after != before
    decoded = _json.loads(base64.b64decode(after))
    assert decoded["client_heartbeat_session_id"] == "rotated"
    run(http.close())


def test_set_profile_rebuilds_session_headers():
    http, _ = make_http([])
    http.set_token("tok")
    http.set_profile(build_profile("android"))
    assert "Discord-Android" in http.session.headers["user-agent"]
    # the token survives a profile rebuild
    assert http.headers["Authorization"] == "tok"
    run(http.close())


def test_unauthenticated_request_omits_token():
    http, fake = make_http([FakeResp(200, '{"url": "wss://x"}')])
    http.set_token("tok")
    run(http.request("GET", "/gateway", auth=False))
    sent = fake.calls[0][2]["headers"] or {}
    assert "Authorization" not in sent
    run(http.close())


def test_x_fingerprint_dropped_once_token_set():
    http, _ = make_http([])
    assert http.headers["X-Fingerprint"] == ""
    assert "x-fingerprint" in http.session.headers
    http.set_token("tok")
    assert "X-Fingerprint" not in http.headers
    assert "x-fingerprint" not in http.session.headers
    assert http.headers["Authorization"] == "tok"
    run(http.close())


def test_encode_context_roundtrip():
    assert json.loads(base64.b64decode(encode_context({"a": 1}))) == {"a": 1}


def test_bucket_reset_from_headers():
    http, _ = make_http(
        [
            FakeResp(
                200,
                "{}",
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.01"},
            )
        ]
    )
    run(http.request("GET", "/users/@me"))
    bucket = http._buckets["GET:users/@me"]
    assert bucket.reset_at > 0
    run(http.close())


def test_204_returns_empty_dict():
    http, _ = make_http([FakeResp(204, "")])
    assert run(http.request("DELETE", "/channels/1")) == {}
    run(http.close())


# -- bucket learning --------------------------------------------------------
def limits(bucket=None, scope=None, remaining="0", reset_after="0", precision=None):
    h = {}
    if bucket:
        h["X-RateLimit-Bucket"] = bucket
    if scope:
        h["X-RateLimit-Scope"] = scope
    if remaining is not None:
        h["X-RateLimit-Remaining"] = remaining
    if reset_after is not None:
        h["X-RateLimit-Reset-After"] = reset_after
    if precision:
        h["X-RateLimit-Precision"] = precision
    return h


def test_bucket_id_replaces_the_path_heuristic():
    """Two unrelated routes sharing a Discord bucket must share one lock."""
    http, fake = make_http(
        [
            FakeResp(200, "{}", limits(bucket="abc123", scope="shared")),
            FakeResp(200, "{}", limits(bucket="abc123", scope="shared")),
        ]
    )
    run(http.request("GET", "/channels/1/messages"))
    run(http.request("GET", "/guilds/9/members"))
    # the heuristic said these were different routes...
    assert http._bucket_key("GET", "/channels/1/messages") != http._bucket_key(
        "GET", "/guilds/9/members"
    )
    # ...but Discord's own hash says they are the same bucket
    assert http._route_buckets[http._bucket_key("GET", "/channels/1/messages")] == "abc123"
    assert http._resolve_bucket("GET", "/guilds/9/members")[0] is http._resolve_bucket(
        "GET", "/channels/1/messages"
    )[0]
    run(http.close())


def test_user_scope_routes_share_one_account_lock():
    """scope=user means the limit is per-account, shared across routes."""
    http, _ = make_http(
        [
            FakeResp(200, "{}", limits(bucket="u1", scope="user")),
            FakeResp(200, "{}", limits(bucket="u2", scope="user")),
        ]
    )
    run(http.request("GET", "/users/@me/relationships"))
    run(http.request("GET", "/users/@me/notes"))
    # distinct bucket ids, but both resolve to the single user lock
    assert http._resolve_bucket("GET", "/users/@me/relationships")[0] is (
        http._user_bucket
    )
    assert http._resolve_bucket("GET", "/users/@me/notes")[0] is http._user_bucket
    run(http.close())


def test_user_scoped_reset_arms_the_shared_lock():
    http, _ = make_http(
        [FakeResp(200, "{}", limits(bucket="u1", scope="user", reset_after="5"))]
    )
    run(http.request("GET", "/users/@me/relationships"))
    assert http._user_bucket.reset_at > 0
    run(http.close())


def test_millisecond_precision_is_converted():
    http, _ = make_http(
        [
            FakeResp(
                200,
                "{}",
                limits(bucket="b", reset_after="2500", precision="millisecond"),
            )
        ]
    )
    start = time.monotonic()
    run(http.request("GET", "/users/@me"))
    bucket, _ = http._resolve_bucket("GET", "/users/@me")
    # 2500ms -> 2.5s, not 2500s
    assert 0 < bucket.reset_at - start <= 3.0
    run(http.close())


def test_reset_is_never_shortened_by_a_later_route():
    http, _ = make_http(
        [
            FakeResp(200, "{}", limits(bucket="shared", reset_after="0.2")),
            FakeResp(200, "{}", limits(bucket="shared", reset_after="0.01")),
        ]
    )
    run(http.request("GET", "/channels/1/messages"))
    bucket, _ = http._resolve_bucket("GET", "/channels/1/messages")
    long_reset = bucket.reset_at
    assert long_reset > 0
    run(http.request("GET", "/channels/1"))
    assert bucket.reset_at >= long_reset
    run(http.close())


def test_first_reset_survives_the_heuristic_handoff():
    """The learn step must not strand an armed reset on a dead bucket."""
    http, _ = make_http(
        [FakeResp(200, "{}", limits(bucket="late", reset_after="0.2"))],
    )
    route = http._bucket_key("GET", "/users/@me")
    run(http.request("GET", "/users/@me"))
    # the heuristic bucket is retired...
    assert route not in http._buckets
    # ...and the real bucket carries the wait forward
    bucket, _ = http._resolve_bucket("GET", "/users/@me")
    assert bucket.reset_at > 0
    run(http.close())


def test_global_scope_arms_the_global_gate():
    http, _ = make_http(
        [FakeResp(200, "{}", limits(bucket="g", scope="global", reset_after="10"))]
    )
    run(http.request("GET", "/users/@me"))
    assert http._global_until > 0
    run(http.close())


def test_no_limit_headers_leaves_the_heuristic_alone():
    http, _ = make_http([FakeResp(200, "{}", {})])
    run(http.request("GET", "/users/@me"))
    assert http._route_buckets == {}
    assert "GET:users/@me" in http._buckets
    run(http.close())


# -- multipart --------------------------------------------------------------
def test_upload_sends_a_multipart_body():
    http, fake = make_http([FakeResp(200, '{"id":"1"}')])
    out = run(
        http.upload(
            "/users/@me",
            fields={"payload": '{"avatar":"data:..."}'},
            file_field="avatar",
            filename="image.png",
            data=b"\x89PNG fake",
            content_type="image/png",
            method="PATCH",
        )
    )
    assert out == {"id": "1"}
    _, url, kwargs = fake.calls[0]
    assert url.endswith("/users/@me")
    # curl_cffi has no files= kwarg; the body must be a CurlMime
    assert kwargs["multipart"] is not None
    assert kwargs["json"] is None
    run(http.close())


def test_upload_supports_only_fields():
    http, fake = make_http([FakeResp(200, "{}")])
    run(http.upload("/guilds/1/emojis", fields={"name": "blob"}))
    assert fake.calls[0][2]["multipart"] is not None
    run(http.close())


def test_upload_accepts_a_list_of_files():
    http, fake = make_http([FakeResp(200, '{"id":"1"}')])
    out = run(
        http.upload(
            "/channels/c1/messages",
            fields={"payload_json": "{}"},
            files=[
                {
                    "field": "files[0]",
                    "filename": "a.png",
                    "content_type": "image/png",
                    "data": b"x",
                },
                {"field": "files[1]", "filename": "b.txt", "data": b"y"},
            ],
        )
    )
    assert out == {"id": "1"}
    assert fake.calls[0][2]["multipart"] is not None
    run(http.close())
