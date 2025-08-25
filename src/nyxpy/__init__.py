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
    UserInfo, FriendUser, FriendRequest, Channel, Role, Participant,  # NEW
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
    "Participant",
    "Channel",
    "Role",
    # clients
    "NyxHTTPClient",
    "NyxClient",
    # decorators
    "nyx_event",
]

# Аудио
from .rtc import NyxRTCSession, nyx_audio_event
__all__.extend(["NyxRTCSession", "nyx_audio_event"])

__version__ = "0.3.0"