from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from .utils import get_logger, _safe_json
from .types import NyxError

try:
    # LiveKit Realtime SDK (install: pip install livekit)
    from livekit import rtc
except Exception as e:  # pragma: no cover
    rtc = None  # type: ignore[assignment]


# -------------------------
# Audio event dispatcher
# -------------------------

class _AudioDispatcher:
    def __init__(self) -> None:
        self._handlers: Dict[str, list[Callable[..., Awaitable[None]]]] = {}

    def register(self, type: str, func):
        # accept sync or async callback; wrap sync into coroutine
        if asyncio.iscoroutinefunction(func):
            coro = func  # type: ignore[assignment]
        else:
            async def coro(*args, **kwargs):  # type: ignore[misc]
                func(*args, **kwargs)
        self._handlers.setdefault(type, []).append(coro)
        return func

    async def fire(self, type: str, *args, **kwargs) -> None:
        for h in list(self._handlers.get(type, [])):
            try:
                await h(*args, **kwargs)
            except Exception:  # pragma: no cover
                get_logger().exception("RTC handler error for %s", type)


class _NyxAudioEventDecorator:
    """Usage:
        @nyx_audio_event(type=nyx_audio_event.REMOTE_AUDIO)
        async def on_audio(participant, frame_bytes, sample_rate, channels): ...
    """
    REMOTE_AUDIO = "REMOTE_AUDIO"
    RTC_CONNECT = "RTC_CONNECT"
    RTC_DISCONNECT = "RTC_DISCONNECT"
    PARTICIPANT_JOIN = "PARTICIPANT_JOIN"
    PARTICIPANT_LEAVE = "PARTICIPANT_LEAVE"
    TRACK_SUBSCRIBED = "TRACK_SUBSCRIBED"
    TRACK_UNSUBSCRIBED = "TRACK_UNSUBSCRIBED"

    def __init__(self, dispatcher: _AudioDispatcher) -> None:
        self._dispatcher = dispatcher

    def __call__(self, *, type: str):
        def deco(func):
            return self._dispatcher.register(type, func)
        return deco


# -------------------------
# Runtime audio publisher
# -------------------------

@dataclass
class NyxAudioSource:
    """Thin wrapper to push PCM frames into LiveKit."""
    _source: "rtc.AudioSource"
    _local_track: "rtc.LocalAudioTrack"
    _room: "rtc.Room"
    _logger: Any

    async def publish_pcm(
            self,
            generator,
            *,
            frame_ms: int = 10,  # 10ms is safest for LiveKit
            sample_rate: int = 48000,
            channels: int = 1,
    ) -> None:
        """Consume a (a)sync generator yielding int16-LE PCM bytes and pace in realtime."""
        frame_dur = frame_ms / 1000.0
        bytes_per_frame = int(sample_rate * frame_dur) * channels * 2  # 2 bytes/sample
        loop = asyncio.get_running_loop()
        next_deadline = loop.time()

        async for chunk in _aiter(generator):
            # keep schedule even on empty chunks
            if not chunk:
                next_deadline += frame_dur
                await asyncio.sleep(max(0.0, next_deadline - loop.time()))
                continue

            # derive samples_per_channel from actual payload
            spc = len(chunk) // (channels * 2)

            # defensive: pad/truncate to exact frame size to avoid buffer jitter
            if len(chunk) < bytes_per_frame:
                # pad with zeros (silence)
                chunk = chunk + b"\x00" * (bytes_per_frame - len(chunk))
                spc = bytes_per_frame // (channels * 2)
            elif len(chunk) > bytes_per_frame:
                chunk = chunk[:bytes_per_frame]
                spc = bytes_per_frame // (channels * 2)

            if spc <= 0:
                next_deadline += frame_dur
                await asyncio.sleep(max(0.0, next_deadline - loop.time()))
                continue

            frm = rtc.AudioFrame(
                data=chunk,
                sample_rate=sample_rate,
                num_channels=channels,
                samples_per_channel=spc,
            )

            # push; await if coroutine on your SDK
            res = self._source.capture_frame(frm)
            if inspect.isawaitable(res):
                await res

            # pace by deadline (accounts for processing time)
            next_deadline += frame_dur
            await asyncio.sleep(max(0.0, next_deadline - loop.time()))

    async def write_pcm(self, data: bytes, *, sample_rate: int = 48000, channels: int = 1) -> None:
        """Push a single int16-LE PCM frame immediately (use for custom pacing)."""
        spc = len(data) // (channels * 2)
        if spc <= 0:
            return
        frm = rtc.AudioFrame(
            data=data,
            sample_rate=sample_rate,
            num_channels=channels,
            samples_per_channel=spc,
        )
        res = self._source.capture_frame(frm)
        if inspect.isawaitable(res):
            await res

    async def close(self) -> None:
        try:
            await self._room.local_participant.unpublish_track(self._local_track)
        except Exception:
            self._logger.debug("RTC: unpublish failed", exc_info=True)


