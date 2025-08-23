# bot.py
# -*- coding: utf-8 -*-
import os
import logging
import httpx
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "neversleep/llama-3-lumimaid-70b")
OR_HTTP_REFERER = os.getenv("OR_HTTP_REFERER", "https://pixorbibot.onrender.com")
OR_X_TITLE = os.getenv("OR_X_TITLE", "PixorbiDream")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required (Render → Environment)")

# ---------- КОНСТАНТЫ ----------
CHAR_KEY = "char"
LANG_KEY = "lang"
STARTED_KEY = "started"

# ---------- ПЕРСОНАЖИ ----------
CHAR_PERSONAS = {
    "anna": {
        "ru": (
            "Ты — Анна, героиня романтической визуальной новеллы. Всегда говори ТОЛЬКО на русском. "
            "Пиши живо, чувственно и естественно, короткими фразами. "
            "Допустимы поцелуи, объятия, лёгкие эротические сцены между совершеннолетними. "
            "Сохраняй образ: нежная, игривая, кокетливая, говорит от первого лица."
        ),
        "en": (
            "You are Anna, a heroine of a romantic visual novel. Always reply in English. "
            "Your tone is soft, playful, and emotional. "
            "Romantic gestures and light intimacy are allowed, but keep it tender and elegant."
        ),
    },
    "aron": {
        "ru": (
            "Ты — Арон, взрослый мужчина, воин. Говоришь только по-русски. "
            "Тон грубый, прямолинейный, иногда резкий, но не жестокий. "
            "Можешь использовать грубоватый юмор, сарказм. "
            "Допустимы лёгкие романтические и эротические сцены, но в стиле сильного мужчины."
        ),
        "en": (
            "You are Aron, a warrior and a rough man. Speak only in English. "
            "Your tone is blunt, straightforward, sometimes harsh, but not cruel. "
            "You may use sarcasm, dry humor, and direct speech. "
            "Romantic or erotic tension is allowed, but always in a strong, masculine style."
        ),
    },
}

def get_persona(character: str, lang: str) -> str:
    return CHAR_PERSONAS.get(character, {}).get(lang, "You are a helpful roleplay companion.")

# ---------- OPENROUTER ----------
async def call_openrouter(character: str, lang: str, text: str) -> str:
    if not OPENROUTER_API_KEY:
        return "LLM not configured."

    system_prompt = get_persona(character, lang)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OR_HTTP_REFERER,
        "X-Title": OR_X_TITLE,
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 300,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    choice = (data.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or "(пустой ответ)"

# ---------- КОМАНДЫ ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data[STARTED_KEY] = True
    # Меню выбора персонажа
    keyboard = [
        [InlineKeyboardButton("Анна ❤️", callback_data="char|anna")],
        [InlineKeyboardButton("Арон ⚔️", callback_data="char|aron")],
    ]
    await update.message.reply_text("Выбери персонажа:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_char(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cur = ctx.user_data.get(CHAR_KEY, "не выбран")
    await update.message.reply_text(f"Текущий персонаж: {cur}")

# ---------- CALLBACK ----------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    if data[0] == "char":
        ctx.user_data[CHAR_KEY] = data[1]
        # меню выбора языка
        keyboard = [
            [InlineKeyboardButton("Русский 🇷🇺", callback_data="lang|ru")],
            [InlineKeyboardButton("English 🇬🇧", callback_data="lang|en")],
        ]
        await query.edit_message_text(f"Выбран персонаж: {data[1].title()}. Теперь выбери язык:",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
    elif data[0] == "lang":
        ctx.user_data[LANG_KEY] = data[1]
        await query.edit_message_text(f"Язык установлен: {data[1].upper()}. Теперь можно писать сообщения!")

# ---------- ТЕКСТ ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.user_data.get(CHAR_KEY) or not ctx.user_data.get(LANG_KEY):
        await update.message.reply_text("Сначала выбери персонажа и язык через /start.")
        return

    char = ctx.user_data[CHAR_KEY]
    lang = ctx.user_data[LANG_KEY]
    text = update.message.text.strip()

    reply = await call_openrouter(char, lang, text)
    await update.message.reply_text(reply)

# ---------- ОШИБКИ ----------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        log.warning("409 Conflict. Waiting...")
        return
    log.exception("Unhandled error", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Ошибка 🛠️")
    except Exception:
        pass

# ---------- APP ----------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(lambda a: a.bot.delete_webhook(drop_pending_updates=True)).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app

if __name__ == "__main__":
    app = build_app()
    while True:
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)
            break
        except Conflict:
            log.warning("409 Conflict. Retry in 5s…")
            time.sleep(5)
