import os
import logging
import asyncio
from typing import Final

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ---------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pixorbi-bot")

# ---------- ENV ---------- #
TELEGRAM_BOT_TOKEN: Final = os.getenv("TELEGRAM_BOT_TOKEN")
RUNPOD_ENDPOINT_URL: Final = os.getenv("RUNPOD_ENDPOINT_URL", "").strip()  # можно пустым
RUNPOD_API_KEY: Final = os.getenv("RUNPOD_API_KEY", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is required")

# текущий выбранный «персонаж» (наивно, в памяти процесса)
DEFAULT_CHAR = "anna"
current_char = DEFAULT_CHAR


# ---------- ВСПОМОГАТЕЛЬНОЕ ---------- #
async def call_runpod(text: str, user_id: int, character: str) -> str:
    """
    Наш тестовый обработчик на RunPod.
    Ожидается, что endpoint вернёт JSON вида:
        {"ok": true, "reply": "..."}
    Если RUNPOD_ENDPOINT_URL не задан — делаем локальный мок‑ответ.
    """
    if not RUNPOD_ENDPOINT_URL:
        return f"{character.title()}: я услышала тебя — «{text}»."

    payload = {
        "input": {
            "user_id": str(user_id),
            "character": character,
            "text": text,
        }
    }

    headers = {"Content-Type": "application/json"}
    if RUNPOD_API_KEY:
        headers["Authorization"] = f"Bearer {RUNPOD_API_KEY}"

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Для runsync у RunPod достаточно POST на /runsync
        url = RUNPOD_ENDPOINT_URL.rstrip("/")
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        reply = data.get("output", {}).get("reply") or data.get("reply")
        if not reply:
            # безопасный дефолт
            reply = f"{character.title()}: я услышала тебя — «{payload['input']['text']}»."
        return reply


# ---------- COMMANDS ---------- #
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я подключён к RunPod.\n"
        "Напиши любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Команды:\n"
        "  /char — текущий персонаж\n"
        "  /char <имя> — выбрать персонажа (пример: /char anna)\n\n"
        "Для теста напиши: Анна"
    )
    await update.message.reply_text(text)


async def cmd_char(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global current_char
    if ctx.args:
        current_char = ctx.args[0].strip().lower()
        await update.message.reply_text(f"Персонаж установлен: *{current_char}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"Текущий персонаж: *{current_char}*", parse_mode=ParseMode.MARKDOWN)


# ---------- MESSAGE ---------- #
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.effective_user.id if update.effective_user else 0
        text = (update.effective_message.text or "").strip()

        if not text:
            return

        reply = await call_runpod(text=text, user_id=user_id, character=current_char)
        await update.message.reply_text(reply)
    except Exception as e:
        log.exception("handler failed")
        await update.message.reply_text(f"Упс… что-то пошло не так: {e}")


# ---------- ERROR HANDLER ---------- #
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)


# ---------- MAIN ---------- #
async def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Безопасно убираем webhook (если вдруг был), и просим Telegram
    # выкинуть накопившиеся апдейты, чтобы избежать 409/задвоений
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (drop_pending_updates=True)")
    except Exception:
        log.exception("delete_webhook failed, continuing…")

    # handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    # запуск polling
    log.info("Starting polling…")
    await app.run_polling(
        poll_interval=1.0,
        allowed_updates=None,          # все типы
        drop_pending_updates=True,     # на всякий случай
        stop_signals=None,             # Render сам шлёт SIGTERM — PTB корректно завершится
    )


if __name__ == "__main__":
    asyncio.run(main())
