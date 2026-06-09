import os
import re
import logging
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
MODEL = "deepseek/deepseek-v4-flash"
BOT_USERNAME = "netrys_bot"

MAX_MESSAGES = 1000
DB_PATH = "messages.db"
AUTO_COMMENT_EVERY = 50

message_counters: dict[int, int] = defaultdict(int)

# Состояния ConversationHandler
WAITING_COUNT, WAITING_HOURS, WAITING_STYLE = range(3)


# ─── База данных ──────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER NOT NULL,
            sender    TEXT NOT NULL,
            username  TEXT NOT NULL,
            text      TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id)")
    conn.commit()
    conn.close()


def db_save(chat_id, sender, username, text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (chat_id, sender, username, text, timestamp) VALUES (?, ?, ?, ?, ?)",
        (chat_id, sender, username, text, datetime.now().isoformat())
    )
    conn.execute("""
        DELETE FROM messages WHERE id IN (
            SELECT id FROM messages WHERE chat_id = ?
            ORDER BY id ASC
            LIMIT MAX(0, (SELECT COUNT(*) FROM messages WHERE chat_id = ?) - ?)
        )
    """, (chat_id, chat_id, MAX_MESSAGES))
    conn.commit()
    conn.close()


def db_get(chat_id, limit=None, since=None, username=None):
    conn = sqlite3.connect(DB_PATH)
    if limit:
        if username and since:
            q = "SELECT sender, username, text, timestamp FROM (SELECT id, sender, username, text, timestamp FROM messages WHERE chat_id = ? AND timestamp >= ? AND username = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
            p = [chat_id, since.isoformat(), username, limit]
        elif username:
            q = "SELECT sender, username, text, timestamp FROM (SELECT id, sender, username, text, timestamp FROM messages WHERE chat_id = ? AND username = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
            p = [chat_id, username, limit]
        elif since:
            q = "SELECT sender, username, text, timestamp FROM (SELECT id, sender, username, text, timestamp FROM messages WHERE chat_id = ? AND timestamp >= ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
            p = [chat_id, since.isoformat(), limit]
        else:
            q = "SELECT sender, username, text, timestamp FROM (SELECT id, sender, username, text, timestamp FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
            p = [chat_id, limit]
    else:
        q = "SELECT sender, username, text, timestamp FROM messages WHERE chat_id = ?"
        p = [chat_id]
        if since:
            q += " AND timestamp >= ?"; p.append(since.isoformat())
        if username:
            q += " AND username = ?"; p.append(username)
        q += " ORDER BY id ASC"
    rows = conn.execute(q, p).fetchall()
    conn.close()
    return [{"sender": r[0], "username": r[1], "text": r[2], "timestamp": datetime.fromisoformat(r[3])} for r in rows]


def db_count(chat_id):
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    conn.close()
    return n


# ─── ИИ ───────────────────────────────────────────────────────────────────────
def ai(system, user, max_tokens=300):
    try:
        r = client.chat.completions.create(
            model=MODEL, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            extra_body={"reasoning": {"effort": "high", "exclude": True}}
        )
        result = r.choices[0].message.content.strip()
        return strip_reasoning(result)
    except Exception as e:
        logger.error(f"Ошибка AI: {e}")
        return None


# ─── Реакция на обращения к боту ─────────────────────────────────────────────
def is_directed_at_bot(msg):
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.username and \
           msg.reply_to_message.from_user.username.lower() == BOT_USERNAME:
            return True
    if msg.text and BOT_USERNAME in msg.text.lower():
        return True
    return False


