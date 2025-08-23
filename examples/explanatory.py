"""
Демонстрация работы NyxPy:

- настройка логгирования
- аутентификация при помощи email + password
- получение списка серверов
- события и сообщения в реальном времени:
  CONNECT, DISCONNECT, NEW_MESSAGE, TYPING, PRESENCE_UPDATE, RAW
- отправка сообщений
"""

import asyncio
import logging

from nyxpy import (
    NyxClient,
    nyx_event,
    configure_logging,
    Server,
)

nyx = NyxClient()

# Обработчики событий
@nyx_event(type=nyx_event.CONNECT) # WS успешно подключен!
def on_connect(ev):
    print("[ПОДКЛЮЧЕНИЕ]", ev.raw)

@nyx_event(type=nyx_event.DISCONNECT) # отключение WS
def on_disconnect(ev):
    print("[ОТКЛЮЧЕНИЕ]", ev)

@nyx_event(type=nyx_event.PRESENCE_UPDATE) # обновления состояний пользователей
def on_presence(ev):
    # print(f"[PRESENCE] channel={ev.channel} size={len(ev.data) if isinstance(ev.data, dict) else '?'}")
    return

@nyx_event(type=nyx_event.NEW_MESSAGE) # новое сообщение
async def on_message(ev):
    if ev.data["sender_id"] == nyx.current_user.id: # игнорируем наши же сообщения
        return
    print(f"[СООБЩЕНИЕ] channel={ev.channel} data={ev.data}")

    server_id = int(ev.data["channel_name"].split(":")[1])

    # получим список ролей на сервере
    roles = await nyx.get_roles(server_id)
    roles_text = ""
    for role in roles:
        roles_text += f"{role.name} ({role.id}), "

    # получим список каналов на сервере
    channels = await nyx.get_channels(server_id)
    channels_text = ""
    for channel in channels:
        channels_text += f"{channel.name} ({channel.id}), "

    await nyx.reply(ev, f"Привет, {ev.data["member"]["username"]}! Твой ID: {ev.data["sender_id"]}, а твоё сообщение имеет длину: {len(ev.data["data"]["content"])}")
    await nyx.reply(ev, f"Нашёл на этом сервере {len(roles)} ролей! Вот они: {roles_text}")
    await nyx.reply(ev, f"Сервер имеет {len(channels)} каналов. Перечисляю: {channels_text}")
    # await nyx.send_message(ev.data["channel_name"], "а это сообщение можно было отправить и по-иному")

@nyx_event(type=nyx_event.TYPING) # событие типа "kolya печатает..."
async def on_typing(ev):
    print(f"[ПЕЧАТАЕТ] channel={ev.channel} data={ev.data}")

@nyx_event(type=nyx_event.FRIEND_REQUEST) # новый запрос в друзья
async def on_friend_request(ev):
    print(f"[ПРЕДЛОЖЕНИЕ ДРУЖБЫ] channel={ev.channel} data={ev.data}")
    # получаем все запросы в друзья
    friend_requests = await nyx.get_received_friend_requests()
    # принимаем все запросы в друзья
    for friend in friend_requests:
        await nyx.accept_friend_request(friend.id)

# Сюда будут приходить все события и иные сообщения
@nyx_event(type=nyx_event.RAW)
def on_raw(ev):
    # Полезная функция для обработки "сырых" сообщений от сервера.
    # print("[RAW]", ev.raw)
    return

async def main():
    configure_logging(logging.DEBUG)

    email = "ваша почта"
    password = "ваш пароль"

    # Пример 1: Аутентификация
    auth = await nyx.login(email, password)
    print("Статус аутентификации:", nyx.is_authenticated)
    print("Текущий пользователь:", nyx.current_user.username)

    # Пример 2: Получить и вывести список серверов
    servers: list[Server] = await nyx.get_servers()
    print("Список серверов:")
    for s in servers:
        print(f"  - id={s.id} name={s.name!r} owner_id={s.owner_id} avatar_url={s.avatar_url!r}")

    # Пример 3: Подключение к обновлениям в реальном времени
    await nyx.connect_ws()
    # подписываемся на все доступные сервера.
    await nyx.ws_subscribe_all()
    # подписаться на один сервер: await nyx.ws_subscribe_server(id=123)

    # Ожидаем события...
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
