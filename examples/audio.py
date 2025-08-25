import asyncio
import os
import wave

import audioop # pip install audioop-lts

from nyxpy import (
    NyxClient, nyx_event, NyxError
)

nyx = NyxClient()


async def wav_pcm_stream(
        path: str,
        *,
        target_sr: int = 48000,
        target_ch: int = 1,
        frame_ms: int = 10,
):
    """
    Данный метод нужен, чтобы разбить WAV файл на int16-LE PCM кадры (фреймы), которые затем отсылаются поверх WebSocket.
    Для иных источников данных может потребоваться дополнительная обработка.
    """
    if not os.path.isfile(path):
        raise NyxError(f"WAV not found: {path}")

    bytes_per_frame = int(target_sr * (frame_ms / 1000.0)) * target_ch * 2  # 2 bytes/sample
    state = None
    buf = b""

    with wave.open(path, "rb") as wf:
        in_ch = wf.getnchannels()
        in_sr = wf.getframerate()
        in_sw = wf.getsampwidth()  # кол-во байт на один сэмпл

        if in_ch not in (1, 2):
            raise NyxError(f"Unsupported channels in WAV: {in_ch}")

        # выбор размера чанка в размере количества входящих фреймов
        in_frames_per_chunk = max(1024, int(in_sr * frame_ms / 1000) * 4)

        while True:
            raw = wf.readframes(in_frames_per_chunk)
            if not raw:
                break

            # стандартизация длины до 16 бит
            if in_sw != 2:
                raw = audioop.lin2lin(raw, in_sw, 2)

            # из стерео в моно если нужно
            if in_ch == 2 and target_ch == 1:
                raw = audioop.tomono(raw, 2, 0.5, 0.5)
            elif in_ch == 1 and target_ch == 2:
                raw = audioop.tostereo(raw, 2, 1.0, 1.0)
            elif in_ch == target_ch:
                pass
            else:
                raise NyxError(f"Unsupported channel mapping: {in_ch} → {target_ch}")

            # перекодирование
            if in_sr != target_sr:
                raw, state = audioop.ratecv(raw, 2, target_ch, in_sr, target_sr, state)

            buf += raw

            # получаем правильные фреймы
            while len(buf) >= bytes_per_frame:
                out = buf[:bytes_per_frame]
                buf = buf[bytes_per_frame:]
                yield out
                await asyncio.sleep(0)

    # добиваем 0x00 до нужной длины (паддинг)
    if buf:
        if len(buf) < bytes_per_frame:
            buf += b"\x00" * (bytes_per_frame - len(buf))
        yield buf

@nyx_event(type=nyx_event.NEW_MESSAGE) # новое сообщение
async def on_message(ev):
    if ev.data["sender_id"] == nyx.current_user.id: # игнорируем наши же сообщения
        return

    msg = ev.data["data"]["content"]
    server_id = int(ev.data["channel_name"].split(":")[1])

    if msg.startswith("!музыка"):
        # получаем каналы на сервере юзера
        channels = await nyx.get_channels(server_id=server_id)

        # находим канал, где сидит пользователь
        voice_channel = next(
            (
                ch for ch in channels
                if isinstance(ch.type, str) and ch.type.lower() == "voice"
                   and ch.participants
                   and any(p.user_id == ev.data["member"]["id"] for p in ch.participants)
            ),
            None,
        )

        if not voice_channel:
            await nyx.reply(ev, f"Привет, {ev.data["member"]["username"]}! Не вижу тебя ни в одном голосовом канале 🤔")
            return

        await nyx.reply(ev, f"Привет, {ev.data["member"]["username"]}! Вижу тебя в #{voice_channel.name} (ID: {voice_channel.id}).")
        session = await nyx.voice_join(
            channel_id=voice_channel.id,
            audio_muted=False,
            sound_muted=False,
            ptt_enabled=False,
            ptt_timeout=200,
        )

        # вот так, кстати, можно обрабатывать входящее аудио:
        @session.events(type=session.events.REMOTE_AUDIO)
        async def _on_audio(participant, frame_bytes, sr, ch):
            return

        # а вот тут мы отсылаем содержимое файла audio_test.wav
        src = await session.start_audio_source()
        await asyncio.sleep(1)
        asyncio.create_task(src.publish_pcm(wav_pcm_stream("examples/audio_test.wav")))


async def main():
    email = "ваша почта"
    password = "ваш пароль"

    # Аутентифицируемся в Nyx
    await nyx.login(email, password)

    # Подписываемся на все обновления
    await nyx.connect_ws()
    await nyx.ws_subscribe_all()

    # Ожидаем события...
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
