# bot.py
# Telegram -> RunPod bridge
# Требует: python-telegram-bot==21.6, requests==2.32.3, python-dotenv (опционально)

import json
import logging
import os
from typing import Any, Dict

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# --- конфиг и логи ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pixorbi-bot")

# Поддержка .env (локально). На Render переменные берутся из Dashboard.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
# ВАЖНО: используем /runsync для мгновенного ответа
RUNPOD_ENDPOINT_URL = os.getenv("RUNPOD_ENDPOINT_URL", "").rstrip("/") + "/runsync"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not RUNPOD_API_KEY:
    raise RuntimeError("RUNPOD_API_KEY is not set")
if "runsync" not in RUNPOD_ENDPOINT_URL:
    raise RuntimeError("RUNPOD_ENDPOINT_URL должен оканчиваться на /runsync")

# --- утилита вызова RunPod ---
def call_runpod(payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(RUNPOD_ENDPOINT_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    # Ответ /runsync обычно {"id": "...", "output": {...}, "status": "COMPLETED"}
    if isinstance(data, dict) and "output" in data:
        return data["output"]
    return data if isinstance(data, dict) else {"raw": data}

# --- handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "Привет! Я подключён к RunPod.\n"
        "Напиши мне любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Для теста напиши, например: Анна"
    )
    await update.message.reply_text(txt)

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    user_id = str(update.effective_user.id)

    # Отправляем НОВЫЙ формат, который ждёт handler.py:
    # {"input": {"user_id", "character", "text"}}
    payload = {
        "input": {
            "user_id": user_id,
            "character": "anna",     # временно фиксируем Анну; позже сделаем переключение
            "text": user_text,
        }
    }

    try:
        rp = call_runpod(payload)

        # handler.py возвращает {"ok": True, "character": "...", "user_id": "...", "reply": "..."}
        msg = None
        if isinstance(rp, dict):
            msg = rp.get("reply") or rp.get("msg") or rp.get("message")
        if not msg:
            # на всякий случай покажем, что пришло
            msg = json.dumps(rp, ensure_ascii=False)[:1000]

        await update.message.reply_text(msg)
    except Exception as e:
        log.exception("RunPod error")
        await update.message.reply_text(f"Упс… ошибка сервера: {e}")

def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot is running…")
    # Без asyncio.run — чтобы не ловить конфликт event loop
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
