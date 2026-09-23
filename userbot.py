import json
import random
import re
import time
from pathlib import Path

from telethon import TelegramClient, events, utils

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_trigger_regex(name: str) -> re.Pattern:
    escaped = re.escape(name)
    pattern = rf"(?<![\wа-яА-ЯёЁ@])@?{escaped}(?![\wа-яА-ЯёЁ])"
    return re.compile(pattern, flags=re.IGNORECASE | re.UNICODE)


def _candidate_ids(raw_id: int):
    candidates = {raw_id, abs(raw_id)}
    s = str(abs(raw_id))
    if s.startswith("100"):
        candidates.add(int(s[3:]))
    else:
        candidates.add(int(f"-100{s}"))
        candidates.add(int(f"100{s}"))
    return candidates


async def resolve_group(client: TelegramClient, group_ref):
    
    dialogs = [d async for d in client.iter_dialogs()]

    try:
        raw_id = int(str(group_ref))
        wanted = _candidate_ids(raw_id)
        for dialog in dialogs:
            if not dialog.is_group:
                continue
            marked_id = utils.get_peer_id(dialog.entity)
            if marked_id in wanted or abs(marked_id) in wanted:
                return dialog.entity, marked_id
    except (ValueError, TypeError):
        pass

    target = str(group_ref).strip().lower()
    for dialog in dialogs:
        if not dialog.is_group:
            continue
        title = (dialog.name or "").strip().lower()
        if title == target:
            return dialog.entity, utils.get_peer_id(dialog.entity)

    try:
        entity = await client.get_entity(group_ref)
        return entity, utils.get_peer_id(entity)
    except Exception:
        pass

    available_groups = [
        f"'{d.name}' (id={utils.get_peer_id(d.entity)})" for d in dialogs if d.is_group
    ]
    raise RuntimeError(
        f"""Не удалось найти группу '{group_ref}'.
        Доступные группы этого аккаунта: {available_groups or 'нет ни одной группы'}"""
    )


async def main():
    config = load_config()

    api_id = config["id"]
    api_hash = config["hash"]
    trigger_name = config["name"]
    session_name = config.get("session_name", "userbot_session")
    group_ref = config["group"]
    responses = config.get("responses") or ["Да, слушаю!"]
    cooldown = float(config.get("cooldown_seconds", 0))

    trigger_re = build_trigger_regex(trigger_name)

    client = TelegramClient(session_name, api_id, api_hash)

    await client.start()

    group_entity, target_chat_id = await resolve_group(client, group_ref)
    print(f"[OK] Слушаю группу: {getattr(group_entity, 'title', group_ref)} (id={target_chat_id})")
    print(f"[OK] Реагирую на упоминания: '{trigger_name}'")

    last_reply_ts = 0.0

    @client.on(events.NewMessage())
    async def handler(event):
        nonlocal last_reply_ts

        if event.chat_id != target_chat_id:
            return
        
        if event.out:
            return

        text = event.raw_text or ""
        print(f"[DEBUG] сообщение в целевой группе: {text!r}")

        if not trigger_re.search(text):
            return

        now = time.time()
        if cooldown and (now - last_reply_ts) < cooldown:
            return

        reply_text = random.choice(responses)
        try:
            await event.reply(reply_text)
            last_reply_ts = now
            print(f"[REPLY] -> {text!r} :: {reply_text!r}")
        except Exception as e:
            print(f"[ERROR] Не удалось ответить: {e}")

    print("[OK] Бот запущен и слушает сообщения. Ctrl+C для остановки.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())