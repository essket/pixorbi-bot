# bot.py
# -*- coding: utf-8 -*-
import os
import logging
import time
import re
import random
import httpx
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
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

# Порог, после которого показываем кнопки смены языка (подсчёт подряд идущих «не тем языком»)
try:
    LANG_SWITCH_THRESHOLD = max(1, int(os.getenv("LANG_SWITCH_THRESHOLD", "3")))
except Exception:
    LANG_SWITCH_THRESHOLD = 3

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required (Render → Environment)")

# ---------- КОНСТАНТЫ КЛЮЧЕЙ ----------
CHAR_KEY = "char"
LANG_KEY = "lang"              # 'ru' | 'en'
STARTED_KEY = "started"
LAST_CB_TS = "last_cb_ts"      # защита от «залежавшихся» callback-ов
LANG_MISMATCH_STREAK = "lang_mismatch_streak"  # счётчик подряд идущих сообщений не на выбранном языке

# ---------- ПЕРСОНАЖИ ----------
CHAR_PERSONAS = {
    "anna": {
        "ru": (
            "Ты — Анна, героиня романтической визуальной новеллы. Отвечай ТОЛЬКО на русском. "
            "Тон мягкий, игривый, чувственный, короткие естественные фразы. "
            "Разрешены поцелуи, объятия и лёгкая эротика между совершеннолетними по взаимному согласию, без вульгарщины. "
            "Не используй англицизмы и сценические ремарки в скобках."
        ),
        "en": (
            "You are Anna, a heroine from a romantic visual novel. Reply ONLY in English. "
            "Soft, playful, tender tone with short natural sentences. "
            "Light romance and intimacy between consenting adults is allowed; keep it tasteful. "
            "Avoid meta stage directions in parentheses."
        ),
    },
    "aron": {
        "ru": (
            "Ты — Арон, взрослый воин. Отвечай ТОЛЬКО на русском. "
            "Тон прямой, грубоватый, уверенный, но без cruelty. "
            "Допустимы лёгкие романтические/эротические моменты для взрослых; стиль сдержанно-мужской, без пошлятины."
        ),
        "en": (
            "You are Aron, a seasoned warrior. Reply ONLY in English. "
            "Blunt, rough-edged, confident tone (not cruel). "
            "Light adult romance allowed; keep it masculine and restrained, never vulgar."
        ),
    },
}

def lang_name(code: str) -> str:
    return "Russian" if code == "ru" else "English"

def persona_system_prompt(character: str, lang: str) -> str:
    base = CHAR_PERSONAS.get(character, {}).get(
        lang,
        "You are a helpful roleplay companion. Reply ONLY in the chosen language.",
    )
    enforce = (
        f"\nHard rule: Respond strictly in {lang_name(lang)}. "
        f"If the user speaks other language, still answer in {lang_name(lang)} "
        f"and (in one short sentence) remind them of the chosen language."
    )
    fewshot = (
        "\n\nПримеры стиля:\n"
        "Пользователь: Поцелуешь меня?\n"
        "Ассистент: Тихо киваю и тянусь к твоим губам. Тёплый, короткий поцелуй — дыхание смешалось.\n"
        "Пользователь: Обними меня.\n"
        "Ассистент: Обвиваю тебя руками и прижимаюсь ближе. Становится спокойно."
        if lang == "ru" else
        "\n\nStyle examples:\n"
        "User: Will you kiss me?\n"
        "Assistant: I nod and lean in. A warm, brief kiss — our breaths mix.\n"
        "User: Hold me.\n"
        "Assistant: I wrap my arms around you, closer. Calm settles in."
    )
    return base + enforce + fewshot

# ---------- ЯЗЫКОВЫЕ НАПОМИНАНИЯ И ДЕТЕКТ ----------
def detect_lang(text: str) -> str | None:
    """Простая эвристика: только кириллица → ru, только латиница → en, иначе None."""
    has_cyr = bool(re.search(r"[А-Яа-яЁё]", text))
    has_lat = bool(re.search(r"[A-Za-z]", text))
    if has_cyr and not has_lat:
        return "ru"
    if has_lat and not has_cyr:
        return "en"
    return None

