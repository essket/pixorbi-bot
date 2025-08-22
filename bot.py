import os
import json
import logging
import asyncio
from collections import defaultdict, deque

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------------- базовая настройка ----------------------
load_dotenv()  # читаем .env локально; на Render переменные берутся из Environment

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
RUNPOD_ENDPOINT_URL = os.getenv("RUNPOD_ENDPOINT_URL", "")  # например: https://api.runpod.ai/v2/<id>/runsync
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "")  # пример: meta-llama/llama-3.1-70b-instruct
OR_HTTP_REFERER = os.getenv("OR_HTTP_REFERER", "")
OR_X_TITLE = os.getenv("OR_X_TITLE", "Pixorbi Telegram Bot")

assert TELEGRAM_TOKEN, "TELEGRAM_TOKEN is required"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pixorbi-bot")

# персоны в стиле «визуальная новелла» — просто примеры
DEFAULT_CHARACTER = "anna"
CHAR_PRESETS = {
    "anna": {
        "name": "Anna",
        "style": "Тёплая, игривая, немного флиртует, отвечает живо и естественно.",
    },
    "mira": {
        "name": "Mira",
        "style": "Спокойная, загадочная, говорит коротко и по делу.",
    },
}

# выбранный персонаж и короткая история на пользователя
user_char: dict[int, str] = defaultdict(lambda: DEFAULT_CHARACTER)
user_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))  # храним последние 6 реплик


# ---------------------- OpenRouter: вызов модели ----------------------
def build_system_prompt(character_key: str) -> str:
    preset = CHAR_PRESETS.get(character_key, CHAR_PRESETS[DEFAULT_CHARACTER])
    return (
        f"Ты играешь роль персонажа визуальной новеллы.\n"
        f"Имя: {preset['name']}\n"
        f"Манера: {preset['style']}\n"
        f"Говори от первого лица, естественно. Сохраняй характер. "
        f"Избегай длинных лекций; отвечай 1–3 короткими абзацами."
    )

def _openrouter_sync_request(model: str, system_prompt: str, messages: list[dict]) -> str:
    """Синхронный HTTP-запрос к OpenRouter (вынесем в to_thread)."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OR_HTTP_REFERER:
        headers["HTTP-Referer"] = OR_HTTP_REFERER
    if OR_X_TITLE:
        headers["X-Title"] = OR_X_TITLE

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.9,
        "top_p": 0.95,
        "presence_penalty": 0.2,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()

async def call_openrouter(character_key: str, user_id: int, user_text: str) -> str:
    if not OPENROUTER_API_KEY or not OPENROUTER_MODEL:
        raise RuntimeError("OpenRouter is not configured")

    # готовим историю в формате OpenAI-like
    msgs = []
    # добавляем предыдущие реплики (user/assistant)
    for role, content in user_history[user_id]:
        msgs.append({"role": role, "content": content})
    # текущий ввод
    msgs.append({"role": "user", "content": user_text})

    system_prompt = build_system_prompt(character_key)
    # выполняем синхронный HTTP в отдельном потоке, чтобы не блокировать обработчик
    reply = await asyncio.to_thread(
        _openrouter_sync_request, OPENROUTER_MODEL, system_prompt, msgs
    )

    # обновляем историю
    user_history[user_id].append(("user", user_text))
    user_history[user_id].append(("assistant", reply))
    return reply


# ---------------------- RunPod: фолбэк-обработчик ----------------------
def _runpod_sync_request(payload: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if RUNPOD_API_KEY:
        headers["Authorization"] = f"Bearer {RUNPOD_API_KEY}"
    r = requests.post(RUNPOD_ENDPOINT_URL, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

async def call_runpod_echo(user_id: int, character_key: str, text: str) -> str:
    """Наш старый «эхо»-эндпойнт на RunPod: оставляем как резервный путь."""
    payload = {
        "input": {
            "user_id": str(user_id),
            "character": character_key,
            "text": text,
        }
    }
    data = await asyncio.to_thread(_runpod_sync_request, payload)
    # ожидаем либо output.reply, либо output.msg/ok от демо
    out = data.get("output", {})
    if "reply" in out:
        return out["reply"]
    if "msg" in out:
        name = CHAR_PRESETS.get(character_key, {}).get("name", character_key.title())
        return f"{name}: {out['msg']}"
    return "…сервер молчит. Попробуй ещё раз."


# ---------------------- Telegram handlers ----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я подключён к RunPod.\n"
        "Напиши любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Команды:\n"
        "  /char — показать текущего персонажа\n"
        "  /char <имя> — выбрать персонажа (пример: /char anna)\n\n"
        "Для теста напиши: Анна"
    )

async def cmd_char(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        cur = user_char[uid]
        preset = CHAR_PRESETS.get(cur, CHAR_PRESETS[DEFAULT_CHARACTER])
        await update.message.reply_text(f"Сейчас выбран персонаж: {preset['name']} (ключ: {cur})")
        return
    key = context.args[0].lower()
    if key not in CHAR_PRESETS:
        keys = ", ".join(CHAR_PRESETS.keys())
        await update.message.reply_text(f"Не знаю такого персонажа. Доступны: {keys}")
        return
    user_char[uid] = key
    user_history[uid].clear()
    preset = CHAR_PRESETS[key]
    await update.message.reply_text(f"Готово! Теперь с тобой говорит {preset['name']}.")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    text = update.message.text.strip()
    character = user_char[uid]

    try:
        if OPENROUTER_API_KEY and OPENROUTER_MODEL:
            reply = await call_openrouter(character, uid, text)
        else:
            # если OpenRouter не настроен — идём через RunPod-демо
            reply = await call_runpod_echo(uid, character, text)
    except requests.HTTPError as http_err:
        log.exception("HTTP error")
        await update.message.reply_text(f"Упс… HTTP ошибка: {http_err}")
        return
    except Exception as e:
        log.exception("Other error")
        await update.message.reply_text(f"Упс… ошибка сервера: {e}")
        return

    await update.message.reply_text(reply)


def main():
    log.info("Bot is starting…")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.run_polling()


if __name__ == "__main__":
    main()
