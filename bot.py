# bot.py — устойчивый старт и диагностика окружения
import os
import json
import logging
from functools import partial

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------- Логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("pixorbi-bot")

# ---------- ENV ----------
load_dotenv()  # не перезаписывает уже выставленные переменные Render’ом

def read_env(*names: str, default: str = "") -> str:
    """
    Возвращает первое найденное значение из перечисленных имён переменных.
    Чистит кавычки/пробелы/переносы. Не падает, если переменной нет.
    """
    for name in names:
        val = os.getenv(name)
        if val is not None:
            # убираем кавычки, пробелы и \n
            cleaned = val.strip().strip('"').strip("'")
            if cleaned:
                return cleaned
    return default

TELEGRAM_TOKEN = read_env("TELEGRAM_TOKEN", "BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
RUNPOD_ENDPOINT_URL = read_env("RUNPOD_ENDPOINT_URL")
OPENROUTER_API_KEY  = read_env("OPENROUTER_API_KEY")
OPENROUTER_MODEL    = read_env("OPENROUTER_MODEL", default="meta-llama/llama-3.1-70b-instruct")

# Диагностика: покажем какие ключи присутствуют (без утечек секретов)
def mask(s: str) -> str:
    if not s: return "∅"
    if len(s) <= 8: return "****"
    return s[:4] + "…" + s[-4:]

log.info(
    "ENV check | TELEGRAM_TOKEN=%s | RUNPOD_ENDPOINT_URL=%s | OPENROUTER_MODEL=%s | OPENROUTER_API_KEY=%s",
    mask(TELEGRAM_TOKEN), RUNPOD_ENDPOINT_URL or "∅", OPENROUTER_MODEL or "∅", mask(OPENROUTER_API_KEY)
)

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN is required. "
        "Проверь в Render → Settings → Environment, что ключ называется именно "
        "`TELEGRAM_TOKEN` (или `BOT_TOKEN`/`TELEGRAM_BOT_TOKEN`) и нет лишних кавычек/пробелов."
    )

# ---------- Простейшая «эхо»-логика через RunPod (как было) ----------
def call_runpod_sync(payload: dict) -> dict:
    if not RUNPOD_ENDPOINT_URL:
        # нет эндпоинта — вернём заглушку, чтобы бот всё равно отвечал
        return {"ok": True, "reply": f"Anna: я услышала тебя — «{payload.get('text','')}»."}

    url = RUNPOD_ENDPOINT_URL.rstrip("/")  # принимаем и /run или полный /runsync
    # поддержим оба варианта: raw /run (Serverless) и /runsync
    if url.endswith("/run"):
        api_url = url
    elif url.endswith("/runsync"):
        api_url = url
    else:
        # если дали базовый endpoint без хвоста — используем /run (быстрее/дешевле)
        api_url = url + "/run"

    headers = {"Content-Type": "application/json"}
    resp = requests.post(api_url, headers=headers, data=json.dumps({"input": payload}), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Унифицируем ответ
    if "output" in data and isinstance(data["output"], dict):
        return data["output"]
    return data

# ---------- Telegram handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я подключён к RunPod.\n"
        "Напиши любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Команды:\n"
        "  /char — показать текущего персонажа\n"
        "  /char <имя> — выбрать персонажа (пример: /char anna)\n\n"
        "Для теста напиши: Анна"
    )
    await update.message.reply_text(text)

async def cmd_char(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = "character"
    if context.args:
        context.user_data[key] = context.args[0].strip().lower()
    char = context.user_data.get(key, "anna")
    await update.message.reply_text(f"Текущий персонаж: {char.capitalize()}")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = (update.message.text or "").strip()
    char = context.user_data.get("character", "anna")

    payload = {
        "user_id": str(update.effective_user.id),
        "character": char,
        "text": user_text,
    }

    try:
        output = await context.application.run_in_executor(
            None, partial(call_runpod_sync, payload)
        )
        reply = output.get("reply") or output.get("msg") or "…"
        await update.message.reply_text(reply)
    except Exception as e:
        log.exception("RunPod error")
        await update.message.reply_text(f"Упс… ошибка сервера: {e}")

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app

def main():
    log.info("Bot is running…")
    app = build_app()
    # на Render это Background Worker → polling ок
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
