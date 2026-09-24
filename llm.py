import asyncio
import json
import re
from pathlib import Path

import requests


CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _groq_headers(config: dict) -> dict:
    return {
        "Authorization": f"Bearer {config['groq_api_key']}",
        "Content-Type": "application/json",
    }


async def ask_llm(text: str, group_prompt: str) -> str:
    config = load_config()

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = _groq_headers(config)

    data = {
        "model": "qwen/qwen3.8-27b",
        "messages": [
            {
                "role": "system",
                "content": group_prompt,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "temperature": 0,
        "max_tokens": 250
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


_ADDRESSEE_SYSTEM_PROMPT = """\
Ты — модуль анализа сообщений в групповом чате.
Тебе дают одну фразу. Определи, обращена ли она к конкретному человеку по имени
(например: "Алекес, как дела?", "@Vasya ты где", "Настя, привет, что нового").

Учитывай опечатки и искажения имени: если имя написано с ошибкой,
верни исправленный, наиболее вероятный вариант имени (например
"Алекес" -> "Алекс", "Alexx" -> "Alex").

Если сообщение НЕ адресовано конкретному человеку по имени (обычная фраза,
реплика в чат, вопрос без обращения к кому-то конкретному) — верни null.

Считай, что обращение может быть максимум к одному человеку.

Отвечай СТРОГО валидным JSON и ничем другим, без пояснений, без markdown:
{"name": "Alex"}
или
{"name": null}
"""


def _parse_addressee_response(content: str) -> str | None:
    """
    Разбирает ответ модели в поисках {"name": ...}. Устойчиво к тому,
    что модель может обернуть JSON в ```json ... ``` или добавить пробелы/текст вокруг.
    """
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


async def main():
    user_input = input("Введите текст для Groq: ")

    result = await ask_llm(
        user_input,
        group_prompt="Обычный разговор",
    )

    print("\nОтвет от Groq:")
    print(result)

    addressee = await detect_addressee(user_input)
    print(f"\nАдресат: {addressee!r}")


if __name__ == "__main__":
    asyncio.run(main())