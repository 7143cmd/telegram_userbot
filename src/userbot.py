import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from datetime import timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events, utils

from llm import ask_llm, detect_addressee, generate_opening_message

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = BASE_DIR / "config.json"
KEYS_DIR = BASE_DIR / "keys"
SESSIONS_DIR = BASE_DIR / "sessions"


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


_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _normalize_name(name: str) -> str:
    lowered = name.strip().lower()
    translit = "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in lowered)
    return translit.replace("ks", "x")


def _names_match(detected: str, trigger_name: str) -> bool:
    a, b = _normalize_name(detected), _normalize_name(trigger_name)
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.75


async def collect_context(event, topic_start_id: Optional[int] = None, limit: int = 10) -> dict:
    trigger = {
        "id": event.id,
        "sender_id": event.sender_id,
        "text": event.raw_text or "",
    }

    messages = []

    async for message in event.client.iter_messages(
        event.chat_id,
        limit=limit + 1,
    ):
        if message.id == event.id:
            continue

        if topic_start_id is not None and message.id < topic_start_id:
            break

        messages.append({
            "id": message.id,
            "sender_id": message.sender_id,
            "text": message.text,
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
    current_topic_start_id: Optional[int] = None


async def trigger_next_bot_response(chat_id: int, group_state: GroupState, participants: list[Account], trigger_msg, min_cooldown: int, max_cooldown: int):
    """Вспомогательная функция генерирует и отправляет ответ следующего случайного бота."""
    msg_sender_id = trigger_msg.sender_id
    other_accounts = [a for a in participants if a.me_id != msg_sender_id]
    if not other_accounts:
        return

    recently_excluded = set(group_state.recent_speakers) | {msg_sender_id}
    candidates = [a for a in other_accounts if a.me_id not in recently_excluded]
    target_account = random.choice(candidates or other_accounts)

    try:
        context = await collect_context(trigger_msg, topic_start_id=group_state.current_topic_start_id, limit=10)
        theme = load_theme()

        llm_task = asyncio.create_task(
            ask_llm(
                context["trigger"],
                context["messages"],
                group_state.prompt,
                target_account.first_name,
                target_account.last_name,
                theme,
            )
        )

        if min_cooldown or max_cooldown:
            await asyncio.sleep(random.uniform(min_cooldown, max_cooldown))

        async with target_account.client.action(chat_id, 'typing'):
            reply_text = await llm_task
            typing_time = min(max(len(reply_text) * 0.05, 1.5), 8)
            await asyncio.sleep(typing_time)

        sent_msg = await target_account.client.send_message(chat_id, reply_text)

        if sent_msg:
            group_state.last_handled_message_id = sent_msg.id

        print(f"[TEXT] [{group_state.title}] {target_account.label} :: {reply_text!r}")

        max_recent = max(1, min(2, len(participants) - 1))
        group_state.recent_speakers.append(target_account.me_id)
        group_state.recent_speakers = group_state.recent_speakers[-max_recent:]

    except Exception as e:
        print(f"[ERROR] Не удалось ответить ({target_account.label}): {e}")


def create_dm_handler(admin_account: Account, participants: list[Account], group_map: dict[int, GroupState], startup_cutoff: float, min_cooldown: int, max_cooldown: int):
    async def handler(event: events.NewMessage.Event):
        if not (event.is_private and not event.out):
            return

        if event.date and event.date.timestamp() < startup_cutoff:
            return

        sender = await event.get_sender()
        first_name = getattr(sender, "first_name", "") or ""
        last_name = getattr(sender, "last_name", "") or ""
        sender_name = f"{first_name} {last_name}".strip() or "Неизвестный"

        text = (event.raw_text or "").strip()
        print(f"[DM] {admin_account.name} <- {sender_name}: {text}")

        if not text:
            return

        try:
            save_theme(text)
            print(f"[THEME UPDATED] {text!r}")
        except Exception as e:
            print(f"[ERROR] Не удалось сохранить новую тему: {e}")
            return

        for chat_id, group_state in group_map.items():
            try:
                async with group_state.lock:
                    sender_bot = random.choice(participants)
                    opening_text = await generate_opening_message(text, group_state.prompt)
                    sent_msg = await sender_bot.client.send_message(chat_id, opening_text)
                    
                    if sent_msg:
                        group_state.current_topic_start_id = sent_msg.id
                        group_state.last_handled_message_id = sent_msg.id
                        group_state.recent_speakers = [sender_bot.me_id]

                    print(f"[OPEN] {sender_bot.label} -> [{group_state.title}] {opening_text!r}")
                    
                    # ПРИНУДИТЕЛЬНО вызываем первого респондента на новое сообщение!
                    await trigger_next_bot_response(chat_id, group_state, participants, sent_msg, min_cooldown, max_cooldown)

            except Exception as e:
                print(f"[ERROR] Не удалось открыть новую тему в группе '{group_state.title}': {e}")

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
        session_path = SESSIONS_DIR / account.session_file
        account.client = TelegramClient(str(session_path), account.app_id, account.app_hash)
        await account.client.start()  # type: ignore
        me = await account.client.get_me()
        account.me_id = me.id
        print(f"[OK] Аккаунт готов: {account.label} (id={account.me_id})")

    admin_accounts = [a for a in accounts if a.is_admin]
    participants = [a for a in accounts if not a.is_admin]

    if not participants:
        raise RuntimeError("Нет ни одного аккаунта-участника.")

    if admin_accounts:
        for admin in admin_accounts:
            print(f"[OK] {admin.label}: админ, исключён из общения — только слушает ЛС")

    print(f"[OK] Участников группы: {len(participants)}")
    for p in participants:
        print(f"[OK]   - {p.label} (me_id={p.me_id}, session={p.session_file}, source={p.source_file})")

    group_map: dict[int, GroupState] = {}
    raw_group_map = await resolve_all_groups(participants[0].client, groups_config)
    for chat_id, info in raw_group_map.items():
        group_map[chat_id] = GroupState(title=info["title"], prompt=info["prompt"])
        print(f"[OK] Слушаю группу: {info['title']} (id={chat_id})")

    server_timestamps = []
    client_for_check = participants[0].client
    for chat_id in group_map:
        try:
            async for msg in client_for_check.iter_messages(chat_id, limit=1):
                if msg and msg.date:
                    server_timestamps.append(msg.date.timestamp())
        except Exception as e:
            print(f"[WARN] Не удалось получить время последнего сообщения из {chat_id}: {e}")

    startup_cutoff = max(server_timestamps) if server_timestamps else time.time()
    print(f"[OK] Точка отсечки времени (startup_cutoff): {startup_cutoff}")

    def account_by_id(user_id: int) -> Optional[Account]:
        return next((a for a in participants if a.me_id == user_id), None)

    async def respond_to_human_directly(account: Account, event, group_state):
        try:
            print(f"[DIRECT TASK] {account.label} выполняет персональный ответ человеку на event.id={event.id}")
            context = await collect_context(event, topic_start_id=group_state.current_topic_start_id, limit=10)
            theme = load_theme()

            reply_text = await ask_llm(
                context["trigger"],
                context["messages"],
                group_state.prompt,
                account.first_name,
                account.last_name,
                theme,
            )

            async with account.client.action(event.chat_id, 'typing'):
                typing_time = min(max(len(reply_text) * 0.05, 1.5), 6)
                await asyncio.sleep(typing_time)

            await account.client.send_message(
                event.chat_id,
                reply_text,
                reply_to=event.id
            )
            print(f"[REPLY-HUMAN] [{group_state.title}] {account.label} -> {event.raw_text!r} :: {reply_text!r}")

        except Exception as e:
            print(f"[ERROR] Ошибка в respond_to_human_directly ({account.label}): {e}")

    def create_message_handler(account: Account):
        async def handle_message(event):
            group_state = group_map.get(event.chat_id)
            if group_state is None:
                return

            if event.sender_id == account.me_id:
                return

            if event.date and event.date.timestamp() < startup_cutoff:
                return

            sender_id = event.sender_id
            is_human = account_by_id(sender_id) is None

            if is_human:
                target_bot_for_human: Optional[Account] = None
                text = event.raw_text or ""

                if event.is_reply:
                    try:
                        replied_msg = await event.get_reply_message()
                        if replied_msg:
                            target_bot_for_human = next(
                                (a for a in participants if a.me_id == replied_msg.sender_id), None
                            )
                    except Exception as e:
                        print(f"[ERROR] Ошибка get_reply_message: {e}")

                if target_bot_for_human is None:
                    try:
                        addressee = await detect_addressee(text)
                        if addressee:
                            target_bot_for_human = _find_addressed_account(addressee, participants)
                    except Exception as e:
                        print(f"[ERROR] Ошибка detect_addressee: {e}")

                if target_bot_for_human is not None:
                    if account.me_id == target_bot_for_human.me_id:
                        asyncio.create_task(
                            respond_to_human_directly(account, event, group_state)
                        )
                    return

            async with group_state.lock:
                latest_messages = await account.client.get_messages(event.chat_id, limit=1)
                if not latest_messages:
                    return

                latest_msg = latest_messages[0]

                if latest_msg.sender_id == account.me_id:
                    return

                if group_state.last_handled_message_id == latest_msg.id:
                    return

                if group_state.current_topic_start_id and latest_msg.id < group_state.current_topic_start_id:
                    return

                group_state.last_handled_message_id = latest_msg.id

                msg_text = latest_msg.raw_text or ""
                msg_sender_id = latest_msg.sender_id
                msg_sender_acc = account_by_id(msg_sender_id)
                sender_label = msg_sender_acc.label if msg_sender_acc else str(msg_sender_id)

                print(f"[DEBUG] [{group_state.title}] Ответ на сообщение от {sender_label}: {msg_text!r}")

                await trigger_next_bot_response(event.chat_id, group_state, participants, latest_msg, min_cooldown, max_cooldown)

        return handle_message

    for account in participants:
        account.client.add_event_handler(create_message_handler(account), events.NewMessage())

    for admin in admin_accounts:
        admin.client.add_event_handler(create_dm_handler(admin, participants, group_map, startup_cutoff, min_cooldown, max_cooldown), events.NewMessage())

    starter_account = random.choice(participants)
    current_theme = load_theme()

    # Начальный старт при запуске
    for chat_id, group_state in group_map.items():
        try:
            async with group_state.lock:
                starter_message = await generate_opening_message(current_theme, group_state.prompt)
                sent_msg = await starter_account.client.send_message(chat_id, starter_message)
                if sent_msg:
                    group_state.current_topic_start_id = sent_msg.id
                    group_state.last_handled_message_id = sent_msg.id
                    group_state.recent_speakers = [starter_account.me_id]

                print(f"[START] {starter_account.label} -> [{group_state.title}] {starter_message!r}")

                # ГЕНЕРИРУЕМ ПЕРВЫЙ ОТВЕТ НА СТАРТОВОЕ СООБЩЕНИЕ
                await trigger_next_bot_response(chat_id, group_state, participants, sent_msg, min_cooldown, max_cooldown)

        except Exception as e:
            print(f"[ERROR] Не удалось сгенерировать стартовое сообщение для группы '{group_state.title}': {e}")

    print("[OK] Все боты запущены и слушают сообщения. Ctrl+C для остановки.")
    await asyncio.gather(*(a.client.run_until_disconnected() for a in accounts))  # type: ignore


if __name__ == "__main__":
    asyncio.run(main())