"""Single shared curl_cffi AsyncSession. Owned by DespiseHttp, borrowed by all.

Rules:
- Exactly ONE AsyncSession per Client. Gateway WS, REST, and boot
  version-fetches all share it (same impersonate/proxy/timeout/headers).
- Restart-frozen: impersonate + base headers are installed on the session
  constructor, NOT passed per request. Passing headers per request makes
  curl_cffi merge them over its own impersonation defaults, which perturbs
  header ordering -- the exact thing we are trying to spoof. Only genuinely
  dynamic headers (Authorization, X-Context-Properties, X-Audit-Log-Reason)
  travel per request, and they *override* the session set.
- Rate limits are driven by Discord's own ``X-RateLimit-Bucket`` +
  ``X-RateLimit-Scope``. The path heuristic is only a bootstrap guess until
  the first response teaches us the real bucket id.
- Every failure surfaces as a typed exception from ``despise.exceptions``
  carrying status + Discord error code, never a bare RuntimeError.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from typing import Any
from urllib.parse import quote

from curl_cffi.requests import AsyncSession

from .exceptions import (
    AuthenticationHTTPError,
    Forbidden,
    HTTPError,
    NotFound,
    RateLimited,
    ServerError,
)
from .headers import (
    BrowserProfile,
    EphemeralIds,
    base_headers,
    encode_super_properties,
    super_properties_dict,
)

log = logging.getLogger("despise.http")

API_BASE = "https://discord.com/api/v9"

# Join/accept/friends context presets (X-Context-Properties, base64 JSON).
CONTEXT_PROPERTIES: dict[str, dict] = {
    "join": {"location": "Join Guild", "location_guild_id": None},
    "accept": {"location": "Accept Invite Page"},
    "friends": {"location": "Add Friend"},
}

MAX_429_RETRIES = 5
MAX_5XX_RETRIES = 3
# Retrying an idempotent request is safe; re-POSTing a message is not.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


def encode_context(props: dict) -> str:
    return base64.b64encode(json.dumps(props, separators=(",", ":")).encode()).decode()


def _is_retryable_5xx(status: int) -> bool:
    # 500/502/503/504 are the transient gateway/CDN pair; 521/522 mean the
    # edge refused us outright, which retries cannot fix -- treat as fatal.
    return status in {500, 502, 503, 504}


class RateLimitBucket:
    __slots__ = ("lock", "reset_at")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.reset_at = 0.0


class DespiseHttp:
    """Owns the one shared AsyncSession. All modules borrow ``self.session``."""

    def __init__(
        self,
        profile: BrowserProfile,
        eph: EphemeralIds,
        *,
        proxy: str | None = None,
        timeout: float = 30.0,
        api_base: str = API_BASE,
    ) -> None:
        self.profile = profile
        self.eph = eph
        self.api_base = api_base.rstrip("/")
        # Canonical-case source of truth. The session holds its own
        # lowercased copy (curl_cffi normalizes keys), so every mutation
        # must be followed by _sync_headers().
        self._headers = base_headers(profile, eph)
        self._token: str = ""
        # No base_url on the session: request() builds absolute URLs itself so
        # that a caller can hand us a full https:// URL (CDN, webhook) without
        # curl_cffi's urljoin silently resolving it against the API host.
        self.session: Any = AsyncSession(
            impersonate=profile.impersonate,
            proxy=proxy,
            timeout=timeout,
            headers=self._headers,
        )
        # Real buckets, keyed by Discord's own hash once we have seen it.
        self._buckets: dict[str, RateLimitBucket] = {}
        # route key -> bucket id, learned from X-RateLimit-Bucket.
        self._route_buckets: dict[str, str] = {}
        # bucket id -> "user" | "global" | "shared".
        self._bucket_scopes: dict[str, str] = {}
        # Every scope=user bucket shares one limit per account, so they all
        # serialize through a single lock. Learning this is what stops
        # unrelated routes from tripping each other's limits.
        self._user_bucket = RateLimitBucket()
        self._global_until = 0.0
        # A 401 on an authenticated request means the token is dead. Once
        # tripped, this object refuses to keep talking to Discord: the caller
        # (a Client) stops the gateway too. There is nothing to resume.
        self._auth_dead = False
        self._auth_error: HTTPError | None = None
        self._on_auth_dead: list = []

    # -- headers ---------------------------------------------------------
    @property
    def headers(self) -> dict:
        """Canonical view: frozen session headers plus the token, if set."""
        if self._token:
            return {**self._headers, "Authorization": self._token}
        return dict(self._headers)

    def _sync_headers(self) -> None:
        """Push the frozen header set onto the shared session.

        Authorization is deliberately NOT here. curl_cffi's ``Headers`` turns
        a ``None`` value into the literal string ``"None"``, so a session-level
        Authorization could not be suppressed for unauthenticated calls -- and
        an unauthenticated call (e.g. GET /gateway during boot) would then
        authenticate as a bogus token. It is injected per request instead.
        """
        session_headers = self.session.headers
        session_headers.update(self._headers)
        # Drop keys removed from the canonical set (e.g. X-Fingerprint after
        # login); update() only adds/replaces. curl_cffi lowercases keys, so
        # this comparison must be case-insensitive or it deletes everything.
        canonical = {key.lower() for key in self._headers}
        for stale in [k for k in session_headers if k.lower() not in canonical]:
            del session_headers[stale]

    def set_profile(self, profile: BrowserProfile) -> None:
        """Rebuild headers around a new profile, keeping the same session.

        Note: ``impersonate`` is fixed at session construction, so swapping
        presets after boot will not change the TLS/HTTP2 fingerprint. Boot
        keeps one preset, so this is only used to refresh versions.
        """
        self.profile = profile
        self._headers = base_headers(profile, self.eph)
        self._sync_headers()

    def refresh_super_properties(self) -> None:
        """Re-encode after heartbeat-session rotation. Only mid-run refresh."""
        self._headers["X-Super-Properties"] = encode_super_properties(
            super_properties_dict(self.profile, self.eph)
        )
        self._sync_headers()

    def set_token(self, token: str) -> None:
        self._token = token
        self._headers.pop("X-Fingerprint", None)
        self._sync_headers()

    # -- auth circuit breaker --------------------------------------------
    @property
    def auth_dead(self) -> bool:
        """True once a 401 proved the token no longer works."""
        return self._auth_dead

    def on_auth_dead(self, callback) -> None:
        """Register ``callback(exc)`` to run when the token is rejected.

        The Client uses this to stop the gateway: a dead token has nothing
        to resume, so reconnecting would only burn the Identify budget.
        """
        self._on_auth_dead.append(callback)

    def trip_auth(self, exc: HTTPError) -> None:
        """Mark the token dead and notify watchers. Idempotent.

        After this, authenticated requests fail fast with no network call,
        so a rejected token is never hammered.
        """
        if self._auth_dead:
            return
        self._auth_dead = True
        self._auth_error = exc
        for callback in list(self._on_auth_dead):
            try:
                callback(exc)
            except Exception:  # a bad watcher must not mask the 401
                log.debug("auth-dead callback raised", exc_info=True)

    # -- rate limit routing ----------------------------------------------
    def _bucket_key(self, method: str, path: str) -> str:
        """Bootstrap route key: major-parameter scoping.

        Only used until Discord tells us the real bucket id. It over-locks
        (two routes sharing a bucket are treated as distinct) and under-locks
        nothing once ``_observe_limits`` has run, because the learning step
        replaces it with Discord's own hash.
        """
        parts = path.strip("/").split("/")
        scope = parts[0] if len(parts) < 2 else f"{parts[0]}/{parts[1]}"
        return f"{method.upper()}:{scope}"

    def _resolve_bucket(self, method: str, path: str) -> tuple[RateLimitBucket, str]:
        route = self._bucket_key(method, path)
        bucket_id = self._route_buckets.get(route)
        if bucket_id and self._bucket_scopes.get(bucket_id) == "user":
            return self._user_bucket, route
        key = f"b:{bucket_id}" if bucket_id else route
        return self._buckets.setdefault(key, RateLimitBucket()), route

    def _observe_limits(self, headers: Any, route: str, bucket: RateLimitBucket) -> None:
        """Learn the real bucket id/scope and arm the right reset."""
        try:
            remaining = headers.get("X-RateLimit-Remaining")
            reset_after = headers.get("X-RateLimit-Reset-After")
            bucket_id = headers.get("X-RateLimit-Bucket")
            scope = (headers.get("X-RateLimit-Scope") or "").strip().lower()
            precision = (headers.get("X-RateLimit-Precision") or "").strip().lower()
        except AttributeError:
            return

        if bucket_id:
            known = self._route_buckets.get(route)
            self._route_buckets[route] = bucket_id
            if scope:
                self._bucket_scopes[bucket_id] = scope
            if known != bucket_id:
                # This request was throttled against the heuristic bucket (or
                # an id we have since been told is wrong). Carry any armed
                # reset over to the real bucket, or the *next* request for
                # this route would be sent with no wait at all.
                target = self._buckets.setdefault(f"b:{bucket_id}", RateLimitBucket())
                if bucket.reset_at > target.reset_at:
                    target.reset_at = bucket.reset_at
                # Preserve the old bucket's reset_at before removing it
                old_reset_at = bucket.reset_at
                if known is None:
                    self._buckets.pop(route, None)
                bucket = target
                # Copy the old bucket's reset_at to the new bucket as well
                if old_reset_at > bucket.reset_at:
                    bucket.reset_at = old_reset_at
        if scope == "user":
            # The reset belongs to the account-wide bucket, not ours.
            bucket = self._user_bucket

        if reset_after is None or remaining != "0":
            return
        try:
            seconds = float(reset_after)
        except (TypeError, ValueError):
            return
        if precision == "millisecond":
            seconds /= 1000.0

        deadline = time.monotonic() + seconds
        if scope == "global":
            self._global_until = max(self._global_until, deadline)
        else:
            # Never shorten an existing reset: several routes can share one
            # bucket, and a short window from one must not unblock others.
            bucket.reset_at = (
                max(bucket.reset_at, deadline) if bucket.reset_at else deadline
            )

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else f"{self.api_base}{path}"

    # -- requests --------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict | None = None,
        params: dict | list[tuple] | None = None,
        context: str | None = None,
        auth: bool = True,
        reason: str | None = None,
    ) -> dict | list:
        return await self._send(
            method,
            path,
            json_data=json_data,
            params=params,
            context=context,
            auth=auth,
            reason=reason,
        )

    async def upload(
        self,
        path: str,
        *,
        fields: dict | None = None,
        files: list[dict] | None = None,
        file_field: str = "file",
        filename: str = "upload.png",
        data: bytes | None = None,
        local_path: str | bytes | None = None,
        content_type: str = "application/octet-stream",
        method: str = "POST",
        reason: str | None = None,
    ) -> dict | list:
        """multipart/form-data upload (attachments, avatars, banners, ...).

        curl_cffi does not support a ``files=`` kwarg; multipart bodies are
        built with ``CurlMime`` and handed to the session as ``multipart=``.

        Two ways to attach bytes: the single-file ``data``/``local_path``
        pair kept for callers that predate multi-file sends, or ``files`` -- a
        list of dicts with ``filename`` plus ``data`` (or ``local_path``), an
        optional ``content_type``, and an optional ``field`` name (defaults to
        ``files[N]``, which is what Discord's message upload wants).
        """
        from curl_cffi import CurlMime

        parts: list[dict] = list(files or [])
        if data is not None or local_path is not None:
            parts.append(
                {
                    "filename": filename,
                    "content_type": content_type,
                    "data": data,
                    "local_path": local_path,
                    "field": file_field,
                }
            )

        mime = CurlMime()
        try:
            for name, value in (fields or {}).items():
                mime.addpart(
                    name=name,
                    data=value if isinstance(value, bytes) else str(value).encode(),
                )
            for index, part in enumerate(parts):
                mime.addpart(
                    name=part.get("field") or f"files[{index}]",
                    filename=part.get("filename") or f"file{index}",
                    content_type=part.get("content_type")
                    or "application/octet-stream",
                    data=part.get("data"),
                    local_path=part.get("local_path"),
                )
            result = await self._send(
                method,
                path,
                multipart=mime,
                reason=reason,
            )
            mime.close()
            return result
        except Exception:
            mime.close()
            raise

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_data: dict | None = None,
        params: dict | list[tuple] | None = None,
        context: str | None = None,
        auth: bool = True,
        reason: str | None = None,
        multipart: Any = None,
    ) -> dict | list:
        method = method.upper()
        if auth and self._token and self._auth_dead:
            # The token was already rejected once; don't touch the network.
            assert self._auth_error is not None
            raise self._auth_error
        # Only genuinely per-request headers go here; the frozen set already
        # lives on the session.
        headers: dict[str, str] = {}
        if auth and self._token:
            headers["Authorization"] = self._token
        if context and context in CONTEXT_PROPERTIES:
            headers["X-Context-Properties"] = encode_context(
                CONTEXT_PROPERTIES[context]
            )
        if reason:
            headers["X-Audit-Log-Reason"] = quote(reason, safe="")

        bucket, route = self._resolve_bucket(method, path)
        idempotent = method in IDEMPOTENT_METHODS
        url = self._url(path)

        r = None
        for attempt in range(MAX_429_RETRIES + 1):
            await self._await_global()
            async with bucket.lock:
                await self._await_bucket(bucket)
                # A global 429 may have landed while we waited on the lock.
                await self._await_global()
                r = await self.session.request(
                    method,
                    url,
                    headers=headers or None,
                    json=json_data,
                    params=params,
                    multipart=multipart,
                )
                self._observe_limits(r.headers, route, bucket)
                log.debug("%s %s -> %s", method, path, r.status_code)

                if r.status_code == 429:
                    exc = self._build_error(r, method, path)
                    retry_after = getattr(exc, "retry_after", None) or 1.0
                    if getattr(exc, "is_global", False):
                        self._global_until = time.monotonic() + retry_after
                    else:
                        bucket.reset_at = time.monotonic() + retry_after
                    if attempt >= MAX_429_RETRIES:
                        raise exc
                    await asyncio.sleep(retry_after + random.uniform(0, 0.5))
                    continue

                if _is_retryable_5xx(r.status_code):
                    if idempotent and attempt < MAX_5XX_RETRIES:
                        await asyncio.sleep(1 + attempt)
                        continue
                    raise self._build_error(r, method, path)

                if r.status_code == 401 and auth and self._token:
                    # The token is dead: trip the breaker so the Client stops
                    # the gateway, and re-raise for this caller.
                    exc = self._build_error(r, method, path)
                    self.trip_auth(exc)
                    raise exc

                if r.status_code >= 400:
                    raise self._build_error(r, method, path)

                if r.status_code == 204 or not r.text:
                    return {}
                return json.loads(r.text)

        raise self._build_error(r, method, path)

    async def _await_global(self) -> None:
        now = time.monotonic()
        if now < self._global_until:
            await asyncio.sleep(self._global_until - now)

    async def _await_bucket(self, bucket: RateLimitBucket) -> None:
        now = time.monotonic()
        if now < bucket.reset_at:
            await asyncio.sleep(bucket.reset_at - now)

    @staticmethod
    def _build_error(r, method: str, path: str) -> HTTPError:
        """Map an error response onto the typed hierarchy."""
        status = r.status_code
        text = r.text if isinstance(r.text, str) else ""
        body: object = None
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = text[:200] or None
        err = body if isinstance(body, dict) else {}
        message = err.get("message") if isinstance(err.get("message"), str) else None
        if message is None:
            message = text[:200] or f"HTTP {status}"
        raw_retry = err.get("retry_after")
        retry_after = float(raw_retry) if isinstance(raw_retry, int | float) else None
        fields = err.get("errors") if isinstance(err.get("errors"), dict) else None
        code = err.get("code") if isinstance(err.get("code"), int) else None

        kwargs = {
            "status": status,
            "code": code,
            "errors": fields,
            "retry_after": retry_after,
            "method": method,
            "path": path,
            "body": body,
        }
        if status == 429:
            return RateLimited(message, is_global=bool(err.get("global")), **kwargs)
        if status == 401:
            return AuthenticationHTTPError(message, **kwargs)
        if status == 403:
            return Forbidden(message, **kwargs)
        if status == 404:
            return NotFound(message, **kwargs)
        if status >= 500:
            return ServerError(message, **kwargs)
        return HTTPError(message, **kwargs)

    async def close(self) -> None:
        await self.session.close()
