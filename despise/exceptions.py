"""Typed exceptions.

Why not ``RuntimeError`` for everything: selfbot code branches on Discord
error *codes* far more often than on status text. Every HTTP failure carries
its status, Discord JSON ``code``, field-level ``errors``, and ``retry_after``
so callers can react instead of string-matching ``"401"``.

    try:
        await client.rest.history(cid, limit=100)
    except NotFound:
        ...
    except Forbidden as e:
        if e.code == 50013:  # missing permissions
            ...
"""

from __future__ import annotations


class DespiseError(Exception):
    """Base for everything this library raises."""


class NotConnected(DespiseError):
    """A gateway operation was attempted while the socket was down."""


class GatewayTimeout(DespiseError):
    """The handshake did not complete inside the allotted window."""


class GatewayClosed(DespiseError):
    """The gateway socket closed and will not be resumed.

    ``code`` is the WebSocket close code (``None`` when the socket died
    without a close frame). ``reason`` is Discord's close reason string.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        detail = message or reason or "gateway closed"
        if code is not None:
            detail = f"{detail} (close code {code})"
        super().__init__(detail)


class AuthenticationFailed(GatewayClosed):
    """Close code 4004 -- the token was rejected. Never retried."""


class InvalidIntents(GatewayClosed):
    """Close code 4014 -- the supplied ``intents`` are unacceptable."""


class TooManySubscriptions(DespiseError):
    """Refused a guild subscription because READY's ``max_concurrency`` is hit.

    The server answers an over-subscribed client with close code 4013, which
    costs a reconnect, so this is caught locally instead.
    """

    def __init__(
        self,
        message: str = "",
        *,
        limit: int | None = None,
        guild_id: str | None = None,
    ) -> None:
        self.limit = limit
        self.guild_id = guild_id
        super().__init__(message or f"subscription limit ({limit}) reached")


class HTTPError(DespiseError):
    """A REST call returned >= 400.

    Attributes mirror Discord's error body: ``status`` (HTTP), ``code``
    (Discord's own numeric error code), ``errors`` (per-field validation
    details), ``retry_after`` (when present), plus the request coordinates.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        code: int | None = None,
        errors: dict | None = None,
        retry_after: float | None = None,
        method: str | None = None,
        path: str | None = None,
        body: object = None,
    ) -> None:
        self.status = status
        self.code = code
        self.errors = errors
        self.retry_after = retry_after
        self.method = method
        self.path = path
        self.body = body
        self.message = message or f"HTTP {status}"
        super().__init__(self._format())

    def _format(self) -> str:
        where = f" {self.method} {self.path}" if self.method and self.path else ""
        parts = [f"{type(self).__name__}{where}: {self.message}"]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.retry_after is not None:
            parts.append(f"retry_after={self.retry_after}")
        return " | ".join(parts)


class RateLimited(HTTPError):
    """HTTP 429 that survived ``MAX_429_RETRIES``."""

    def __init__(self, message: str = "", *, is_global: bool = False, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.is_global = is_global


class AuthenticationHTTPError(HTTPError):
    """HTTP 401 -- token missing, malformed, or revoked."""


class Forbidden(HTTPError):
    """HTTP 403 -- missing permissions, or the action is selfbot-blocked."""


class NotFound(HTTPError):
    """HTTP 404."""


class ServerError(HTTPError):
    """HTTP 5xx that survived ``MAX_5XX_RETRIES``."""