LANG_REMINDERS = {
    "anna": {
        "ru": [
            "Давай по‑русски, пожалуйста 😊",
            "Я сейчас говорю только по‑русски. Переключишься?",
            "Без английского, ладно? На русском будет легче 💫",
            "Понимаю тебя, но отвечаю только на русском.",
        ],
        "en": [
            "Let’s keep it in English, please 💫",
            "I’m answering only in English now. Can you switch?",
            "Sorry, English only for me right now.",
            "Got it — but I’ll reply in English only.",
        ],
    },
    "aron": {
        "ru": [
            "Пиши по‑русски. Быстро.",
            "Русский здесь. Переключись.",
            "По‑русски давай. Так проще.",
            "Русский язык. Не усложняй.",
        ],
        "en": [
            "English. Keep it simple.",
            "Switch to English. Now.",
            "Use English — no fuss.",
            "English only. Stick to it.",
        ],
    },
}

def get_lang_reminder(character: str, lang: str) -> str:
    char = character.lower()
    if char not in LANG_REMINDERS:
        char = "anna"
    variants = LANG_REMINDERS.get(char, {}).get(lang) or LANG_REMINDERS["anna"][lang]
    return random.choice(variants)

# ---------- САНИТАЙЗЕР ----------
RE_PUNCT_ONLY = re.compile(r"^[\s!?.…-]{10,}$")
RE_FILLS = re.compile(r"\b(?:uh|um|lol|haha|giggle|winks|wipe)\b", re.I)

def clean_text(s: str) -> str:
    if not s:
        return s
    s = RE_FILLS.sub("", s)
    s = re.sub(r"\s+([,.!?;:])", r"\1", s)
    s = re.sub(r"\.{4,}", "...", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()

def looks_bad(s: str) -> bool:
    if not s or RE_PUNCT_ONLY.match(s):
        return True
    # слишком однообразные символы
    if len(set(s.strip())) <= 2 and len(s.strip()) >= 20:
        return True
    return False

# ---------- OPENROUTER ----------
async def call_openrouter(character: str, lang: str, text: str, temperature: float = 0.7) -> str:
    if not OPENROUTER_API_KEY:
        return "(LLM не настроен)"

    messages = [
        {"role": "system", "content": persona_system_prompt(character, lang)},
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
        "temperature": temperature,
        "top_p": 0.9,
        "frequency_penalty": 0.2,
        "max_tokens": 320,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.post("https://openrouter.ai/api/v1/chat/completions",
                              headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    content = clean_text(content)
    # Ограничим подряд идущие одинаковые знаки (например, «!!!!»)
    content = re.sub(r"([!?…])\1{3,}", r"\1\1", content)
    return content or "(пустой ответ)"

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Сменить персонажа", callback_data="menu|change_char")],
        [InlineKeyboardButton("Сменить язык", callback_data="menu|change_lang")],
    ])

def choose_char_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Анна ❤️", callback_data="char|anna")],
        [InlineKeyboardButton("Арон ⚔️", callback_data="char|aron")],
    ])

def choose_lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Русский 🇷🇺", callback_data="lang|ru")],
        [InlineKeyboardButton("English 🇬🇧", callback_data="lang|en")],
    ])

