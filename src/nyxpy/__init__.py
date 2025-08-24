"""NyxPy public API.

Async SDK for Nyx (bots / user-bots).
"""
from .types import (
    NyxError,
    NyxHTTPError,
    NyxAuthError,
    AuthResult,
    Event,
    EventType,
    Server,
    UserInfo, FriendUser, FriendRequest, Channel, Role,  # NEW
)
from .utils import configure_logging, get_logger
from .http_api import NyxHTTPClient
from .gateway import NyxClient, nyx_event  # NyxClient extends NyxHTTPClient with WS/events

__all__ = [
    # logging
    "configure_logging",
    "get_logger",
    # errors/types
    "NyxError",
    "NyxHTTPError",
    "NyxAuthError",
    "AuthResult",
    "Event",
    "EventType",
    "Server",
    "UserInfo",
    "FriendUser",
    "FriendRequest",
    "Channel",
    "Role",
    # clients
    "NyxHTTPClient",
    "NyxClient",
    # decorators
    "nyx_event",
]

__version__ = "0.2.0"