# -------------------------
# Session per voice room
# -------------------------

class NyxRTCSession:
    """A joined LiveKit room tied to a Nyx voice channel."""

    def __init__(
        self,
        *,
        http_client,                       # NyxHTTPClient (or NyxClient)
        channel_id: int,
        base_ws_url: str = "wss://livekit.nyx-app.ru",
        auto_subscribe: bool = False,
        logger=None,
    ) -> None:
        if rtc is None:
            raise NyxError("livekit sdk not installed. Run: pip install livekit")
        self._subwatch_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._http = http_client
        self._channel_id = int(channel_id)
        self._base_ws_url = base_ws_url.rstrip("/")
        self._auto_subscribe = bool(auto_subscribe)
        self._logger = logger or get_logger()
        self._room: Optional["rtc.Room"] = None
        self._connected = asyncio.Event()

        # events
        self._disp = _AudioDispatcher()
        self.events = _NyxAudioEventDecorator(self._disp)

        # track housekeeping
        self._remote_audio_tasks: set[asyncio.Task] = set()
        self._republish_audio: Optional[NyxAudioSource] = None  # restart on reconnect

    def _wrap_cb(self, coro_fn):
        def _cb(*args, **kwargs):
            loop = self._loop or asyncio.get_running_loop()
            loop.create_task(coro_fn(*args, **kwargs))

        return _cb

    # ---- public API ----
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def on(self, *, type: str):
        """Alternate to decorator: session.on(type=...)."""
        return self._disp.__call__(type=type)

    async def leave(self) -> None:
        await self._teardown_room()

    async def start_audio_source(
            self, *, sample_rate: int = 48000, channels: int = 1, name: str = "livekit-track"
    ) -> NyxAudioSource:
        """Create and publish an audio source; you can feed frames via write_pcm/publish_pcm."""
        room = await self._require_room()

        src = rtc.AudioSource(sample_rate=sample_rate, num_channels=channels)
        track = rtc.LocalAudioTrack.create_audio_track(name, src)

        # Publish with source=MICROPHONE if the SDK supports it
        publish_opts = None
        try:
            TrackSource = getattr(rtc, "TrackSource", None)
            TrackPublishOptions = getattr(rtc, "TrackPublishOptions", None)
            if TrackSource and TrackPublishOptions:
                mic_src = getattr(TrackSource, "SOURCE_MICROPHONE", getattr(TrackSource, "TRACK_SOURCE_MICROPHONE", None))
                if mic_src is not None:
                    publish_opts = TrackPublishOptions(source=mic_src)
        except Exception:
            publish_opts = None

        if publish_opts is not None:
            await room.local_participant.publish_track(track, publish_opts)
        else:
            # fallback: old SDKs without publish options
            await room.local_participant.publish_track(track)

        # Make sure the track is enabled/unmuted
        try:
            if hasattr(track, "set_enabled"):
                track.set_enabled(True)
        except Exception:
            self._logger.debug("RTC: set_enabled(True) failed", exc_info=True)
        try:
            if hasattr(track, "unmute"):
                track.unmute()
            elif hasattr(track, "set_muted"):
                track.set_muted(False)
        except Exception:
            self._logger.debug("RTC: unmute/set_muted(False) failed", exc_info=True)

        # Optional: reflect state for app UIs that read participant attributes
        try:
            await room.local_participant.set_attributes(
                {"sound_muted": "false", "mic_muted_by_audio": "false", "audio_muted": "false"}
            )
        except Exception:
            self._logger.debug("RTC: set_attributes failed", exc_info=True)

        aus = NyxAudioSource(src, track, room, self._logger)
        self._republish_audio = aus  # re-publish after reconnect
        return aus

    # ---- lifecycle ----
    async def connect(self, *, wait_connected: bool = False, timeout: float = 10.0, **join_opts) -> "NyxRTCSession":
        """Fetch token from Nyx and join LiveKit room."""
        token = await self._fetch_token(**join_opts)
        await self._connect_livekit(token, wait_connected=wait_connected, timeout=timeout)
        return self

    # ---- internal plumbing ----
    async def _fetch_token(self, **join_opts) -> str:
        """GET /i/channels/{id}/token with passthrough options; returns JWT string."""
        # Defaults mirroring your browser URL
        defaults = dict(
            sound_muted=False,
            mic_muted_by_audio=False,
            audio_muted=False,
            audio_level_threshold=0.01,
            ptt_enabled=False,
            ptt_timeout=200,
        )
        params = {**defaults, **join_opts}
        path = f"/i/channels/{self._channel_id}/token"
        resp = await self._http.request("GET", path, params=params)
        try:
            data = resp.json()
        except Exception:
            data = {}
        token = (data or {}).get("token")
        if not isinstance(token, str) or not token:
            self._logger.error("RTC token missing for %s params=%s", path, _safe_json(params))
            raise NyxError("Failed to obtain RTC token")
        return token

    async def _connect_livekit(self, token: str, *, wait_connected: bool, timeout: float) -> None:
        """Connect to LiveKit signaling and wire events."""
        await self._teardown_room()

        room = rtc.Room()
        self._room = room

        self._loop = asyncio.get_running_loop()
        room.on("connected", self._wrap_cb(self._on_connected))
        room.on("disconnected", self._wrap_cb(self._on_disconnected))
        room.on("participant_connected", self._wrap_cb(self._on_participant_connected))
        room.on("participant_disconnected", self._wrap_cb(self._on_participant_disconnected))
        room.on("track_subscribed", self._wrap_cb(self._on_track_subscribed))
        room.on("track_unsubscribed", self._wrap_cb(self._on_track_unsubscribed))
        # IMPORTANT: no room-level 'track_published' hook (not present in this SDK)

        if hasattr(rtc, "ConnectOptions"):
            opts = rtc.ConnectOptions(auto_subscribe=self._auto_subscribe)
        elif hasattr(rtc, "RoomOptions"):
            opts = rtc.RoomOptions(auto_subscribe=self._auto_subscribe)
        else:
            opts = None

        await room.connect(self._base_ws_url, token, opts)

        if wait_connected:
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "RTC: timeout (%.1fs) waiting for connected event; proceeding. state=%r",
                    timeout, getattr(room, "connection_state", None)
                )
                try:
                    state = getattr(room, "connection_state", None)
                    if state and "CONNECTED" in str(state).upper():
                        self._connected.set()
                except Exception:
                    pass

        # Re-publish audio source after reconnect, if we had one
        if self._republish_audio:
            try:
                aus = self._republish_audio
                publish_opts = None
                try:
                    TrackSource = getattr(rtc, "TrackSource", None)
                    TrackPublishOptions = getattr(rtc, "TrackPublishOptions", None)
                    if TrackSource and TrackPublishOptions:
                        mic_src = getattr(TrackSource, "MICROPHONE",
                                          getattr(TrackSource, "TRACK_SOURCE_MICROPHONE", None))
                        if mic_src is not None:
                            publish_opts = TrackPublishOptions(source=mic_src)
                except Exception:
                    publish_opts = None

                if publish_opts is not None:
                    await room.local_participant.publish_track(aus._local_track, publish_opts)
                else:
                    await room.local_participant.publish_track(aus._local_track)
            except Exception:
                self._logger.debug("RTC re-publish failed after reconnect", exc_info=True)

    def _iter_remote_participants(self) -> list:
        room = self._room
        if not room:
            return []
        rp = getattr(room, "remote_participants", None)
        if isinstance(rp, dict):
            return list(rp.values())
        if isinstance(rp, list):
            return rp
        return []

    async def _subscribe_pub_if_audio(self, pub) -> None:
        try:
            kind_audio = getattr(rtc.TrackKind, "AUDIO", getattr(rtc.TrackKind, "KIND_AUDIO", None))
            k = getattr(pub, "kind", None)
            if k is None and hasattr(pub, "track"):
                k = getattr(pub.track, "kind", None)
            if k == kind_audio or (k and str(k).upper().endswith("AUDIO")):
                maybe = pub.set_subscribed(True)
                if inspect.isawaitable(maybe):
                    await maybe
        except Exception:
            self._logger.debug("RTC: subscribe pub failed", exc_info=True)

    async def _subscribe_all_audio(self) -> None:
        """Subscribe to all audio publications currently visible."""
        try:
            for p in self._iter_remote_participants():
                pubs = getattr(p, "track_publications", None) or []
                if isinstance(pubs, dict):
                    pubs = list(pubs.values())
                for pub in pubs:
                    await self._subscribe_pub_if_audio(pub)
        except Exception:
            self._logger.debug("RTC: subscribe all failed", exc_info=True)

    async def _subscription_watcher(self) -> None:
        """Poll for new remote audio publications and subscribe them."""
        try:
            while self._room is not None and self._connected.is_set():
                await self._subscribe_all_audio()
                await asyncio.sleep(0.75)  # light polling; adjust if needed
        except asyncio.CancelledError:
            return
        except Exception:
            self._logger.debug("RTC: subscription watcher error", exc_info=True)

    def _start_subscription_watcher(self) -> None:
        if self._subwatch_task is None or self._subwatch_task.done():
            loop = self._loop or asyncio.get_running_loop()
            self._subwatch_task = loop.create_task(self._subscription_watcher())

    def _stop_subscription_watcher(self) -> None:
        t = self._subwatch_task
        if t and not t.done():
            t.cancel()
        self._subwatch_task = None

    def _bind_participant_callbacks(self, participant: "rtc.RemoteParticipant") -> None:
        """Bind per-participant events that don't exist at room level in this SDK."""
        # track_published on the participant
        participant.on(
            "track_published",
            self._wrap_cb(lambda publication, p=participant: self._on_participant_track_published(p, publication)),
        )

    def _iter_remote_participants(self) -> list:
        room = self._room
        if not room:
            return []
        rp = getattr(room, "remote_participants", None)
        if isinstance(rp, dict):
            return list(rp.values())
        if isinstance(rp, list):
            return rp
        return []

    async def _subscribe_pub_if_audio(self, pub):
        try:
            kind_audio = getattr(rtc.TrackKind, "AUDIO", getattr(rtc.TrackKind, "KIND_AUDIO", None))
            k = getattr(pub, "kind", None)
            if k is None and hasattr(pub, "track"):
                k = getattr(pub.track, "kind", None)
            if k == kind_audio or (k and str(k).upper().endswith("AUDIO")):
                maybe = pub.set_subscribed(True)
                if inspect.isawaitable(maybe):
                    await maybe
        except Exception:
            self._logger.debug("RTC: subscribe pub failed", exc_info=True)

    async def _subscribe_all_audio(self):
        try:
            for p in self._iter_remote_participants():
                pubs = getattr(p, "track_publications", None) or []
                if isinstance(pubs, dict):
                    pubs = list(pubs.values())
                for pub in pubs:
                    await self._subscribe_pub_if_audio(pub)
        except Exception:
            self._logger.debug("RTC: subscribe all failed", exc_info=True)

    async def _require_room(self) -> "rtc.Room":
        if self._room is None:
            raise NyxError("RTC room not connected")
        return self._room

    # ---- room event handlers ----
    async def _on_connected(self, *_):
        self._connected.set()
        try:
            if not self._auto_subscribe:
                await self._subscribe_all_audio()  # immediate pass
                self._start_subscription_watcher()  # keep subscribing new pubs
        except Exception:
            self._logger.debug("RTC: subscribe-existing failed", exc_info=True)
        await self._disp.fire(_NyxAudioEventDecorator.RTC_CONNECT)

    async def _on_disconnected(self, *_):
        self._stop_subscription_watcher()
        self._connected.clear()
        await self._disp.fire(_NyxAudioEventDecorator.RTC_DISCONNECT)

    async def _teardown_room(self) -> None:
        self._stop_subscription_watcher()
        if self._room is None:
            return
        for t in list(self._remote_audio_tasks):
            t.cancel()
        self._remote_audio_tasks.clear()
        try:
            await self._room.disconnect()
        except Exception:
            pass
        self._room = None
        self._connected.clear()

    async def _on_participant_connected(self, participant: "rtc.RemoteParticipant", *_):
        # bind future track publications from THIS participant
        try:
            self._bind_participant_callbacks(participant)
            # subscribe to anything already published on this participant
            pubs = getattr(participant, "track_publications", None) or []
            if isinstance(pubs, dict):
                pubs = list(pubs.values())
            for pub in pubs:
                await self._subscribe_pub_if_audio(pub)
        except Exception:
            self._logger.debug("RTC: subscribe on participant_connected failed", exc_info=True)
        await self._disp.fire(_NyxAudioEventDecorator.PARTICIPANT_JOIN, participant)

    async def _on_participant_track_published(self, participant: "rtc.RemoteParticipant", publication, *_):
        """participant.on('track_published', ...) → subscribe when auto_subscribe=False"""
        try:
            await self._subscribe_pub_if_audio(publication)
        except Exception:
            self._logger.debug("RTC: failed to subscribe to participant published track", exc_info=True)

    async def _on_participant_disconnected(self, participant: "rtc.RemoteParticipant", *_):
        await self._disp.fire(_NyxAudioEventDecorator.PARTICIPANT_LEAVE, participant)

    async def _on_track_subscribed(self, track: "rtc.RemoteTrack", publication, participant: "rtc.RemoteParticipant", *_):
        await self._disp.fire(_NyxAudioEventDecorator.TRACK_SUBSCRIBED, participant, track)
        # if this is an audio track, start pumping PCM frames
        try:
            if getattr(track, "kind", None) == getattr(rtc.TrackKind, "KIND_AUDIO", None):
                task = (self._loop or asyncio.get_running_loop()).create_task(
                    self._pump_remote_audio(track, participant))
                self._remote_audio_tasks.add(task)
                task.add_done_callback(self._remote_audio_tasks.discard)
        except Exception:
            self._logger.debug("RTC track_subscribed handler failed", exc_info=True)

    async def _on_track_unsubscribed(self, track, publication, participant: "rtc.RemoteParticipant", *_):
        await self._disp.fire(_NyxAudioEventDecorator.TRACK_UNSUBSCRIBED, participant, track)

    async def _on_track_published(self, publication, participant, *_):
        """Manually subscribe when auto_subscribe=False."""
        try:
            kind_audio = getattr(rtc.TrackKind, "AUDIO", None)
            k = getattr(publication, "kind", None)
            # some SDKs only expose kind on the track
            if k is None and hasattr(publication, "track"):
                k = getattr(publication.track, "kind", None)
            if k == kind_audio or (k and str(k).upper().endswith("AUDIO")):
                maybe = publication.set_subscribed(True)
                if inspect.isawaitable(maybe):
                    await maybe
        except Exception:
            self._logger.debug("RTC: failed to subscribe to published track", exc_info=True)

    async def _pump_remote_audio(self, track: "rtc.RemoteAudioTrack", participant: "rtc.RemoteParticipant") -> None:
        """Continuously read PCM frames from a remote audio track and fire REMOTE_AUDIO callbacks."""
        try:
            # LiveKit Python SDK provides AudioStream over audio tracks
            stream = rtc.AudioStream(track)
            async for frame in stream:
                # frame: rtc.AudioFrame with .data (bytes), .sample_rate, .num_channels
                try:
                    f = frame.frame
                    await self._disp.fire(
                        _NyxAudioEventDecorator.REMOTE_AUDIO,
                        participant,
                        f.data,
                        f.sample_rate,
                        f.num_channels,
                    )
                except Exception:
                    self._logger.debug("RTC audio dispatch failed", exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception:
            self._logger.debug("RTC audio stream closed", exc_info=True)

# -------------------------
# Public decorator instance
# -------------------------

# This module-level dispatcher is used for *global* audio events if you want it.
# Most users will prefer the per-session decorator: `session.events(...)`.
_audio_dispatcher = _AudioDispatcher()
nyx_audio_event = _NyxAudioEventDecorator(_audio_dispatcher)


# -------------------------
# Utilities
# -------------------------

async def _aiter(obj):
    """Turn sync/async generators or iterables into an async generator."""
    if hasattr(obj, "__aiter__"):
        async for x in obj:
            yield x
        return
    for x in obj:
        yield x
        await asyncio.sleep(0)  # allow switch
