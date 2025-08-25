from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from .http import NyxHTTPClient as _Base
from .types import Server, UserInfo, Member, Event, FriendRequest, Role, Channel, FriendUser, NyxAuthError, NyxError, \
    Participant
from .http_parsers import (
    parse_user_info, parse_member, parse_friend_request, parse_role, parse_channel, parse_friend_user,
    parse_participant,
)

from .rtc import NyxRTCSession

class NyxHTTPClient(_Base):
    """High-level HTTP API on top of the core transport/auth client."""

    # ---- messages ----
    async def send_message(
        self,
        channel_name: str,
        content: str,
        *,
        reply_id: Optional[str] = None,
        reply: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"channel_name": channel_name, "content": content}
        if reply is not None:
            payload["reply"] = dict(reply)
        elif reply_id:
            payload["reply"] = {"id": str(reply_id)}
        resp = await self.request("POST", "/i/messages/send", json_body=payload)
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {}

    async def reply(self, ev: Event, content: str) -> Dict[str, Any]:
        channel_name: Optional[str] = None
        if isinstance(ev.data, dict):
            cn = ev.data.get("channel_name")
            if isinstance(cn, str) and cn:
                channel_name = cn
        if not channel_name and isinstance(ev.channel, str) and ev.channel:
            channel_name = ev.channel
        if not channel_name:
            raise RuntimeError("Cannot determine channel_name from event to reply().")
        reply_id: Optional[str] = None
        if isinstance(ev.data, dict):
            mid = ev.data.get("id")
            if isinstance(mid, str) and mid:
                reply_id = mid
        if not reply_id:
            raise RuntimeError("Event has no message id to reply to.")
        return await self.send_message(channel_name, content, reply_id=reply_id)

    # ---- friends ----
    async def accept_friend_request(self, request_id: int) -> Dict[str, Any]:
        resp = await self.request("POST", f"/i/friends/accept/{int(request_id)}")
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {}

    async def get_received_friend_requests(self) -> list[FriendRequest]:
        resp = await self.request("GET", "/i/friends/received-requests")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []
        out: list[FriendRequest] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(parse_friend_request(item))
        return out

    # ---- friends (list) ----
    async def get_friends(self) -> list[FriendUser]:
        """GET /i/friends/ — list all accepted friends."""
        resp = await self.request("GET", "/i/friends/")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []
        out: list[FriendUser] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(parse_friend_user(item))
        return out

    async def ensure_friends_cached(self) -> Dict[int, FriendUser]:
        """Return friends as a dict {user_id: FriendUser}, fetching once if needed."""
        cache = getattr(self, "_friends_by_id", None)
        if not cache:
            friends = await self.get_friends()
            self._friends_by_id = {f.id: f for f in friends}
        return self._friends_by_id  # type: ignore[attr-defined]

    async def refresh_friends_cache(self) -> Dict[int, FriendUser]:
        """Force-refresh friends cache."""
        friends = await self.get_friends()
        self._friends_by_id = {f.id: f for f in friends}
        return self._friends_by_id  # type: ignore[attr-defined]

    # ---- servers/users ----
    async def get_servers(self) -> List[Server]:
        resp = await self.request("GET", "/i/servers/")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []
        servers: List[Server] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                servers.append(
                    Server(
                        id=int(item.get("id")),
                        name=str(item.get("name", "")),
                        owner_id=int(item.get("owner_id")),
                        avatar_url=str(item.get("avatar_url", "")),
                    )
                )
        self._server_list = servers
        return servers

    async def get_user_info(self, *, cached: bool = True, force_refresh: bool = False) -> UserInfo:
        if (cached and not force_refresh) and self.current_user is not None:
            return self.current_user
        resp = await self.request("GET", "/i/user/me")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {}
        user = parse_user_info(data)
        self.current_user = user
        return user

    async def get_server_members(self, server_id: int) -> list[Member]:
        resp = await self.request("GET", f"/i/servers/{server_id}/members")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []
        members: list[Member] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                m = parse_member(item)
                members.append(m)
                self._member_by_user[m.id] = m
        self._server_members[server_id] = {m.id for m in members}
        return members

    async def ensure_server_members_cached(self, server_id: int) -> list[Member]:
        if server_id not in self._server_members:
            return await self.get_server_members(server_id)
        ids = self._server_members[server_id]
        return [self._member_by_user[uid] for uid in ids if uid in self._member_by_user]

    async def get_member(self, server_id: int, user_id: int, *, fetch_if_missing: bool = True) -> Optional[Member]:
        uid = int(user_id)
        if server_id in self._server_members and uid in self._server_members[server_id]:
            return self._member_by_user.get(uid)
        if not fetch_if_missing:
            return self._member_by_user.get(uid)
        await self.get_server_members(server_id)
        return self._member_by_user.get(uid)

    # Channels
    async def get_channels(self, server_id: int) -> list[Channel]:
        """GET /i/channels/?server_id=... ; for voice channels, attach participants."""
        try:
            resp = await self.request("GET", "/i/channels/", params={"server_id": int(server_id)})
        except NyxAuthError:
            return []  # probably requesting channels of DMs?
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []

        if not hasattr(self, "_server_by_channel"):
            self._server_by_channel = {}  # type: ignore[attr-defined]

        out: list[Channel] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                ch = parse_channel(item)
                out.append(ch)

                # cache channel -> server mapping
                try:
                    self._server_by_channel[int(ch.id)] = int(ch.server_id)  # type: ignore[attr-defined]
                except Exception:
                    pass

                # if it's a voice channel, fetch and attach typed participants
                if isinstance(ch.type, str) and ch.type.lower() == "voice":
                    try:
                        ch.participants = await self.get_participants(int(ch.id))
                    except Exception:
                        self._logger.debug("Failed to fetch participants for channel %s", ch.id, exc_info=True)
        return out

    async def get_participants(self, channel_id: int) -> list[Participant]:
        """
        GET /i/channels/{channel_id}/session?server_id=<sid>
        `server_id` is resolved from cache; if unknown, we scan servers+channels once.
        Returns a typed list[Participant].
        """
        cid = int(channel_id)

        # resolve server_id from cache (channel -> server)
        sid: Optional[int] = None
        cache = getattr(self, "_server_by_channel", None)
        if isinstance(cache, dict):
            sid = cache.get(cid)

        # if missing, build/refresh cache by scanning servers
        if sid is None:
            servers = self._server_list or await self.get_servers()
            for s in servers:
                try:
                    await self.get_channels(int(s.id))  # refresh cache for each server
                except Exception:
                    continue
                cache = getattr(self, "_server_by_channel", None)
                if isinstance(cache, dict) and cid in cache:
                    sid = int(cache[cid])
                    break

        if sid is None:
            raise NyxError(f"Cannot resolve server_id for channel_id={cid}")

        # fetch session participants
        resp = await self.request("GET", f"/i/channels/{cid}/session", params={"server_id": int(sid)})
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {}

        raw_list = data.get("participants", [])
        participants: list[Participant] = []
        if isinstance(raw_list, list):
            for p in raw_list:
                if not isinstance(p, dict):
                    continue
                participants.append(parse_participant(p))
        return participants

    # Roles (all in server)
    async def get_roles(self, server_id: int) -> list[Role]:
        """GET /i/roles/?server_id=..."""
        resp = await self.request("GET", "/i/roles/", params={"server_id": int(server_id)})
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []
        out: list[Role] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(parse_role(item))
        return out

    # Roles for current user in server
    async def get_my_roles(self, server_id: int) -> list[Role]:
        """GET /i/roles/user?server_id=..."""
        resp = await self.request("GET", "/i/roles/user", params={"server_id": int(server_id)})
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = []
        out: list[Role] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(parse_role(item))
        return out

    async def voice_join(
            self,
            channel_id: int,
            *,
            base_ws_url: str = "wss://livekit.nyx-app.ru",
            auto_subscribe: bool = False,
            wait_connected: bool = False,
            connect_timeout: float = 10.0,
            **join_opts,
    ) -> "NyxRTCSession":
        """Join a Nyx voice channel (LiveKit room) and return an RTC session.

        Args:
            channel_id: Nyx voice-channel ID to join.
            base_ws_url: LiveKit signaling URL base.
            auto_subscribe: whether to auto-subscribe to remote tracks.
            wait_connected: if True, wait for RTC 'connected' event before returning.
            connect_timeout: seconds to wait when wait_connected=True.
            **join_opts: passed through to /i/channels/{id}/token (e.g., audio_muted, ptt_enabled).
        """
        session = NyxRTCSession(
            http_client=self,
            channel_id=int(channel_id),
            base_ws_url=base_ws_url,
            auto_subscribe=auto_subscribe,
            logger=self._logger,
        )
        await session.connect(wait_connected=wait_connected, timeout=connect_timeout, **join_opts)
        return session