import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events, utils

from llm import ask_llm, detect_addressee

CONFIG_PATH = Path(__file__).with_name("config.json")
KEYS_DIR = Path(__file__).with_name("keys")
SESSIONS_DIR = Path(__file__).with_name("sessions")

STARTER_MESSAGE = "Как прошёл твой день?"

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

@dataclass
class Account:
    app_id: int
    app_hash: str
    session_file: str
    first_name: str
    last_name: str
    is_admin: bool
    source_file: str
    client: Optional[TelegramClient] = None
    me_id: Optional[int] = None

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.session_file

    @property
    def label(self) -> str:
        return f"{self.name}{' [admin]' if self.is_admin else ''}"


def load_accounts() -> list[Account]:
    accounts = []
    for json_path in sorted(KEYS_DIR.glob("*.json")):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        is_admin = str(data.get("admin", "")).strip().lower() == "yes"

        accounts.append(Account(
            app_id=data["app_id"],
            app_hash=data["app_hash"],
            session_file=data["session_file"],
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            is_admin=is_admin,
            source_file=json_path.name,
        ))
    return accounts

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

async def collect_context(event, limit: int = 10) -> dict:
    trigger = {
        "id": event.id,
        "sender_id": event.sender_id,
        #"date": event.date.isoformat() if event.date else None,
        "text": event.raw_text or "",
        #"entities": event.entities,
    }

    messages = []

    async for message in event.client.iter_messages(
        event.chat_id,
        limit=limit + 1,
    ):

        if message.id == event.id:
            continue

        messages.append({
            "id": message.id,
            "sender_id": message.sender_id,
            #"date": message.date.isoformat() if message.date else None,
            "text": message.text,
            #"entities": message.entities,
        })

        if len(messages) >= limit:
            break

    messages.reverse()

    return {
        "trigger": trigger,
        "messages": messages,
    }

def _find_addressed_account(addressee: str, accounts: list[Account]) -> Optional[Account]:
    for account in accounts:
        if _names_match(addressee, account.first_name or account.name):
            return account
    return None

@dataclass
class GroupState:
    title: str
    prompt: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_handled_message_id: Optional[int] = None


async def main():
    config = load_config()
    groups_config = config["groups"]
    min_cooldown = int(config.get("min_random_cooldown", 0))
    max_cooldown = int(config.get("max_random_cooldown", 0))

    accounts = load_accounts()
    if not accounts:
        raise RuntimeError(f"В папке '{KEYS_DIR}' не найдено ни одного .json аккаунта")

    print(f"[OK] Найдено аккаунтов: {len(accounts)}")

    for account in accounts:
        session_path = SESSIONS_DIR / account.session_file  # без расширения
        account.client = TelegramClient(str(session_path), account.app_id, account.app_hash)
        await account.client.start()  # type: ignore
        me = await account.client.get_me()
        account.me_id = me.id
        print(f"[OK] Аккаунт готов: {account.label} (id={account.me_id})")

    group_map: dict[int, GroupState] = {}
    raw_group_map = await resolve_all_groups(accounts[0].client, groups_config)
    for chat_id, info in raw_group_map.items():
        group_map[chat_id] = GroupState(title=info["title"], prompt=info["prompt"])
        print(f"[OK] Слушаю группу: {info['title']} (id={chat_id})")

    def account_by_id(user_id: int) -> Optional[Account]:
        return next((a for a in accounts if a.me_id == user_id), None)

    async def handle_message(event):
        group_state = group_map.get(event.chat_id)
        if group_state is None:
            return
        
        if event.out:
            return

        async with group_state.lock:
            if group_state.last_handled_message_id == event.id:
                return
            group_state.last_handled_message_id = event.id

            text = event.raw_text or ""
            sender_id = event.sender_id
            sender_account = account_by_id(sender_id)
            sender_label = sender_account.label if sender_account else str(sender_id)
            print(f"[DEBUG] [{group_state.title}] {sender_label}: {text!r}")

            other_accounts = [a for a in accounts if a.me_id != sender_id]
            if not other_accounts:
                return

            target_account: Optional[Account] = None
            should_reply_as_mention = False

            if event.is_reply:
                replied = await event.get_reply_message()
                if replied:
                    target_account = next(
                        (a for a in other_accounts if a.me_id == replied.sender_id), None
                    )
                    if target_account is not None:
                        should_reply_as_mention = True

            if target_account is None:
                try:
                    addressee = await detect_addressee(text)
                except Exception as e:
                    print(f"[ERROR] detect_addressee: {e}")
                    addressee = None

                if addressee is not None:
                    matched = _find_addressed_account(addressee, other_accounts)
                    if matched is not None:
                        target_account = matched
                        should_reply_as_mention = True
                    else:
                        print(f"[DEBUG] сообщение адресовано '{addressee}' — это не один из наших ботов, игнор")
                        return

            if target_account is None:
                target_account = random.choice(other_accounts)
                should_reply_as_mention = False

            try:
                context = await collect_context(event, limit=10)
                context_trigger = context["trigger"]
                context_history = context["messages"]

                llm_task = asyncio.create_task(ask_llm(context_trigger, context_history, group_state.prompt))

                if min_cooldown or max_cooldown:
                    await asyncio.sleep(random.uniform(min_cooldown, max_cooldown))

                async with target_account.client.action(event.chat_id, 'typing'):  # type: ignore
                    reply_text = await llm_task
                    typing_time = min(max(len(reply_text) * 0.05, 1.5), 10)
                    await asyncio.sleep(typing_time)

                if should_reply_as_mention:
                    await target_account.client.send_message(
                        event.chat_id, reply_text, reply_to=event.id
                    )
                    kind = "REPLY"
                else:
                    await target_account.client.send_message(event.chat_id, reply_text)
                    kind = "TEXT"

                print(f"[{kind}] [{group_state.title}] {target_account.label} -> {text!r} :: {reply_text!r}")
            except Exception as e:
                print(f"[ERROR] Не удалось ответить ({target_account.label}): {e}")

    for account in accounts:
        account.client.add_event_handler(handle_message, events.NewMessage())

    starter_account = random.choice(accounts)
    for chat_id in group_map:
        await starter_account.client.send_message(chat_id, STARTER_MESSAGE)
        print(f"[START] {starter_account.label} -> {STARTER_MESSAGE!r}")

    print("[OK] Все боты запущены и слушают сообщения. Ctrl+C для остановки.")
    await asyncio.gather(*(a.client.run_until_disconnected() for a in accounts))  # type: ignore


if __name__ == "__main__":
    asyncio.run(main())