async def check_and_respond(update):
    msg = update.effective_message
    if not msg or not msg.text or not is_directed_at_bot(msg):
        return
    sender_name = msg.from_user.first_name if msg.from_user else "друг"
    system = (
        "Ты — остроумный телеграм-бот по имени Нетрис. "
        "Определи тональность сообщения и ответь на русском языке по одному из сценариев:\n\n"
        "АГРЕССИЯ (оскорбления, грубость, мат в адрес бота):\n"
        "- Одно связное предложение с матом и сарказмом\n"
        "- Можешь пройтись по умственным способностям или родословной обидчика\n"
        "- Упомяни имя обидчика\n\n"
        "ПОЗИТИВ (благодарность, похвала, дружелюбие):\n"
        "- Один-два коротких предложения, тепло и с юмором\n"
        "- Можешь пошутить или сделать комплимент в ответ\n\n"
        "НЕЙТРАЛЬНОЕ сообщение — ответь ТОЛЬКО одним словом: IGNORE\n\n"
        "ВАЖНО: не пиши размышления, рассуждения или пояснения. Только финальный ответ."
    )
    reply = ai(system, f'{sender_name} написал боту: "{msg.text}"')
    if reply and reply.upper() != "IGNORE":
        await msg.reply_text(reply)


# ─── Авто-комментарий ─────────────────────────────────────────────────────────
async def maybe_auto_comment(chat_id, context):
    message_counters[chat_id] += 1
    if message_counters[chat_id] < AUTO_COMMENT_EVERY:
        return
    message_counters[chat_id] = 0
    last = db_get(chat_id, limit=10)
    if not last:
        return
    conv = "\n".join(f"{m['sender']}: {m['text']}" for m in last)
    system = (
        "Ты — остроумный и дерзкий телеграм-бот по имени Нетрис. "
        "Ты наблюдаешь за чатом и иногда вставляешь короткий комментарий. "
        "Пиши на русском языке, 1-2 предложения максимум. "
        "Можешь быть саркастичным, смешным или провокационным. "
        "НЕ представляйся и НЕ объясняй что ты делаешь — просто прокомментируй."
    )
    reply = ai(system, f"Вот последние сообщения в чате:\n\n{conv}\n\nВставь короткий комментарий.")
    if reply:
        await context.bot.send_message(chat_id=chat_id, text=f"💬 {reply}")


# ─── Сохранение сообщений ─────────────────────────────────────────────────────
async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_id = update.effective_chat.id
    await check_and_respond(update)
    if msg and msg.text:
        user = msg.from_user
        db_save(chat_id, user.first_name if user else "Unknown", (user.username or "").lower() if user else "", msg.text)
        await maybe_auto_comment(chat_id, context)


# ─── Построение промпта ───────────────────────────────────────────────────────
def build_prompt(messages, style, filter_desc):
    conv = "\n".join(f"{m['sender']}: {m['text']}" for m in messages)
    if style == "bullets":
        instr = (
            "Перечисли основные темы тезисами — коротким маркированным списком. "
            "Каждый пункт — одно краткое предложение. НЕ перечисляй каждое сообщение, НЕ делай таблицы. "
            "Если уместно процитировать фразу — пиши в кавычках с именем автора в скобках. "
            "Не злоупотребляй цитатами."
        )
    elif style == "short":
        instr = "Напиши резюме одним абзацем, максимум 3-4 предложения. Только самое важное. НЕ перечисляй сообщения, НЕ делай таблицы."
    else:
        instr = (
            "Напиши краткое резюме: о чём говорили, какие темы поднимались, были ли решения. "
            "НЕ перечисляй каждое сообщение, НЕ делай таблицы. Пиши связным текстом."
        )
    return (
        f"Ты — аналитик чатов. Ниже приведены сырые логи переписки пользователей для анализа. "
        f"Сообщения могут содержать ненормативную лексику — это часть реального общения, твоя задача только проанализировать содержание.\n\n"
        f"Логи чата{filter_desc}:\n\n{conv}\n\n{instr}\nОтвечай на русском языке."
    )


