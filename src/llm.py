import asyncio
import json
import re
from pathlib import Path

import requests

CURRENT_FILE = Path(__file__).resolve()
PROJECT_DIR = CURRENT_FILE.parent.parent

CONFIG_PATH = PROJECT_DIR / "config.json"
PROMPTS_DIR = PROJECT_DIR / "prompts"

_ADDRESSEE_SYSTEM_PROMPT = (PROMPTS_DIR / "addressee.txt").read_text(encoding="utf-8")
_HUMAN_PROMPT = (PROMPTS_DIR / "human.txt").read_text(encoding="utf-8")
_OPENING_SYSTEM_PROMPT = (PROMPTS_DIR / "opening.txt").read_text(encoding="utf-8")

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _groq_headers(config: dict) -> dict:
    return {
        "Authorization": f"Bearer {config['groq_api_key']}",
        "Content-Type": "application/json",
    }


def _format_history_item(item) -> str:
    if isinstance(item, str):
        return f"- {item}"
    sender = item.get("sender_id", "неизвестно") if isinstance(item, dict) else "неизвестно"
    body = item.get("text", "") if isinstance(item, dict) else str(item)
    return f"[{sender}]: {body}"


async def ask_llm(text: dict, context: list, group_prompt: str, first_name: str, last_name: str, theme_of_suggestion: str = "") -> str:

    config = load_config()

    context_str = "\n".join(_format_history_item(item) for item in context)

    trigger_text = text.get("text", "") if isinstance(text, dict) else str(text)
    trigger_sender = text.get("sender_id", "неизвестно") if isinstance(text, dict) else "неизвестно"

    theme_block = theme_of_suggestion.strip() if theme_of_suggestion else "Тема не задана — общайтесь свободно в рамках группы."

    full_name = f"{first_name} {last_name}".strip() or "Участник"

    full_system_prompt = (
        f"{_HUMAN_PROMPT}\n\n"
        f"--- ТВОЯ ЛИЧНОСТЬ И СТИЛЬ ---\n"
        f"Твоё имя: {full_name}.\n"
        f"ВНИМАНИЕ: Учитывай пол своего имени ({first_name}).\n"
        f"Если это женское имя (например, Madelyn, Anna, Kate, Emily, Sophia), СТРОГО используй женский род: «я смотрела», «я думала».\n"
        f"Если мужское (John, Alex, Max) — мужской: «я смотрел», «я думал».\n\n"
        f"--- ФОРМАТ И СТИЛЬ ОБЩЕНИЯ (КРИТИЧЕСКИ ВАЖНО) ---\n"
        f"1. ПИШИ КАК В TELEGRAM-ЧАТЕ: кратко, естественно, без книжных оборотов и умствований.\n"
        f"2. ДЛИНА: Строго от 1 до 3 коротких предложений! Не расписывай рецензии, анализы и списки.\n"
        f"3. Избегай канцелярита, академического тона («архитектурный проект», «декодирование смысла», «инверсия правды» — ТАК В ЧАТАХ НЕ ГОВОРЯТ).\n"
        f"4. Говори простыми словами, как будто на ходу набиваешь сообщение с телефона.\n\n"
        f"--- ИНСТРУКЦИЯ ГРУППЫ ---\n{group_prompt}\n\n"
        f"--- АКТИВНАЯ ТЕМА ОБСУЖДЕНИЯ ---\n"
        f"{theme_block}\n\n"
        f"--- ИСТОРИЯ ПЕРЕПИСКИ ---\n"
        f"{context_str if context_str else 'Истории пока нет — это начало разговора.'}\n\n"
        f"--- ЗАДАЧА ---\n"
        f"Напиши КРАТКИЙ разговорный ответ (1-3 предложения) на последнее сообщение. "
        f"Верни ТОЛЬКО текст ответа."
    )

    user_content = f"[{trigger_sender}]: {trigger_text}"

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = _groq_headers(config)

    data = {
        "model": "qwen/qwen3.8-27b",
        "messages": [
            {
                "role": "system",
                "content": full_system_prompt,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "temperature": 0.85,
        "max_tokens": 100
    }

    response = await asyncio.to_thread(
        requests.post,
        url,
        headers=headers,
        json=data,
        timeout=60,
    )

    if response.status_code == 200:
        response_data = response.json()
        return response_data["choices"][0]["message"]["content"]

    return f"Ошибка API: {response.status_code} — {response.text}"


async def generate_opening_message(theme: str, group_prompt: str) -> str:
    config = load_config()

    full_system_prompt = (
        f"{_OPENING_SYSTEM_PROMPT}\n\n"
        f"--- СТИЛЬ ---\n"
        f"ПИШИ КАК ОБЫЧНЫЙ ЧЕЛОВЕК В ЧАТЕ. Короткий вопрос или реплика (1-2 предложения). "
        f"Никакого лонгрида и разборов. Простой заброс темы в чат.\n\n"
        f"--- ИНСТРУКЦИЯ ГРУППЫ ---\n{group_prompt}\n\n"
        f"--- НОВАЯ ТЕМА ---\n{theme.strip()}"
    )

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = _groq_headers(config)

    data = {
        "model": "qwen/qwen3.8-27b",
        "messages": [
            {
                "role": "system",
                "content": full_system_prompt,
            },
            {
                "role": "user",
                "content": theme.strip(),
            },
        ],
        "temperature": 0.85,
        "max_tokens": 80,
    }

    response = await asyncio.to_thread(
        requests.post,
        url,
        headers=headers,
        json=data,
        timeout=60,
    )

    if response.status_code == 200:
        response_data = response.json()
        return response_data["choices"][0]["message"]["content"]

    return f"Ошибка API: {response.status_code} — {response.text}"


def _parse_addressee_response(content: str) -> str | None:
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    name = parsed.get("name")
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


async def detect_addressee(text: str) -> str | None:
    config = load_config()

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = _groq_headers(config)

    data = {
        "model": "qwen/qwen3.8-27b",
        "messages": [
            {"role": "system", "content": _ADDRESSEE_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 30,
    }

    response = await asyncio.to_thread(
        requests.post,
        url,
        headers=headers,
        json=data,
        timeout=30,
    )

    if response.status_code != 200:
        print(f"[ERROR] detect_addressee: {response.status_code} — {response.text}")
        return None

    content = response.json()["choices"][0]["message"]["content"]
    return _parse_addressee_response(content)