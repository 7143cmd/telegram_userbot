import asyncio
import json
from pathlib import Path

import requests


CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


async def ask_llm(text: str, group_prompt: str) -> str:
    config = load_config()

    groq_api_key = config["groq_api_key"]

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json",
    }

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


async def main():
    user_input = input("Введите текст для Groq: ")

    result = await ask_llm(
        user_input,
        group_prompt="Обычный разговор",
    )

    print("\nОтвет от Groq:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())