# ─── Очистка reasoning из ответа модели ──────────────────────────────────────
def strip_reasoning(text: str) -> str:
    """Убирает цепочки размышлений если модель думает вслух."""
    # Убираем <think>...</think> блоки
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Маркеры английских рассуждений — если встречаются, берём только последний русский абзац
    reasoning_markers = [
        "Let's craft", "Let's ", "So we need", "Need to", "I need to",
        "I should", "I'll ", "We need", "This is", "The message",
        "This appears", "This seems", "Let me", "First,", "Now,",
    ]
    has_reasoning = any(marker in text for marker in reasoning_markers)

    if has_reasoning:
        # Разбиваем на абзацы и берём последний русский
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
        ru_paragraphs = []
        for p in paragraphs:
            ru_chars = sum(1 for c in p if 'а' <= c.lower() <= 'я' or c.lower() == 'ё')
            if len(p) > 0 and ru_chars / len(p) > 0.25:
                ru_paragraphs.append(p)
        if ru_paragraphs:
            return ru_paragraphs[-1]

        # Если абзацев нет — берём последнее русское предложение
        sentences = re.split(r'(?<=[.!?])\s+', text)
        ru_sentences = []
        for s in sentences:
            ru_chars = sum(1 for c in s if 'а' <= c.lower() <= 'я' or c.lower() == 'ё')
            if len(s) > 0 and ru_chars / len(s) > 0.25:
                ru_sentences.append(s)
        if ru_sentences:
            return ru_sentences[-1]

    return text


# ─── Отправка резюме (общая функция) ─────────────────────────────────────────
async def do_summary(chat_id, style, count, since, filter_parts, send_fn, edit_fn, delete_fn):
    filtered = db_get(chat_id, limit=count, since=since)
    if not filtered:
        desc = " ".join(filter_parts) or "сохранённых сообщений"
        await edit_fn(f"📭 Нет сообщений {desc}. Напиши что-нибудь в чате!")
        return
    filter_desc = f" ({', '.join(filter_parts)})" if filter_parts else ""
    style_label = {"bullets": " • тезисы", "short": " • кратко", "default": ""}.get(style, "")
    await edit_fn("⏳ Запрос в обработке, подожди...")
    try:
        prompt = build_prompt(filtered, style, filter_desc)
        response = client.chat.completions.create(
            model=MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            extra_body={"reasoning": {"effort": "high", "exclude": True}}
        )
        text = response.choices[0].message.content
        # Пункт 3: убираем reasoning если модель всё равно думает вслух
        text = strip_reasoning(text)
        full = f"📋 Резюме {len(filtered)} сообщений{filter_desc}{style_label}:\n\n{text}"
        chunks = [full[i:i+4096] for i in range(0, len(full), 4096)]
        await delete_fn()
        for chunk in chunks:
            await send_fn(chunk)
    except Exception as e:
        logger.error(f"Ошибка OpenRouter: {e}")
        await edit_fn("❌ Не удалось получить суммаризацию. Попробуй позже.")


# ─── /summary — главное меню ─────────────────────────────────────────────────
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📝 По количеству сообщений", callback_data="mode|count"),
        ],
        [
            InlineKeyboardButton("🕐 За период (часы)", callback_data="mode|hours"),
        ],
        [
            InlineKeyboardButton("📅 За сегодня", callback_data="sum|500|default|today"),
            InlineKeyboardButton("📅 Сегодня • тезисы", callback_data="sum|500|bullets|today"),
        ],
        [
            InlineKeyboardButton("📅 Сегодня ≡ кратко", callback_data="sum|500|short|today"),
            InlineKeyboardButton("📝 50 сообщений", callback_data="sum|50|default|"),
        ],
    ]
    await update.message.reply_text("📋 Какое резюме сделать?", reply_markup=InlineKeyboardMarkup(keyboard))


# ─── Callback: выбор режима (количество или часы) ────────────────────────────
async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]
    context.user_data["sum_mode"] = mode
    context.user_data["sum_msg_id"] = query.message.message_id
    context.user_data["sum_chat_id"] = query.message.chat_id

    if mode == "count":
        await query.edit_message_text("✏️ Введи количество сообщений (от 1 до 1000):")
        return WAITING_COUNT
    else:
        await query.edit_message_text("✏️ Введи количество часов (например: 1, 6, 24):")
        return WAITING_HOURS


