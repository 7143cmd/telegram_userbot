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

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _groq_headers(config: dict) -> dict:
    return {
        "Authorization": f"Bearer {config['groq_api_key']}",
        "Content-Type": "application/json",
    }


async def ask_llm(text: dict, context: list, group_prompt: str) -> str:

    config = load_config()
    context_str = "\n".join(
        [f"- {item}" if isinstance(item, str) else json.dumps(item, ensure_ascii=False) for item in context]
    )

    text_str = json.dumps(text, ensure_ascii=False, indent=2)

    full_system_prompt = (
        f"{_HUMAN_PROMPT}\n\n"
        f"--- ИНСТРУКЦИЯ ГРУППЫ ---\n{group_prompt}\n\n"
        f"--- КОНТЕКСТ ---\n{context_str if context_str else 'Контекст отсутствует.'}"
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
                "content": text_str,
            },
        ],
        "temperature": 0.6,
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


# async def main():
#     user_input = input("Введите текст для Groq: ")

#     result = await ask_llm(
#         user_input,
#         group_prompt="Обычный разговор",
#     )

#     print("\nОтвет от Groq:")
#     print(result)

#     addressee = await detect_addressee(user_input)
#     print(f"\nАдресат: {addressee!r}")


# if __name__ == "__main__":
#     asyncio.run(main())