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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# RunPod (для связки с серверной логикой/генерацией картинок и т.п.)
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_RUNSYNC_URL = (
    f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/runsync" if RUNPOD_ENDPOINT_ID else None
)

# OpenRouter (LLM для диалогов)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nothingiisreal/mn-celeste-12b")
OR_HTTP_REFERER = os.getenv("OR_HTTP_REFERER", "https://pixorbibot.onrender.com")
OR_X_TITLE = os.getenv("OR_X_TITLE", "PixorbiDream")

DEFAULT_CHAR = os.getenv("DEFAULT_CHARACTER", "anna").lower()
CHAR_KEY = "char"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required (Render → Environment)")

# ---------- ПЕРСОНАЖИ / ПЕРСОНЫ ----------
# Мини-шаблоны «личностей» для разных персонажей (можешь дополнять словарь)
CHAR_PERSONAS = {
    "anna": (
        "You are Anna — a warm, witty companion from a romantic visual novel. "
        "Speak in a soft, playful tone, staying in-character. Be concise, engaging, and keep continuity."
    ),
    # пример: "mira": "You are Mira — ...",
}

def get_persona(character: str) -> str:
    return CHAR_PERSONAS.get(
        character.lower(),
        "You are a helpful, engaging companion. Stay consistent and in character as defined by the user's choice.",
    )

# ---------- ТЕХНИЧЕСКОЕ ----------
async def delete_webhook(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (drop_pending_updates=True).")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

# ----- RunPod вызов (опционально) -----
async def call_runpod(user_id: int, character: str, text: str) -> str:
    """Вызов Serverless через /runsync. Если RunPod не настроен — возвращаем простую заглушку."""
    if not (RUNPOD_RUNSYNC_URL and RUNPOD_API_KEY):
        return f"{character.title()}: я услышала тебя — «{text}»."

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

        output = data.get("output") if isinstance(data, dict) else data
        if isinstance(output, dict):
            if "reply" in output:
                return str(output["reply"])
            if "msg" in output:
                return str(output["msg"])
        return str(output)

    except httpx.HTTPStatusError as e:
        log.exception("RunPod HTTP error")
        return f"Упс… ошибка сервера: {e.response.status_code} {e.response.reason_phrase}\n{e.request.url}"
    except Exception as e:
        log.exception("RunPod error")
        return f"Упс… ошибка сервера: {e}"

# ----- OpenRouter вызов (LLM-диалог) -----
async def call_openrouter(user_id: int, character: str, text: str) -> str:
    """Диалог через OpenRouter. Если ключа нет — бросаем исключение, чтобы вызвать fallback на RunPod."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing")

    system_prompt = get_persona(character)
    # (при желании можно добавить контекст переписки/память тут)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        # Эти два заголовка OpenRouter просит указывать (реферер может быть URL твоего сервиса)
        "HTTP-Referer": OR_HTTP_REFERER,
        "X-Title": OR_X_TITLE,
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 256,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        # Извлекаем текст ответа
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not content:
            return f"{character.title()}: (пустой ответ модели)"
        return content.strip()
    except httpx.HTTPStatusError as e:
        log.exception("OpenRouter HTTP error")
        code = e.response.status_code
        reason = e.response.reason_phrase
        detail = ""
        try:
            detail = e.response.text[:300]
        except Exception:
            pass
        return f"LLM ошибка: {code} {reason}\n{detail}"
    except Exception as e:
        log.exception("OpenRouter error")
        return f"LLM ошибка: {e}"

# ---------- УТИЛИТЫ ----------
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
        "Привет! Я подключён к RunPod и OpenRouter.\n"
        "Напиши любой текст — я ответлю в стиле выбранного персонажа.\n\n"
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

    # 1) Пытаемся ответить через OpenRouter (если ключ задан).
    # 2) Если что-то не так — падаем в RunPod (или заглушку).
    reply = None
    if OPENROUTER_API_KEY:
        reply = await call_openrouter(user_id=user_id, character=character, text=text)
        # Если ответ — явная ошибка LLM, попробуем RunPod как fallback
        if reply.startswith("LLM ошибка:"):
            rp_reply = await call_runpod(user_id=user_id, character=character, text=text)
            reply = rp_reply or reply
    else:
        reply = await call_runpod(user_id=user_id, character=character, text=text)

    await update.message.reply_text(reply)

# ---------- ОШИБКИ ----------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Тихо игнорируем конфликт поллинга при деплое/рестарте
    if isinstance(ctx.error, Conflict):
        log.warning("Telegram 409 Conflict: второй getUpdates в тот же токен. Подождём — само рассосётся.")
        return
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

