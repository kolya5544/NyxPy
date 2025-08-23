from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, List


class NyxError(Exception):
    pass


class NyxHTTPError(NyxError):
    def __init__(self, status_code: int, message: str, *, payload: Optional[Mapping[str, Any]] = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


class NyxAuthError(NyxHTTPError):
    pass


@dataclass(slots=True)
class AuthResult:
    access_token: Optional[str]
    refresh_token: Optional[str]
    token_type: str = "Bearer"
    raw: Dict[str, Any] = None


class EventType(str, Enum):
    NEW_MESSAGE = "NEW_MESSAGE"
    TYPING = "TYPING"
    PRESENCE_UPDATE = "PRESENCE_UPDATE"
    CONNECT = "CONNECT"
    DISCONNECT = "DISCONNECT"
    RAW = "RAW"
    FRIEND_REQUEST = "FRIEND_REQUEST"


@dataclass(slots=True)
class Event:
    type: EventType
    data: Dict[str, Any]
    channel: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


# HTTP models
@dataclass(slots=True)
class Server:
    id: int
    name: str
    owner_id: int
    avatar_url: str


@dataclass(slots=True)
class UserInfo:
    id: int
    email: str
    username: str
    avatar_url: str

@dataclass(slots=True)
class Member:
    id: int
    email: str
    username: str
    avatar_url: str
    roles: List[int]

@dataclass(slots=True)
class FriendUser:
    id: int
    email: str
    username: str
    avatar_url: str

@dataclass(slots=True)
class FriendRequest:
    id: int
    sender_id: int
    receiver_id: int
    status: str
    sender: Optional[FriendUser] = None

@dataclass(slots=True)
class Channel:
    id: int
    name: str
    server_id: int
    type: str
    position: int
    parent_channel_id: Optional[int]
    roles_overrides: List[Any]
    users_overrides: List[Any]

@dataclass(slots=True)
class Role:
    id: int
    name: str
    color: str
    server_id: int
    position: int
    display_role_members: bool
    permissions: List[str]