"""despise: minimal discord.py-self.

curl_cffi-only, single shared HTTP session, restart-frozen profiles.
"""

from .cache import EntityCache
from .client import Client
from .exceptions import (
    AuthenticationFailed,
    AuthenticationHTTPError,
    DespiseError,
    Forbidden,
    GatewayClosed,
    GatewayTimeout,
    HTTPError,
    InvalidIntents,
    NotConnected,
    NotFound,
    RateLimited,
    ServerError,
    TooManySubscriptions,
)
from .gateway import GATEWAY_ENCODINGS
from .headers import PROFILE_NAMES, BrowserProfile, ClientKind, build_profile
from .interactions import (
    build_action_row,
    build_button,
    build_modal,
    build_select,
    defer,
)
from .models import (
    Attachment,
    Channel,
    Embed,
    Emoji,
    Guild,
    Invite,
    Member,
    Message,
    Reaction,
    Relationship,
    Role,
    User,
    VoiceState,
)
from .rest import TypingLoop, build_activity
from .session_cache import SessionCache, SessionState
from .tasks import Task
from .tasks import loop as task_loop

__version__ = "1.0.0"

__all__ = [
    "PROFILE_NAMES",
    "BrowserProfile",
    "build_profile",
    "Client",
    "User",
    "Message",
    "Channel",
    "Guild",
    "build_activity",
    "DespiseError",
    "HTTPError",
    "AuthenticationHTTPError",
    "Forbidden",
    "NotFound",
    "RateLimited",
    "ServerError",
    "GatewayClosed",
    "AuthenticationFailed",
    "InvalidIntents",
    "GatewayTimeout",
    "NotConnected",
    "TooManySubscriptions",
    "SessionCache",
    "SessionState",
    "GATEWAY_ENCODINGS",
    "ClientKind",
    "EntityCache",
    "Member",
    "Role",
    "Attachment",
    "Embed",
    "Reaction",
    "Relationship",
    "Emoji",
    "Invite",
    "VoiceState",
    "TypingLoop",
    "Task",
    "task_loop",
    "build_button",
    "build_action_row",
    "build_select",
    "build_modal",
    "defer",
    "__version__",
]
