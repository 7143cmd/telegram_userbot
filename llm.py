import requests
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
    
async def ask_llm(text, group_prompt):
    config = load_config()

    GROQ_API_KEY = config["groq_api_key"]

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "qwen/qwen3.8-27b",
        "messages": [
            {"role": "system", "content": group_prompt},
            {"role": "user", "content": text}
        ],
        "temperature": 0
    }

    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        return f"Ошибка API: {response.status_code} — {response.text}"


# if __name__ == "__main__":
#     user_input = input("Введите текст для Groq: ")
#     result = ask_llm(user_input)
#     print("\nОтвет от Groq:")
#     print(result)