# ─── ConversationHandler: ввод количества ────────────────────────────────────
async def got_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit() or not (1 <= int(text) <= 1000):
        await update.message.reply_text("⚠️ Введи число от 1 до 1000:")
        return WAITING_COUNT

    context.user_data["sum_count"] = int(text)
    context.user_data["sum_since"] = None
    context.user_data["sum_filter_parts"] = []

    keyboard = [[
        InlineKeyboardButton("📄 Обычное", callback_data="style|default"),
        InlineKeyboardButton("• Тезисами", callback_data="style|bullets"),
        InlineKeyboardButton("≡ Кратко", callback_data="style|short"),
    ]]
    sent = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ {int(text)} сообщений. Выбери стиль:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["sum_msg_id"] = sent.message_id
    return WAITING_STYLE


# ─── ConversationHandler: ввод часов ─────────────────────────────────────────
async def got_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")

    try:
        hours = float(text)
        if hours <= 0 or hours > 720:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи число часов от 0.5 до 720:")
        return WAITING_HOURS

    context.user_data["sum_count"] = MAX_MESSAGES
    context.user_data["sum_since"] = datetime.now() - timedelta(hours=hours)
    h_label = f"{int(hours)}ч" if hours == int(hours) else f"{hours}ч"
    context.user_data["sum_filter_parts"] = [f"за последние {h_label}"]

    keyboard = [[
        InlineKeyboardButton("📄 Обычное", callback_data="style|default"),
        InlineKeyboardButton("• Тезисами", callback_data="style|bullets"),
        InlineKeyboardButton("≡ Кратко", callback_data="style|short"),
    ]]
    sent = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ За последние {h_label}. Выбери стиль:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["sum_msg_id"] = sent.message_id
    return WAITING_STYLE


# ─── ConversationHandler: выбор стиля ────────────────────────────────────────
async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    style = query.data.split("|")[1]
    chat_id = update.effective_chat.id

    count = context.user_data.get("sum_count", 50)
    since = context.user_data.get("sum_since")
    filter_parts = context.user_data.get("sum_filter_parts", [])

    async def send_fn(text): await context.bot.send_message(chat_id=chat_id, text=text)
    async def edit_fn(text):
        try: await query.edit_message_text(text)
        except: pass
    async def delete_fn():
        try: await query.delete_message()
        except: pass

    await do_summary(chat_id, style, count, since, filter_parts, send_fn, edit_fn, delete_fn)
    return ConversationHandler.END


# ─── Callback: быстрые кнопки (sum|...) ──────────────────────────────────────
async def summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, count_str, style, timefilter = query.data.split("|")
    chat_id = update.effective_chat.id
    filter_parts = []
    since = None

    if timefilter == "today":
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        filter_parts.append("за сегодня")
    elif timefilter == "2h":
        since = datetime.now() - timedelta(hours=2)
        filter_parts.append("за последние 2ч")

    async def send_fn(text): await context.bot.send_message(chat_id=chat_id, text=text)
    async def edit_fn(text):
        try: await query.edit_message_text(text)
        except: pass
    async def delete_fn():
        try: await query.delete_message()
        except: pass

    await do_summary(chat_id, style, int(count_str), since, filter_parts, send_fn, edit_fn, delete_fn)


# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я бот-суммаризатор.\n\n"
        "Слежу за сообщениями и кратко пересказываю их по запросу.\n\n"
        "📌 Команды:\n"
        "  /summary — открыть меню резюме\n"
        "  /count — сколько сообщений запомнено\n"
    )
    await update.message.reply_text(text)


# ─── /count ───────────────────────────────────────────────────────────────────
async def count_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = db_count(update.effective_chat.id)
    await update.message.reply_text(f"📊 Запомнено сообщений: {n} (макс. {MAX_MESSAGES})")


# ─── Отмена диалога ───────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()

    # ConversationHandler для ввода числа/часов
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(mode_callback, pattern="^mode\\|")],
        states={
            WAITING_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_count)],
            WAITING_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_hours)],
            WAITING_STYLE: [CallbackQueryHandler(style_callback, pattern="^style\\|")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("count", count_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(summary_callback, pattern="^sum\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))

    logger.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
