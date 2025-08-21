# bot.py
# -*- coding: utf-8 -*-

import os, json, asyncio, logging
from typing import Dict, Any
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
RUNPOD_API_KEY    = os.environ.get("RUNPOD_API_KEY", "").strip()
RUNPOD_ENDPOINT_URL = os.environ.get("RUNPOD_ENDPOINT_URL", "").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pixorbi-bot")

# in‑memory выбор персонажа (переживает диалог, но не перезапуск процесса)
user_character: Dict[int, str] = {}

def _make_runsync_url(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if base.endswith("/run") or base.endswith("/runsync"):
        return base
    return f"{base}/runsync"

RUNPOD_URL_POST = _make_runsync_url(RUNPOD_ENDPOINT_URL)

def call_runpod(payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
    r = requests.post(RUNPOD_URL_POST, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data.get("output", data) if isinstance(data, dict) else data

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if context.args:
        # сохраняем выбор
        chosen = context.args[0].lower()
        user_character[uid] = chosen
        await update.message.reply_text(f"Персонаж установлен: {chosen}")
        return
    current = user_character.get(uid, "anna")
    await update.message.reply_text(f"Текущий персонаж: {current}\n"
                                    f"Сменить: /char anna | /char bella | /char lucy (пример)")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_text = update.message.text.strip()
    uid = update.effective_user.id
    character = user_character.get(uid, "anna")

    payload = {"input": {"user_id": str(uid), "character": character, "text": user_text}}

    try:
        rp = await asyncio.to_thread(call_runpod, payload)
        msg = None
        if isinstance(rp, dict):
            msg = rp.get("reply") or rp.get("msg") or rp.get("message")
        if not msg:
            msg = json.dumps(rp, ensure_ascii=False)[:1000]
        await update.message.reply_text(msg)
    except Exception as e:
        log.exception("RunPod error")
        await update.message.reply_text(f"Упс… ошибка сервера: {e}")

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot is running…")
    app.run_polling()

if __name__ == "__main__":
    main()
