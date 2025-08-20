# bot.py — Telegram → RunPod /runsync

import asyncio
import json
import logging
import os
from typing import Dict, Any

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pixorbi-bot")

# ---------- .ENV ----------
load_dotenv()  # файл должен называться .env и лежать рядом с bot.py

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_URL = os.getenv("RUNPOD_ENDPOINT_URL")  # https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync

# Проверка переменных окружения
missing = [k for k, v in {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "RUNPOD_API_KEY": RUNPOD_API_KEY,
    "RUNPOD_ENDPOINT_URL": RUNPOD_ENDPOINT_URL
}.items() if not v]
if missing:
    raise SystemExit(f"❌ Не заданы переменные в .env: {', '.join(missing)}")

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
def call_runpod(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Синхронный вызов RunPod /runsync с простейшим ретраем."""
    headers = {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }
    last_err = None
    for _ in range(2):  # небольшой ретрай на случай сетевого сбоя
        try:
            r = requests.post(RUNPOD_ENDPOINT_URL, headers=headers, json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "output" in data:
                return data["output"]
            return data if isinstance(data, dict) else {"message": str(data)}
        except Exception as e:
            last_err = e
            continue
    raise last_err

def extract_message_text(output: Dict[str, Any]) -> str:
    """Достаём текст из ответа handler'а."""
    if isinstance(output, dict):
        return (
            output.get("msg")
            or output.get("reply")
            or output.get("message")
            or json.dumps(output, ensure_ascii=False)[:1000]
        )
    return str(output)

# ---------- HANDLERS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я подключён к RunPod.\n"
        "Напиши мне любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Для теста напиши, например: Анна"
    )

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()

    # Сейчас твой handler ожидает именно {"input":{"name": "..."}}
    payload = {"input": {"name": user_text}}

    try:
        output = await asyncio.to_thread(call_runpod, payload)
        reply_text = extract_message_text(output)
        await update.message.reply_text(reply_text)
    except requests.HTTPError as http_err:
        log.exception("RunPod HTTP error")
        txt = f"Ошибка RunPod: {http_err.response.status_code} {http_err.response.text[:200]}"
        await update.message.reply_text(txt)
    except Exception as e:
        log.exception("RunPod error")
        await update.message.reply_text(f"Упс… ошибка сервера: {e}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error in bot", exc_info=context.error)

# ---------- ЗАПУСК ----------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    log.info("Bot is running…")
    app.run_polling()  # без asyncio.run()

if __name__ == "__main__":
    main()
