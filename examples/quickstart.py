import asyncio

from nyxpy import (
    NyxClient, nox_event
)

nyx = NyxClient()

@nox_event(type=nox_event.NEW_MESSAGE) # новое сообщение
async def on_message(ev):
    if ev.data["sender_id"] == nyx.current_user.id: # игнорируем наши же сообщения
        return

    msg = ev.data["data"]["content"]

    if msg.startswith("!привет"):
        await nyx.reply(ev, f"Привет, {ev.data["member"]["username"]}!")

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
