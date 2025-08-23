# bot.py
# -*- coding: utf-8 -*-
import os
import logging
import httpx
import time

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

# ---------- УТИЛЫ ----------
def as_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}

# ---------- ENV ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Гейт: отвечать только после /start?
REQUIRE_START = as_bool(os.getenv("REQUIRE_START"), True)
# Показывать пользователю причину фоллбэка (иначе только в логи)
DEBUG_TO_CHAT = as_bool(os.getenv("DEBUG_TO_CHAT"), False)

# RunPod (опционально)
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_RUNSYNC_URL = (
    f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/runsync" if RUNPOD_ENDPOINT_ID else None
)

# OpenRouter (LLM для диалогов)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "neversleep/llama-3-lumimaid-70b")
OR_HTTP_REFERER = os.getenv("OR_HTTP_REFERER", "https://pixorbibot.onrender.com")
OR_X_TITLE = os.getenv("OR_X_TITLE", "PixorbiDream")

DEFAULT_CHAR = os.getenv("DEFAULT_CHARACTER", "anna").lower()
CHAR_KEY = "char"
STARTED_KEY = "started"  # отметка, что юзер нажал /start

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is required (Render → Environment)")

# ---------- ПЕРСОНАЖИ / ПЕРСОНЫ ----------
CHAR_PERSONAS = {
    "anna": (
        "Ты — Анна, героиня романтической визуальной новеллы. Всегда говори ТОЛЬКО на русском. "
        "Пиши живо, чувственно и естественно, короткими фразами. "
        "Допустимы поцелуи, объятия, лёгкие эротические сцены между СОВЕРШЕННОЛЕТНИМИ по взаимному согласию. "
        "Избегай вульгарщины и клише, не переходи границы табу. "
        "Сохраняй целостность образа Анны: нежная, игривая, немного кокетливая, говорит от первого лица. "
        "Не вставляй английских слов и междометий. Не используй скобки и мета‑комментарии."
    ),
}

def get_persona(character: str) -> str:
    base = CHAR_PERSONAS.get(
        character.lower(),
        "Ты — романтическая собеседница из визуальной новеллы. Всегда на русском. "
        "Разрешены поцелуи, прикосновения и лёгкая эротика для взрослых по взаимному согласию. "
        "Короткие фразы, естественная речь, без английских вставок."
    )
    # Два мини‑примера тона/формата (few-shot)
    examples = (
        "\n\nПримеры стиля:\n"
        "Пользователь: Поцелуешь меня?\n"
        "Ассистент: Тихо киваю и тянусь к твоим губам. Тёплый, мягкий поцелуй — и дыхание смешалось.\n"
        "Пользователь: Опиши, как ты обнимаешь меня.\n"
        "Ассистент: Обвиваю тебя руками, прижимаюсь ближе. Слышу твой ритм — и мне спокойно."
    )
    return base + examples


# ---------- ТЕХНИКА ----------
async def delete_webhook(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (drop_pending_updates=True).")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

# ----- RunPod (опц.) -----
async def call_runpod(user_id: int, character: str, text: str) -> str:
    if not (RUNPOD_RUNSYNC_URL and RUNPOD_API_KEY):
        # заглушка, если RunPod не настроен
        return f"{character.title()}: я услышала тебя — «{text}»."

    payload = {"input": {"user_id": str(user_id), "character": character, "text": text}}
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(RUNPOD_RUNSYNC_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        output = data.get("output") if isinstance(data, dict) else data
        if isinstance(output, dict):
            if "reply" in output: return str(output["reply"])
            if "msg" in output:   return str(output["msg"])
        return str(output)
    except httpx.HTTPStatusError as e:
        log.exception("RunPod HTTP error")
        msg = f"RunPod HTTP {e.response.status_code} {e.response.reason_phrase}"
        return msg if DEBUG_TO_CHAT else f"Упс… ошибка сервера."
    except Exception as e:
        log.exception("RunPod error")
        return str(e) if DEBUG_TO_CHAT else "Упс… ошибка сервера."

# ----- OpenRouter (LLM) -----
import re

def _sanitize(text: str) -> str:
    if not text:
        return text
    # уберём случайные латинские вставки вроде 'uh', 'wipe', 'lol' между русскими словами
    text = re.sub(r'\b(?:uh|um|lol|haha|giggle|winks|wipe)\b', '', text, flags=re.IGNORECASE)
    # двойные пробелы → один
    text = re.sub(r'[ \t]{2,}', ' ', text)
    # странные пробелы перед пунктуацией
    text = re.sub(r'\s+([,.!?;:])', r'\1', text)
    # нормализуем длинные троеточия
    text = re.sub(r'\.{4,}', '...', text)
    return text.strip()

async def call_openrouter(user_id: int, character: str, text: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing")

    system_prompt = get_persona(character)
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
        "temperature": 0.6,        # было 0.8
        "top_p": 0.9,
        "frequency_penalty": 0.2,  # чуть меньше повторов
        "presence_penalty": 0.0,
        "max_tokens": 320,         # немного больше воздуха ответу
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

        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        return _sanitize(content) if content else f"{character.title()}: (пустой ответ модели)"
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
    ctx.user_data[STARTED_KEY] = True
    await update.message.reply_text(
        "Привет! Я подключён к RunPod и OpenRouter.\n"
        "Напиши любой текст — я отвечу в стиле выбранного персонажа.\n\n"
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

    # ГЕЙТ /start
    if REQUIRE_START and not ctx.user_data.get(STARTED_KEY):
        await update.message.reply_text("Чтобы начать, нажми /start.")
        return

    user_id = update.effective_user.id if update.effective_user else 0
    character = get_user_char(ctx)
    text = update.message.text.strip()

    reply = None
    or_err = None
    if OPENROUTER_API_KEY:
        reply, or_err = await call_openrouter(user_id=user_id, character=character, text=text)

    if reply is None:
        # Диагностика фоллбэка
        if DEBUG_TO_CHAT and or_err:
            await update.message.reply_text(f"[LLM fallback] {or_err}")

        # RunPod или заглушка
        reply = await call_runpod(user_id=user_id, character=character, text=text)

    await update.message.reply_text(reply)

# ---------- ОШИБКИ ----------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, Conflict):
        log.warning("Telegram 409 Conflict: второй getUpdates в тот же токен. Жду и продолжаю…")
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
    while True:
        try:
            app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)
            break
        except Conflict:
            log.warning("409 Conflict (другой инстанс бота). Жду 5 сек и пробую снова…")
            time.sleep(5)



