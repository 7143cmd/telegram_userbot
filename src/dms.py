import asyncio
import json
from pathlib import Path
from telethon import TelegramClient, events
from userbot import Account

PROJECT_DIR = Path(__file__).resolve().parent.parent
KEYS_DIR = PROJECT_DIR / "keys"
SESSIONS_DIR = PROJECT_DIR / "sessions"

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def load_admin_accounts() -> list[Account]:
    admin_accounts: list[Account] = []

    if not KEYS_DIR.exists():
        print(f"[!] Папка {KEYS_DIR} не найдена")
        return admin_accounts

    for json_file in sorted(KEYS_DIR.glob("*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                continue

            admin_val = str(data.get("admin", "")).strip().lower()
            is_admin = admin_val in ("yes", "true", "1")

            if is_admin:
                admin_accounts.append(
                    Account(
                        app_id=data["app_id"],
                        app_hash=data["app_hash"],
                        session_file=data["session_file"],
                        first_name=data.get("first_name", ""),
                        last_name=data.get("last_name", ""),
                        is_admin=True,
                        source_file=json_file.name,
                    )
                )
        except Exception as e:
            print(f"[!] Ошибка чтения файла {json_file.name}: {e}")

    return admin_accounts


async def main():
    admin_accounts = load_admin_accounts()

    if not admin_accounts:
        print("[!] Не найдено ни одного аккаунта с admin = yes")
        return

    print(f"[+] Найдено админ-аккаунтов: {len(admin_accounts)}")

    for acc in admin_accounts:
        session_name = Path(acc.session_file).stem
        session_path = SESSIONS_DIR / session_name

        acc.client = TelegramClient(str(session_path), acc.app_id, acc.app_hash)
        await acc.client.start()  # type: ignore

        me = await acc.client.get_me()
        acc.me_id = me.id
        print(f"[OK] Авторизован админ: {acc.name} (id={acc.me_id})")

    def create_dm_handler(account: Account):
        async def handler(event: events.NewMessage.Event):
            if event.is_private and not event.out:
                sender = await event.get_sender()

                first_name = getattr(sender, "first_name", "") or ""
                last_name = getattr(sender, "last_name", "") or ""
                sender_name = f"{first_name} {last_name}".strip() or "Неизвестный"

                text = event.raw_text or ""
                print(f"DM: {account.name} <- {sender_name}: {text}")

        return handler

    for acc in admin_accounts:
        acc.client.add_event_handler(create_dm_handler(acc), events.NewMessage())

    print("\n[OK] Начинаем прослушивание личных сообщений. Нажмите Ctrl+C для выхода.")
    await asyncio.gather(*(acc.client.run_until_disconnected() for acc in admin_accounts))  # type: ignore


if __name__ == "__main__":
    asyncio.run(main())