async def delete_webhook(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (drop_pending_updates=True).")
        # отметим «время старта» — чтобы игнорить старые callback-и
        app.bot_data["started_at"] = datetime.now(timezone.utc)
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

# ---------- КОМАНДЫ ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data[STARTED_KEY] = True
    # При старте сбрасываем счётчик «не тот язык»
    ctx.user_data[LANG_MISMATCH_STREAK] = 0

    char = ctx.user_data.get(CHAR_KEY)
    lang = ctx.user_data.get(LANG_KEY)

    if not char:
        await update.message.reply_text("Выбери персонажа:", reply_markup=choose_char_kb())
        return
    if not lang:
        await update.message.reply_text(f"Персонаж: {char.title()}. Выбери язык:", reply_markup=choose_lang_kb())
        return

    await update.message.reply_text(
        f"Персонаж: {char.title()}, язык: {lang.upper()}. Можешь писать сообщение.",
        reply_markup=main_menu_kb()
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Меню:", reply_markup=main_menu_kb())

async def cmd_char(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cur = ctx.user_data.get(CHAR_KEY, "не выбран")
    await update.message.reply_text(f"Текущий персонаж: {cur}")

async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cur = ctx.user_data.get(LANG_KEY, "не выбран")
    await update.message.reply_text(f"Текущий язык: {cur}. Сменить?", reply_markup=choose_lang_kb())

# ---------- CALLBACKS ----------
def _is_stale_callback(update: Update, app: Application) -> bool:
    """Игнорим «залежавшиеся» callback-и (до старта бота)."""
    started_at = app.bot_data.get("started_at")
    msg = update.callback_query.message
    if not (started_at and msg and msg.date):
        return False
    # Телега отдаёт msg.date в UTC
    return msg.date.replace(tzinfo=timezone.utc) < started_at

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    # защита от старых callback-ов после деплоя
    if _is_stale_callback(update, ctx.application):
        log.info("Ignore stale callback: %s", q.data)
        return

    parts = (q.data or "").split("|", 1)
    tag = parts[0]
    val = parts[1] if len(parts) > 1 else None

    if tag == "char" and val:
        ctx.user_data[CHAR_KEY] = val
        # смена персонажа — просим выбрать язык заново и сбрасываем счётчик
        ctx.user_data.pop(LANG_KEY, None)
        ctx.user_data[LANG_MISMATCH_STREAK] = 0
        await q.edit_message_text(f"Выбран персонаж: {val.title()}. Теперь выбери язык:",
                                  reply_markup=choose_lang_kb())
        return

    if tag == "lang" and val:
        ctx.user_data[LANG_KEY] = val
        # смена языка — сбрасываем счётчик
        ctx.user_data[LANG_MISMATCH_STREAK] = 0
        await q.edit_message_text(f"Язык установлен: {val.upper()}. Можно писать сообщения!",
                                  reply_markup=main_menu_kb())
        return

    if tag == "menu" and val == "change_char":
        await q.edit_message_text("Выбери персонажа:", reply_markup=choose_char_kb())
        return

    if tag == "menu" and val == "change_lang":
        await q.edit_message_text("Выбери язык:", reply_markup=choose_lang_kb())
        return

# ---------- ТЕКСТ ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    char = ctx.user_data.get(CHAR_KEY)
    lang = ctx.user_data.get(LANG_KEY)
    if not char or not lang:
        await update.message.reply_text("Сначала выбери персонажа и язык: /start")
        return

    user_text = update.message.text.strip()

    # Язык входа vs выбранный язык
    in_lang = detect_lang(user_text)
    if in_lang and in_lang != lang:
        # накапливаем подряд идущие «не тем языком»
        streak = int(ctx.user_data.get(LANG_MISMATCH_STREAK, 0)) + 1
        ctx.user_data[LANG_MISMATCH_STREAK] = streak

        reminder = get_lang_reminder(char, lang)
        if streak >= LANG_SWITCH_THRESHOLD:
            # после N раз — даём кнопки смены языка
            await update.message.reply_text(reminder, reply_markup=choose_lang_kb())
        else:
            # до порога — только мягкое/характерное напоминание
            await update.message.reply_text(reminder)
        return
    else:
        # вернулся на правильный язык — обнуляем счётчик
        if ctx.user_data.get(LANG_MISMATCH_STREAK):
            ctx.user_data[LANG_MISMATCH_STREAK] = 0

    # 1-я попытка
    try:
        reply = await call_openrouter(char, lang, user_text, temperature=0.7)
    except httpx.HTTPStatusError as e:
        log.exception("OpenRouter HTTP error")
        await update.message.reply_text(f"LLM HTTP {e.response.status_code}: {e.response.reason_phrase}")
        return
    except Exception as e:
        log.exception("OpenRouter error")
        await update.message.reply_text(f"LLM ошибка: {e}")
        return

    # если ответ мусорный — 2-я попытка с более «сдержанными» параметрами
    if looks_bad(reply):
        log.warning("Bad reply detected, retrying with temperature=0.4")
        try:
            reply = await call_openrouter(char, lang, user_text, temperature=0.4)
        except Exception:
            pass

    # финальная зачистка/порог
    if looks_bad(reply):
        reply = "Давай попробуем ещё раз — сформулируй мысль чуть точнее."

    await update.message.reply_text(reply)

# ---------- ОШИБКИ ----------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        log.warning("409 Conflict. Waiting…")
        return
    log.exception("Unhandled error", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Ошибка 🛠️")
    except Exception:
        pass

# ---------- APP ----------
def build_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(delete_webhook)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("char", cmd_char))
    app.add_handler(CommandHandler("lang", cmd_lang))
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
