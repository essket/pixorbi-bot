# bot.py
# -*- coding: utf-8 -*-
import os
import logging
import httpx

from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("pixorbi-bot")

# ---------- ENV ----------
# ВАЖНО: имя переменной для токена — именно TELEGRAM_BOT_TOKEN
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# НОВОЕ: вместо полного URL используем ID эндпоинта
# его видно на странице RunPod Serverless, выглядит как ehcln4zeklsxdu
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

# Ключ API RunPod (обязательно)
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")

DEFAULT_CHAR = os.getenv("DEFAULT_CHARACTER", "anna").lower()
CHAR_KEY = "char"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required (Render → Environment)")
if not RUNPOD_ENDPOINT_ID:
    raise RuntimeError("RUNPOD_ENDPOINT_ID is required (Render → Environment)")
if not RUNPOD_API_KEY:
    raise RuntimeError("RUNPOD_API_KEY is required (Render → Environment)")

# Собираем правильный runsync URL:
RUNPOD_RUNSYNC_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/runsync"

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
async def delete_webhook(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (drop_pending_updates=True).")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

async def call_runpod(user_id: int, character: str, text: str) -> str:
    """Синхронный вызов Serverless через /runsync."""
    payload = {
        "input": {
            "user_id": str(user_id),
            "character": character,
            "text": text,
        }
    }
    headers = {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(RUNPOD_RUNSYNC_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Унифицируем поле с ответом
        output = data.get("output") if isinstance(data, dict) else data
        if isinstance(output, dict):
            if "reply" in output:
                return str(output["reply"])
            if "msg" in output:
                return str(output["msg"])
        return str(output)

    except httpx.HTTPStatusError as e:
        log.exception("RunPod HTTP error")
        return (
            f"Упс… ошибка сервера: {e.response.status_code} {e.response.reason_phrase}\n"
            f"{e.request.url}"
        )
    except Exception as e:
        log.exception("RunPod error")
        return f"Упс… ошибка сервера: {e}"

def get_user_char(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    char = ctx.user_data.get(CHAR_KEY)
    if not char:
        ctx.user_data[CHAR_KEY] = DEFAULT_CHAR
        char = DEFAULT_CHAR
    return char

# ---------- КОМАНДЫ ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.setdefault(CHAR_KEY, DEFAULT_CHAR)
    await update.message.reply_text(
        "Привет! Я подключён к RunPod.\n"
        "Напиши любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Команды:\n"
        "  /char — показать текущего персонажа\n"
        "  /char <имя> — выбрать персонажа (пример: /char anna)\n\n"
        "Для теста напиши: Анна"
    )

async def cmd_char(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if ctx.args:
        ctx.user_data[CHAR_KEY] = " ".join(ctx.args).strip().lower()
        await update.message.reply_text(f"Ок, выбран персонаж: {ctx.user_data[CHAR_KEY]}")
    else:
        await update.message.reply_text(f"Текущий персонаж: {get_user_char(ctx)}")

# ---------- ТЕКСТЫ ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    character = get_user_char(ctx)
    text = update.message.text.strip()

    reply = await call_runpod(user_id=user_id, character=character, text=text)
    await update.message.reply_text(reply)

# ---------- ОШИБКИ ----------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Что‑то пошло не так. Уже чиним 🛠️")
    except Exception:
        pass

# ---------- СБОРКА И ЗАПУСК ----------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(delete_webhook).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app

if __name__ == "__main__":
    app = build_app()
    # Ретрей на случай 409 Conflict при горячем деплое
    while True:
        try:
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                poll_interval=1.0,
            )
            break
        except Conflict:
            log.warning("409 Conflict (другой инстанс бота). Жду 5 сек и пробую снова…")
            import time
            time.sleep(5)
