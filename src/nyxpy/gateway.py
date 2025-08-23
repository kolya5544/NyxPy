from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import ssl
from collections import defaultdict, Counter
from dataclasses import asdict
from typing import Any, Dict, Mapping, Optional, Set, Iterable

import websockets
from websockets import exceptions as wse

from .http_api import NyxHTTPClient
from .types import Event, EventType, NyxAuthError, NyxError, Server
from .utils import get_logger


class _Dispatcher:
    def __init__(self) -> None:
        self._handlers: Dict[EventType, list] = defaultdict(list)

    def register(self, event_type: EventType, func):
        if inspect.iscoroutinefunction(func):
            coro = func
        else:
            async def coro(ev: Event):  # type: ignore[misc]
                return func(ev)
        self._handlers[event_type].append(coro)
        return func

    async def dispatch(self, ev: Event) -> None:
        for handler in list(self._handlers.get(ev.type, [])):
            try:
                await handler(ev)
            except Exception as e:  # pragma: no cover
                get_logger().exception("Handler error on %s: %r", ev.type, e)
        if ev.type is not EventType.RAW:
            for handler in list(self._handlers.get(EventType.RAW, [])):
                try:
                    await handler(ev)
                except Exception as e:  # pragma: no cover
                    get_logger().exception("Handler error on RAW: %r", e)


_dispatcher = _Dispatcher()


class _NoxEventDecorator:
    NEW_MESSAGE = EventType.NEW_MESSAGE
    TYPING = EventType.TYPING
    PRESENCE_UPDATE = EventType.PRESENCE_UPDATE
    CONNECT = EventType.CONNECT
    DISCONNECT = EventType.DISCONNECT
    RAW = EventType.RAW
    FRIEND_REQUEST = EventType.FRIEND_REQUEST

    def __call__(self, *, type: EventType):
        def deco(func):
            return _dispatcher.register(type, func)
        return deco


nyx_event = _NoxEventDecorator()


