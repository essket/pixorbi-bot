# bot.py
import os
import logging
import json
import httpx

from telegram import Update
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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # <— именно это имя переменной
RUNPOD_ENDPOINT_URL = os.getenv("RUNPOD_ENDPOINT_URL")  # опционально
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")     # опционально

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required in Render → Environment")

# ---------- ПЕРСОНАЖ (простой выбор) ----------
DEFAULT_CHAR = "anna"
CHAR_KEY = "char"  # ключ, по которому будем хранить выбранный персонаж в user_data


# Удаляем вебхук при старте, чтобы polling работал без конфликтов
async def on_startup(app: Application) -> None:
    await app.bot.delete_webhook(drop_pending_updates=True)
    log.info("Webhook deleted (drop_pending_updates=True). Starting polling…")


# /start
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.setdefault(CHAR_KEY, DEFAULT_CHAR)
    await update.message.reply_text(
        "Привет! Я подключён к RunPod.\n"
        "Напиши любой текст — я отправлю его на сервер и верну ответ.\n\n"
        "Команды:\n"
        "  /char — показать текущего персонажа\n"
        "  /char <имя> — выбрать персонажа (пример: /char anna)\n\n"
        "Для теста напиши: Анна"
    )


# /char
async def cmd_char(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        context.user_data[CHAR_KEY] = context.args[0].strip().lower()
        await update.message.reply_text(f"Ок, выбран персонаж: {context.user_data[CHAR_KEY]}")
    else:
        cur = context.user_data.get(CHAR_KEY, DEFAULT_CHAR)
        await update.message.reply_text(f"Текущий персонаж: {cur}")


# Отправка текста на RunPod (если настроен)
async def call_runpod(user_id: int, character: str, text: str) -> str:
    if not RUNPOD_ENDPOINT_URL:
        # заглушка если RunPod не настроен
        return f"{character.title()}: я услышала тебя — «{text}»."

    payload = {
        "input": {
            "user_id": str(user_id),
            "character": character,
            "text": text,
        }
    }

    try:
        # Синхронный httpx в отдельном потоке не нужен — PTB 20 сам крутит loop.
        # Используем httpx.AsyncClient, чтобы не блокировать.
        async with httpx.AsyncClient(timeout=30) as client:
            # для endpoint’ов типа /runsync:
            if RUNPOD_ENDPOINT_URL.rstrip("/").endswith("/runsync"):
                resp = await client.post(RUNPOD_ENDPOINT_URL, json=payload)
            else:
                # обычный /run + ожидание статуса
                run = await client.post(RUNPOD_ENDPOINT_URL, json=payload)
                run.raise_for_status()
                run_id = run.json().get("id")
                status_url = f"{RUNPOD_ENDPOINT_URL.rstrip('/')}/status/{run_id}"
                # простое ожидание готовности
                for _ in range(60):
                    st = await client.get(status_url)
                    st.raise_for_status()
                    data = st.json()
                    if data.get("status") == "COMPLETED":
                        resp = st  # финальный ответ в st
                        break
                else:
                    raise RuntimeError("RunPod timeout while waiting for COMPLETED status")

        resp.raise_for_status()
        data = resp.json()
        # Унифицируем поле с ответом
        output = data.get("output") or data.get("response") or data
        if isinstance(output, dict) and "reply" in output:
            return str(output["reply"])
        if isinstance(output, dict) and "msg" in output:
            return str(output["msg"])
        return str(output)
    except Exception as e:
        log.exception("RunPod error")
        return f"Упс… ошибка сервера: {e}"


# Обработка любого текста
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    character = context.user_data.get(CHAR_KEY, DEFAULT_CHAR)
    text = update.message.text.strip()

    reply = await call_runpod(user_id=user_id, character=character, text=text)
    await update.message.reply_text(reply)


# Ловим исключения, чтобы они не падали в логи «без обработчиков»
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Что‑то пошло не так. Уже чиним 🛠️")
    except Exception:
        pass


# ---------- Application сборка ----------
def build_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)   # удаляем вебхук перед polling
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)
    return app


# ---------- main ----------
if __name__ == "__main__":
    application = build_app()
    # В PTB 20.x run_polling — синхронный блокирующий вызов
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=1.0,
    )
