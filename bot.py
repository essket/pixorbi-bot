# bot.py
# -*- coding: utf-8 -*-

import os
import json
import asyncio
import logging
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

# ---------- загрузка переменных окружения ----------
# .env должен лежать рядом с bot.py локально; на Render переменные задаются в Settings → Environment
load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "").strip()
RUNPOD_ENDPOINT_URL = os.environ.get("RUNPOD_ENDPOINT_URL", "").strip()

# ---------- логирование ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pixorbi-bot")

# ---------- утилита: нормализация URL RunPod ----------
def _make_runsync_url(base: str) -> str:
    """
    Принимает базу вида:
      - https://api.runpod.ai/v2/<endpointId>
      - или с суффиксом /run или /runsync
    Возвращает корректный URL, по которому можно POST'ить.
    """
    if not base:
        raise RuntimeError("RUNPOD_ENDPOINT_URL is empty")

    base = base.strip().rstrip("/")  # срезаем хвостовой /
    # если админ случайно вписал /run или /runsync — используем как есть
    if base.endswith("/run") or base.endswith("/runsync"):
        return base
    # иначе добавим /runsync
    return f"{base}/runsync"

RUNPOD_URL_POST = _make_runsync_url(RUNPOD_ENDPOINT_URL)

# ---------- вызов RunPod ----------
def call_runpod(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Синхронный POST на RunPod (через /runsync или /run).
    Возвращает словарь. Если RunPod вернул {"output": {...}}, то достаём "output".
    """
    headers = {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(RUNPOD_URL_POST, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "output" in data:
        return data["output"]
    return data

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я подключён к RunPod.\n"
        "Напиши мне любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Для теста напиши, например: Анна"
    )
    await update.message.reply_text(text)

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    user_id = str(update.effective_user.id)

    # простая заготовка: пока используем одного персонажа "anna"
    payload = {
        "input": {
            "user_id": user_id,
            "character": "anna",
            "text": user_text,
        }
    }

    try:
        # т.к. requests блокирующий — уводим в отдельный поток
        rp = await asyncio.to_thread(call_runpod, payload)

        # максимально мягко достаём текст ответа
        msg = None
        if isinstance(rp, dict):
            # поддержим разные варианты ключей
            msg = rp.get("reply") or rp.get("msg") or rp.get("message")
        if not msg:
            # если структура иная — вернём «как есть», но не больше 1000 символов
            msg = json.dumps(rp, ensure_ascii=False)[:1000]

        await update.message.reply_text(msg)
    except Exception as e:
        log.exception("RunPod error")
        await update.message.reply_text(f"Упс… ошибка сервера: {e}")

# ---------- main ----------
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot is running…")
    # В 21.6 корректно без asyncio.run()
    app.run_polling()

if __name__ == "__main__":
    main()