class NyxClient(NyxHTTPClient):
    """HTTP + WebSocket client with decorator-based events."""

    def __init__(self):
        super().__init__()
        self._ws_ready = asyncio.Event()

    # ---- WebSocket lifecycle ----
    async def connect_ws(
        self,
        token: Optional[str] = None,
        *,
        url: str = "wss://nyx-app.ru/api/v2/i/ws",
        reconnect: bool = True,
        ping_interval: float = 25.0,
        ssl_context: ssl.SSLContext | bool | None = ssl.SSLContext(),
        subprotocols: Optional[Iterable[str]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        client_name: str = "js",
    ) -> None:
        self._ws_ready.clear()
        self._ws_token = token or self._access_token
        if not self._ws_token:
            raise NyxAuthError(401, "No token for WS. Call login() first or pass token=.")
        self._ws_url = f"{url}?token={self._ws_token}"
        self._ws_reconnect = reconnect
        self._subscriptions: Set[str] = set()
        self._ws_ssl = ssl_context
        self._ws_subprotocols = list(subprotocols) if subprotocols else None
        self._ws_headers = dict(extra_headers) if extra_headers else None
        self._ws_client_name = client_name
        self._ws_task = asyncio.create_task(self._ws_loop(self._ws_url, ping_interval))
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=None)
        except Exception:
            # if the loop already crashed, raise its exception
            if self._ws_task.done():
                self._ws_task.result()
            raise

    async def close_ws(self) -> None:
        task = getattr(self, "_ws_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        ws = getattr(self, "_ws", None)
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _ws_loop(self, url: str, ping_interval: float) -> None:
        backoff = 1.0
        while True:
            try:
                self._logger.info("WS connecting: %s", url)
                async with websockets.connect(
                        url,
                        ssl=self._ws_ssl,
                        ping_interval=ping_interval,
                        ping_timeout=20,
                        close_timeout=5,
                        max_size=8 * 1024 * 1024,
                        open_timeout=15,
                        subprotocols=self._ws_subprotocols or ["json"],
                        additional_headers=self._ws_headers,
                ) as ws:
                    self._ws = ws
                    self._ws_id = 0
                    self._ka_task = asyncio.create_task(self._keepalive(ws, interval=10))
                    # send CONNECT like the browser does (include name; many servers also want token here)
                    await self._ws_send({"connect": {"name": self._ws_client_name}})
                    # get user data
                    userData = await self.get_user_info()
                    id = userData.id
                    await self._ws_send({"subscribe":{"channel":f"user:{id}","data":{"user_id":id}}})
                    self._ws_ready.set()
                    await _dispatcher.dispatch(Event(EventType.CONNECT, data={}, raw={"url": url}))
                    async for message in ws:
                        obj = json.loads(message)
                        if message == "{}":
                            await self._ws_send_raw("{}")
                            continue
                        ev = self._map_ws_to_event(obj)
                        if ev.type == EventType.NEW_MESSAGE and isinstance(ev.channel, str) and ev.channel.startswith(
                                "server:"):
                            try:
                                # channel format: server:{server_id}:{...}
                                server_id = int(ev.channel.split(":", 2)[1])

                                # ---- attach sender's member (unchanged) ----
                                sender_id = None
                                if isinstance(ev.data, dict):
                                    sid = ev.data.get("sender_id") or ev.data.get("user_id") or ev.data.get("author_id")
                                    if isinstance(sid, str):
                                        sender_id = int(sid) if sid.isdigit() else None
                                    elif isinstance(sid, (int, float)):
                                        sender_id = int(sid)
                                if sender_id is not None:
                                    member = await self.get_member(server_id, sender_id, fetch_if_missing=True)
                                    if member:
                                        ev.data["member"] = asdict(member)

                                # ---- NEW: attach replied-to member, if present ----
                                # looks inside ev.data["data"]["reply"]["sender_id"]
                                reply_sender_id = None
                                inner = ev.data.get("data") if isinstance(ev.data, dict) else None
                                if isinstance(inner, dict):
                                    rep = inner.get("reply")
                                    if isinstance(rep, dict):
                                        rsid = rep.get("sender_id")
                                        if isinstance(rsid, str):
                                            reply_sender_id = int(rsid) if rsid.isdigit() else None
                                        elif isinstance(rsid, (int, float)):
                                            reply_sender_id = int(rsid)

                                if reply_sender_id:
                                    rmember = await self.get_member(server_id, reply_sender_id, fetch_if_missing=True)
                                    if rmember:
                                        # prefer to attach alongside the nested reply payload
                                        if isinstance(inner, dict):
                                            inner["reply_member"] = asdict(rmember)
                                        elif isinstance(ev.data, dict):
                                            ev.data["reply_member"] = asdict(rmember)
                            except Exception as e:  # pragma: no cover
                                self._logger.debug("Failed to enrich NEW_MESSAGE for %s: %r", ev.channel, e)

                        await _dispatcher.dispatch(ev)
            except asyncio.CancelledError:
                self._logger.info("WS loop cancelled")
                raise
            except wse.InvalidStatusCode as e:
                self._logger.warning("WS handshake failed with HTTP %s", getattr(e, "status_code", "?"))
            except (wse.ConnectionClosedOK, wse.ConnectionClosedError) as e:
                self._logger.warning("WS closed: code=%s reason=%r", getattr(e, "code", "?"), getattr(e, "reason", ""))
            except ssl.SSLError as e:
                self._logger.warning("WS SSL error: %r", e)
            except Exception as e:  # pragma: no cover
                self._logger.warning("WS error: %r", e)
            finally:
                # stop keepalive if running
                ka = getattr(self, "_ka_task", None)
                if ka:
                    ka.cancel()
                    with contextlib.suppress(Exception):
                        await ka
                await _dispatcher.dispatch(Event(EventType.DISCONNECT, data={}, raw={}))

            if not getattr(self, "_ws_reconnect", True):
                break
            await asyncio.sleep(min(backoff, 30))
            backoff *= 2

    async def _keepalive(self, ws, interval: float) -> None:
        """Send WS control pings periodically (defensive against idle timeouts)."""
        try:
            while True:
                await asyncio.sleep(interval)
                # iterate over all servers, send presence
                for s in self._server_list:
                    await self._ws_send({"presence": {"channel":f"server:{s.id}:general"}})
                await ws.ping()
                #await ws.send("{}")
        except asyncio.CancelledError:
            return

    # ---- Mapping & send helpers ----
    def _map_ws_to_event(self, obj: Dict[str, Any]) -> Event:
        # push
        if isinstance(obj, dict) and "push" in obj:
            push = obj["push"]
            pub = push.get("pub", {}) if isinstance(push, dict) else {}
            chan = pub.get("channel") or push.get("channel")
            data = pub.get("data", {})
            raw_type = data.get("type") if isinstance(data, dict) else None
            payload = None
            if isinstance(data, dict):
                payload = data.get("data")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        pass
            if raw_type == "message":
                return Event(EventType.NEW_MESSAGE, data=payload or {}, channel=chan, raw=obj)
            if raw_type == "typing":
                return Event(EventType.TYPING, data=payload or {}, channel=chan, raw=obj)
            if raw_type == "friend_request":
                return Event(EventType.FRIEND_REQUEST, data=payload or {}, channel=chan, raw=obj)
            return Event(EventType.RAW, data=data if isinstance(data, dict) else {"data": data}, channel=chan, raw=obj)

        # presence
        if isinstance(obj, dict) and "presence" in obj:
            pres = obj["presence"]
            chan = pres.get("channel") if isinstance(pres, dict) else None
            details = pres.get("presence") if isinstance(pres, dict) else pres
            if not isinstance(details, dict):
                details = {"presence": details}
            return Event(EventType.PRESENCE_UPDATE, data=details, channel=chan, raw=obj)

        # subscribe/connect acks fall back to RAW (useful for debugging)
        if isinstance(obj, dict) and ("connect" in obj or "subscribe" in obj):
            return Event(EventType.RAW, data=obj, raw=obj)

        return Event(EventType.RAW, data=obj if isinstance(obj, dict) else {"raw": obj}, raw=obj)

    async def _ws_send(self, payload: Dict[str, Any]) -> None:
        ws = getattr(self, "_ws", None)
        if ws is None:
            raise NyxError("WS not connected; call connect_ws() first")
        self._ws_id = getattr(self, "_ws_id", 0) + 1
        frame = dict(payload)
        frame["id"] = self._ws_id
        jsonstr = json.dumps(frame, separators=(",", ":"))
        #logging.debug(f"[SENT] {jsonstr}")
        await ws.send(jsonstr)


    async def _ws_send_raw(self, text: str) -> None:
        """Send a raw text frame without injecting an 'id' field."""
        ws = getattr(self, "_ws", None)
        if ws is None:
            raise NyxError("WS not connected; call connect_ws() first")
        await ws.send(text)

    # ---- Presence (snapshot) ----
    async def ws_presence(self, channel: str) -> None:
        """Request presence snapshot for a channel."""
        userInfo = await self.get_user_info()
        userId = userInfo.id
        await self._ws_send({"subscribe": {"channel": channel, "data":{"user_id":userId}}})
        self._subscriptions.add(channel)

    # ---- Subscriptions ----
    async def ws_subscribe_channel(self, channel: str, *, data: Optional[Mapping[str, Any]] = None) -> None:
        """Generic subscribe helper (useful for 'user:<id>' or any channel requiring extra data)."""
        payload: Dict[str, Any] = {"subscribe": {"channel": channel}}
        if data:
            payload["subscribe"]["data"] = dict(data)
        await self._ws_send(payload)
        self._subscriptions.add(channel)

    async def ws_subscribe_user(self, user_id: int) -> None:
        """Subscribe to personal user channel per your flow: {"subscribe":{"channel":"user:<id>","data":{"user_id":<id>}}}"""
        await self.ws_subscribe_channel(f"user:{user_id}", data={"user_id": user_id})

    async def ws_subscribe_server(self, server_id: int, *, channel_name: str = "general") -> None:
        """Subscribe to a specific server's channel. Many deployments accept presence as the join mechanism."""
        channel = f"server:{server_id}:{channel_name}"
        # If your backend prefers explicit subscribe for servers too, switch to ws_subscribe_channel(channel)
        await self.ws_presence(channel)

    # replace the method with this version
    async def ws_subscribe_all(self, *, channel_name: str = "general") -> None:
        """Fetch servers via HTTP and subscribe to each one's channel,
        excluding any servers whose IDs are non-unique in the response.
        """
        servers: list[Server] = await self.get_servers()
        counts = Counter(s.id for s in servers)

        # keep only servers that appear exactly once; drop all duplicates entirely
        unique_servers = [s for s in servers if counts[s.id] == 1]

        # (optional) debug log duplicate IDs we skipped
        dup_ids = [sid for sid, c in counts.items() if c > 1]
        if dup_ids:
            self._logger.debug("Skipping duplicate server IDs: %s", dup_ids)

        for s in unique_servers:
            await self.ws_subscribe_server(s.id, channel_name=channel_name)
            await self._ws_send({"presence": {"channel": f"server:{s.id}:general"}})