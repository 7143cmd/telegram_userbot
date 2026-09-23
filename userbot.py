import json
import asyncio
import re
import time
import random
from pathlib import Path
from telethon import TelegramClient, events, utils
from llm import ask_llm

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

def _find_group_dialog(dialogs, group_ref):
    try:
        raw_id = int(str(group_ref))
        wanted = _candidate_ids(raw_id)
        for dialog in dialogs:
            if not dialog.is_group:
                continue
            marked_id = utils.get_peer_id(dialog.entity)
            if marked_id in wanted or abs(marked_id) in wanted:
                return dialog.entity
    except (ValueError, TypeError):
        pass

    target = str(group_ref).strip().lower()
    for dialog in dialogs:
        if not dialog.is_group:
            continue
        title = (dialog.name or "").strip().lower()
        if title == target:
            return dialog.entity

    return None


async def resolve_group(client: TelegramClient, dialogs, group_ref):
    entity = _find_group_dialog(dialogs, group_ref)
    if entity is not None:
        return entity, utils.get_peer_id(entity)

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

async def resolve_all_groups(client: TelegramClient, groups_config: dict):
    dialogs = [d async for d in client.iter_dialogs()]

    group_map = {}
    for group_ref, prompt in groups_config.items():
        entity, marked_id = await resolve_group(client, dialogs, group_ref)
        group_map[marked_id] = {
            "title": getattr(entity, "title", str(group_ref)),
            "prompt": prompt,
        }
    return group_map


async def main():
    config = load_config()

    api_id = config["id"]
    api_hash = config["hash"]
    trigger_name = config["name"]
    session_name = config.get("session_name", "userbot_session")
    groups_config = config["groups"]
    min_cooldown = int(config.get("min_random_cooldown", 0))
    max_cooldown = int(config.get("max_random_cooldown", 0))
    cooldown = float(config.get("cooldown_seconds", 0))

    trigger_re = build_trigger_regex(trigger_name)

    client = TelegramClient(session_name, api_id, api_hash)

    await client.start()        #type: ignore

    group_map = await resolve_all_groups(client, groups_config)

    print(f"[OK] Реагирую на упоминания: '{trigger_name}'")
    for chat_id, info in group_map.items():
        print(f"[OK] Слушаю группу: {info['title']} (id={chat_id}) :: prompt='{info['prompt']}'")

    last_reply_ts: dict[int, float] = {}

    @client.on(events.NewMessage())
    async def handler(event):
        group_info = group_map.get(event.chat_id)
        if group_info is None:
            return
        if event.out:
            return

        text = event.raw_text or ""
        print(f"[DEBUG] [{group_info['title']}] сообщение: {text!r}")

        if not trigger_re.search(text):
            return

        now = time.time()
        last_ts = last_reply_ts.get(event.chat_id, 0.0)
        if cooldown and (now - last_ts) < cooldown:
            return

        group_prompt = group_info["prompt"]
        try:
            random_kd = random.randint(min_cooldown, max_cooldown)
            print(random_kd)
            reply_text, _ = await asyncio.gather(
                ask_llm(text, group_prompt),
                asyncio.sleep(random_kd)
            )
        except Exception as e:
            print(f"[ERROR] Не удалось сгенерировать ответ: {e}")
            return

        try:
            await event.reply(reply_text)
            last_reply_ts[event.chat_id] = now
            print(f"[REPLY] [{group_info['title']}] -> {text!r} :: {reply_text!r}")
        except Exception as e:
            print(f"[ERROR] Не удалось ответить: {e}")

    print("[OK] Бот запущен и слушает сообщения. Ctrl+C для остановки.")
    await client.run_until_disconnected()       #type: ignore


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())