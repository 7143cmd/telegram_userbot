import asyncio
import json
import random
import time
from difflib import SequenceMatcher
from pathlib import Path

from telethon import TelegramClient, events, utils

from llm import ask_llm, detect_addressee

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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
        f"Не удалось найти группу '{group_ref}'.\n"
        f"Доступные группы этого аккаунта: {available_groups or 'нет ни одной группы'}"
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


def _names_match(detected: str, trigger_name: str) -> bool:
    a, b = detected.strip().lower(), trigger_name.strip().lower()
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.8


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

    client = TelegramClient(session_name, api_id, api_hash)

    await client.start()  # type: ignore

    me = await client.get_me()

    group_map = await resolve_all_groups(client, groups_config)

    print(f"[OK] Имя бота: '{trigger_name}' (me.id={me.id})")
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

        now = time.time()
        last_ts = last_reply_ts.get(event.chat_id, 0.0)
        if cooldown and (now - last_ts) < cooldown:
            return

        group_prompt = group_info["prompt"]

        is_reply_to_me = False
        if event.is_reply:
            replied = await event.get_reply_message()
            is_reply_to_me = bool(replied and replied.sender_id == me.id)

        should_reply_as_mention = False
        should_respond_as_plain_text = False

        if is_reply_to_me:
            should_reply_as_mention = True
        else:
            try:
                addressee = await detect_addressee(text)
            except Exception as e:
                print(f"[ERROR] detect_addressee: {e}")
                return

            if addressee is None:
                should_respond_as_plain_text = True
            elif _names_match(addressee, trigger_name):
                should_reply_as_mention = True
            else:
                print(f"[DEBUG] сообщение адресовано '{addressee}', не боту — игнор")
                return

        try:
            llm_task = asyncio.create_task(ask_llm(text, group_prompt))

            await asyncio.sleep(random.uniform(min_cooldown, max_cooldown))

            async with client.action(event.chat_id, 'typing'):      #type: ignore
                reply_text = await llm_task

                typing_time = min(max(len(reply_text) * 0.05, 1.5), 10)
                await asyncio.sleep(typing_time)
            if should_reply_as_mention:
                await event.reply(reply_text)
                kind = "REPLY"
            else:
                assert should_respond_as_plain_text
                await event.respond(reply_text)
                kind = "TEXT"

            last_reply_ts[event.chat_id] = now
            print(f"[{kind}] [{group_info['title']}] -> {text!r} :: {reply_text!r}")
        except Exception as e:
            print(f"[ERROR] Не удалось ответить: {e}")

    print("[OK] Бот запущен и слушает сообщения. Ctrl+C для остановки.")
    await client.run_until_disconnected()  # type: ignore


if __name__ == "__main__":
    asyncio.run(main())