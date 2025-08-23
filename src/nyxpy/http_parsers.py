from __future__ import annotations
from typing import Mapping, Any
from .types import UserInfo, Member, FriendUser, FriendRequest, Role, Channel


def parse_user_info(data: Mapping[str, Any]) -> UserInfo:
    return UserInfo(
        id=int(data.get("id", 0)),
        email=str(data.get("email", "")),
        username=str(data.get("username", "")),
        avatar_url=str(data.get("avatar_url", "")),
    )

def parse_member(data: Mapping[str, Any]) -> Member:
    roles = data.get("roles") or []
    if not isinstance(roles, list):
        roles = []
    return Member(
        id=int(data.get("id", 0)),
        email=str(data.get("email", "")),
        username=str(data.get("username", "")),
        avatar_url=str(data.get("avatar_url", "")),
        roles=[int(r) for r in roles if isinstance(r, (int, str))],
    )

def parse_friend_user(data: Mapping[str, Any]) -> FriendUser:
    return FriendUser(
        id=int(data.get("id", 0)),
        email=str(data.get("email", "")),
        username=str(data.get("username", "")),
        avatar_url=str(data.get("avatar_url", "")),
    )

def parse_friend_request(data: Mapping[str, Any]) -> FriendRequest:
    sender = data.get("sender")
    sender_obj = parse_friend_user(sender) if isinstance(sender, Mapping) else None
    return FriendRequest(
        id=int(data.get("id", 0)),
        sender_id=int(data.get("sender_id", 0)),
        receiver_id=int(data.get("receiver_id", 0)),
        status=str(data.get("status", "")),
        sender=sender_obj,
    )

def parse_channel(data: Mapping[str, Any]) -> Channel:
    parent = data.get("parent_channel_id")
    parent_id = int(parent) if isinstance(parent, (int, str)) and str(parent).isdigit() else None
    return Channel(
        id=int(data.get("id", 0)),
        name=str(data.get("name", "")),
        server_id=int(data.get("server_id", 0)),
        type=str(data.get("type", "")),
        position=int(data.get("position", 0)),
        parent_channel_id=parent_id,
        roles_overrides=list(data.get("roles_overrides", []) or []),
        users_overrides=list(data.get("users_overrides", []) or []),
    )

def parse_role(data: Mapping[str, Any]) -> Role:
    perms = data.get("permissions") or []
    if not isinstance(perms, list):
        perms = []
    return Role(
        id=int(data.get("id", 0)),
        name=str(data.get("name", "")),
        color=str(data.get("color", "")),
        server_id=int(data.get("server_id", 0)),
        position=int(data.get("position", 0)),
        display_role_members=bool(data.get("display_role_members", False)),
        permissions=[str(p) for p in perms],
    )
