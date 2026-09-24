import asyncio
import json
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.messages import ImportChatInviteRequest

KEYS_DIR = Path(__file__).with_name("keys")
SESSIONS_DIR = Path(__file__).with_name("sessions")

GROUP_INVITE_LINK = "https://t.me/+qh8IbglsZLU0MDEy"
GROUP_ID = -5107735777

DELAY_BETWEEN_ACCOUNTS = 5


def _extract_invite_hash(link: str) -> str:
    link = link.strip()
    if "joinchat/" in link:
        return link.split("joinchat/")[-1]
    if "/+" in link:
        return link.split("/+")[-1]
    return link.rstrip("/").split("/")[-1]


def load_accounts() -> list[dict]:
    accounts = []
    for json_path in sorted(KEYS_DIR.glob("*.json")):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        is_admin = str(data.get("admin", "")).strip().lower() == "yes"

        accounts.append({
            "app_id": data["app_id"],
            "app_hash": data["app_hash"],
            "session_file": data["session_file"],
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
            "is_admin": is_admin,
            "source_file": json_path.name,
        })
    return accounts


async def join_group(client: TelegramClient, label: str) -> None:
    invite_hash = _extract_invite_hash(GROUP_INVITE_LINK)
    try:
        await client(ImportChatInviteRequest(invite_hash))
        print(f"[OK]    {label}: вступил в группу")
    except UserAlreadyParticipantError:
        print(f"[SKIP]  {label}: уже состоит в группе")
    except (InviteHashExpiredError, InviteHashInvalidError) as e:
        print(f"[ERROR] {label}: инвайт-ссылка недействительна ({e})")
    except FloodWaitError as e:
        print(f"[ERROR] {label}: флуд-контроль, нужно подождать {e.seconds} сек")
    except Exception as e:
        print(f"[ERROR] {label}: не удалось вступить — {e}")


async def process_account(account: dict) -> None:
    label = f"{account['first_name']} {account['last_name']}".strip() or account["session_file"]
    if account["is_admin"]:
        label += " [admin]"

    session_path = SESSIONS_DIR / account["session_file"]

    if not session_path.with_suffix(".session").exists():
        print(f"[ERROR] {label}: файл сессии не найден: {session_path.with_suffix('.session')}")
        return

    client = TelegramClient(str(session_path), account["app_id"], account["app_hash"])

    await client.connect()

    if not await client.is_user_authorized():
        print(f"[ERROR] {label}: сессия не авторизована (см. {account['source_file']}), пропускаю")
        await client.disconnect()
        return

    await join_group(client, label)

    await client.disconnect()


async def main():
    accounts = load_accounts()
    if not accounts:
        print(f"[ERROR] В папке '{KEYS_DIR}' не найдено ни одного .json файла")
        return

    print(f"[OK] Найдено аккаунтов: {len(accounts)}")

    for i, account in enumerate(accounts):
        await process_account(account)
        if i != len(accounts) - 1:
            await asyncio.sleep(DELAY_BETWEEN_ACCOUNTS)

    print("[OK] Готово.")


if __name__ == "__main__":
    asyncio.run(main())