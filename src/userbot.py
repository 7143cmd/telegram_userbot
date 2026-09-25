import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events, utils

from llm import ask_llm, detect_addressee, generate_opening_message

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = BASE_DIR / "config.json"
KEYS_DIR = BASE_DIR / "keys"
SESSIONS_DIR = BASE_DIR / "sessions"

STARTER_MESSAGE = "Какой лучший фильм на ваш взгляд?"

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_theme() -> str:
    config = load_config()
    return str(config.get("theme_of_suggestion", "") or "")


def save_theme(new_theme: str) -> None:
    config = load_config()
    config["theme_of_suggestion"] = new_theme
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

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

async def collect_context(
    event,
    limit: int = 10,
    min_message_id: Optional[int] = None,
) -> dict:
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

        # Ignore every message that belongs to the previous topic.
        # The message that opened the new topic is the first valid
        # message of the new context.
        if min_message_id is not None and message.id < min_message_id:
            break

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
    recent_speakers: list = field(default_factory=list)

    # Runtime state of the currently active discussion topic.
    theme: str = ""
    theme_version: int = 0
    theme_started_message_id: Optional[int] = None


def create_dm_handler(account: Account, group_map: dict[int, GroupState]):
    async def handler(event: events.NewMessage.Event):
        if not (event.is_private and not event.out):
            return

        sender = await event.get_sender()

        first_name = getattr(sender, "first_name", "") or ""
        last_name = getattr(sender, "last_name", "") or ""
        sender_name = f"{first_name} {last_name}".strip() or "Неизвестный"

        text = (event.raw_text or "").strip()
        print(f"[DM] {account.name} <- {sender_name}: {text}")

        if not text:
            return

        try:
            save_theme(text)
            print(f"[THEME UPDATED] {text!r}")
        except Exception as e:
            print(f"[ERROR] Не удалось сохранить новую тему: {e}")
            return

        for chat_id, group_state in group_map.items():
            # Switch the runtime state immediately. Every new LLM request
            # will use this theme and a new theme version.
            group_state.theme = text
            group_state.theme_version += 1
            group_state.recent_speakers.clear()
            group_state.theme_started_message_id = None

            current_theme_version = group_state.theme_version

            try:
                opening_text = await generate_opening_message(
                    text,
                    group_state.prompt,
                )

                sent_message = await account.client.send_message(
                    chat_id,
                    opening_text,
                )

                # Everything before this message belongs to the previous topic.
                group_state.theme_started_message_id = sent_message.id

                print(
                    f"[OPEN] {account.label} -> [{group_state.title}] "
                    f"theme_version={current_theme_version}, "
                    f"start_message_id={sent_message.id}, "
                    f"{opening_text!r}"
                )
            except Exception as e:
                print(
                    f"[ERROR] Не удалось открыть новую тему "
                    f"в группе '{group_state.title}': {e}"
                )

    return handler


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
        print(f"[OK] Аккаунт готов: {account.label:<13} (id={account.me_id:<11})")

    admin_accounts = [a for a in accounts if a.is_admin]
    participants = [a for a in accounts if not a.is_admin]

    if not participants:
        raise RuntimeError(
            "Нет ни одного аккаунта-участника: все найденные аккаунты помечены как admin."
        )

    if admin_accounts:
        for admin in admin_accounts:
            print(f"[OK] {admin.label}: админ, исключён из общения — только слушает ЛС")

    group_map: dict[int, GroupState] = {}
    initial_theme = load_theme()

    raw_group_map = await resolve_all_groups(participants[0].client, groups_config)
    for chat_id, info in raw_group_map.items():
        group_map[chat_id] = GroupState(
            title=info["title"],
            prompt=info["prompt"],
            theme=initial_theme,
        )
        print(
            f"[OK] Слушаю группу: {info['title']} (id={chat_id}), "
            f"начальная тема: {initial_theme!r}"
        )

    def account_by_id(user_id: int) -> Optional[Account]:
        return next((a for a in participants if a.me_id == user_id), None)

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

            other_accounts = [a for a in participants if a.me_id != sender_id]
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

                recently_excluded_ids = set(group_state.recent_speakers) | {sender_id}
                candidates = [a for a in other_accounts if a.me_id not in recently_excluded_ids]
                target_account = random.choice(candidates or other_accounts)
                should_reply_as_mention = False

            try:
                # Snapshot the topic state for this particular generation.
                # If an admin changes the topic while the LLM is working,
                # the generated answer becomes stale and will not be sent.
                theme = group_state.theme
                theme_version = group_state.theme_version
                theme_started_message_id = group_state.theme_started_message_id

                context = await collect_context(
                    event,
                    limit=10,
                    min_message_id=theme_started_message_id,
                )
                context_trigger = context["trigger"]
                context_history = context["messages"]

                print(
                    f"[THEME] [{group_state.title}] "
                    f"version={theme_version}, "
                    f"start_message_id={theme_started_message_id}, "
                    f"{theme!r}"
                )

                llm_task = asyncio.create_task(
                    ask_llm(
                        context_trigger,
                        context_history,
                        group_state.prompt,
                        theme,
                    )
                )

                if min_cooldown or max_cooldown:
                    await asyncio.sleep(random.uniform(min_cooldown, max_cooldown))

                async with target_account.client.action(
                    event.chat_id,
                    'typing',
                ):  # type: ignore
                    reply_text = await llm_task
                    typing_time = min(max(len(reply_text) * 0.05, 1.5), 10)
                    await asyncio.sleep(typing_time)

                # The topic may have changed while the LLM was generating
                # or while the bot was simulating typing.
                if theme_version != group_state.theme_version:
                    print(
                        f"[SKIP] Устаревший ответ [{group_state.title}]: "
                        f"generated_version={theme_version}, "
                        f"current_version={group_state.theme_version}"
                    )
                    return

                if should_reply_as_mention:
                    await target_account.client.send_message(
                        event.chat_id,
                        reply_text,
                        reply_to=event.id,
                    )
                    kind = "REPLY"
                else:
                    await target_account.client.send_message(
                        event.chat_id,
                        reply_text,
                    )
                    kind = "TEXT"

                print(
                    f"[{kind}] [{group_state.title}] {target_account.label} "
                    f"(theme_version={theme_version}) -> {text!r} :: {reply_text!r}"
                )

                max_recent = max(1, min(2, len(participants) - 1))
                group_state.recent_speakers.append(target_account.me_id)
                group_state.recent_speakers = group_state.recent_speakers[-max_recent:]
            except Exception as e:
                print(f"[ERROR] Не удалось ответить ({target_account.label}): {e}")

    for account in participants:
        account.client.add_event_handler(handle_message, events.NewMessage())

    starter_account = random.choice(participants)
    for chat_id in group_map:
        await starter_account.client.send_message(chat_id, STARTER_MESSAGE)
        print(f"[START] {starter_account.label} -> {STARTER_MESSAGE!r}")

    for admin in admin_accounts:
        admin.client.add_event_handler(create_dm_handler(admin, group_map), events.NewMessage())

    print("[OK] Все боты запущены и слушают сообщения. Ctrl+C для остановки.")
    await asyncio.gather(*(a.client.run_until_disconnected() for a in accounts))  # type: ignore


if __name__ == "__main__":
    asyncio.run(main())