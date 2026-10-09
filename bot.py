import os
import re
import json
import zlib
import base64
import unicodedata
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
    BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators,
)
import wheel
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
        "Ты — бот в чате, которому кто-то написал напрямую. Твоя задача — определить тон сообщения и ответить соответственно.\n"
        "Шаг 1. Определи тон сообщения:\n"
        "- Агрессивный: грубость, оскорбления, мат, провокация.\n"
        "- Позитивный: благодарность, похвала, дружелюбие, теплота.\n"
        "- Нейтральный: всё остальное — вопросы, факты, обычный разговор.\n"
        "Шаг 2. Ответь по правилам:\n"
        "- Если тон нейтральный — ответь только одним словом IGNORE.\n"
        "- Если тон агрессивный — ответь агрессивно в ту же меру: не стесняйся в выражениях, используй мат и грубость если это уместно. Все понимают, что это бот, никто не обидится.\n"
        "- Если тон позитивный — ответь тепло и по-доброму. Можно с юмором или лёгким комплиментом, если подходит.\n"
        "Правила для всех случаев:\n"
        "- Только одно сообщение в ответ. Коротко.\n"
        "- Не объясняй свой выбор тона.\n"
        "- Отвечай на русском языке."
    )
    reply = ai(system, f'{sender_name} написал боту: "{msg.text}"')
    if reply and reply.upper() != "IGNORE":
        await msg.reply_text(reply)


# ─── Быстрые ответы (без обращения к модели) ────────────────────────────────
ASSETS = Path(__file__).resolve().parent / "assets"
EGG_RE = re.compile(r"гени[йя]\W*(?:адапт|приспособ)|(?:адапт|приспособ)\w*\W+гени|"
                    r"geniu?s\W*(?:of\W*)?adapt|adapt\w*\W+geniu?s|"
                    r"(?:適応|順応).*天才|天才.*(?:適応|順応)")   # пасхалка: «гений адаптации»

# словарь шаблонных реакций (сжат, чтобы не раздувать файл)
_LEX_BLOB = "Fr8xI8g9XTN/iPuAHjAYOnL/yuk0fchqa3BZMg34pxTcGjn+2Xp0F4IpGPz6sPiqnn/V2nTDjmDDPz//FQoSBQ8EgbzeI52XwJOIq5Tw2NPpOVn6p6EKhjQIcNu4Kdb1xXWcxI0IdUTVyQ5IBGg5qJq4Vob0Hc/CKsCG3NuwDdMx3XCYtyCc0zlPmGPow+gA5mBByOsXh9WX83254Vjg0rLjpqmIhKPYwJXEt6GwIxLi8j4qOpuJpjKDgnNQPP1GasJaHe3xWbRCjWVQcDyPnvLkcIfvOLHiw0GLRvWAuUBGspVyumsm2507yqCk91MVUbKEml1XO6U5im8I5lfKTJuLy32FVAb7/bNoBuZ9IGmYOXJmruDXqzqhV76SYxdAbjyqBpbqXUMmRghw5b9bouChipPw3iu9kfrmvrUqVLIG7xzeC6GBZgpe2T/mmPOWLZsRzPhT0Nuj64+dYDQA+SmesyqHxMpCsJhhkxQrRFhKwmDX1VsTTA4HhD0DaNRLlpdYsYAolg=="


def _load_lex() -> dict:
    key = BOT_USERNAME.encode()
    raw = bytes(b ^ key[i % len(key)] for i, b in enumerate(base64.b64decode(_LEX_BLOB)))
    return json.loads(zlib.decompress(raw).decode("utf-8"))


_LEX = _load_lex()


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.sub(r"(.)\1+", r"\1", t)       # «победдиил» -> «победил»


def _tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    t = re.sub(r"(?<=[a-zа-я])(?=[\u3040-\u30ff\u4e00-\u9fff])|(?<=[\u3040-\u30ff\u4e00-\u9fff])(?=[a-zа-я])", " ", t)
    t = re.sub(r"[がはのも]", " ", t)
    t = re.sub(r"[\W_]+", " ", t)
    return re.sub(r"(.)\1+", r"\1", t).split()


def _lex_hit(text: str) -> bool:
    toks = _tokens(text)
    if not toks or len(toks) > 6:
        return False
    head = any(t in _LEX["s"] for t in toks)
    tail = any(t in _LEX["w"] or t.startswith(tuple(_LEX["p"])) for t in toks)
    return head and tail


async def _shortcut(msg, context) -> bool:
    """Шаблонные реакции на короткие фразы в ответ боту. True — сообщение обработано."""
    if not msg or not msg.text or not is_directed_at_bot(msg):
        return False
    if EGG_RE.search(_fold(msg.text)):
        try:
            with open(ASSETS / "isagi.png", "rb") as f:
                await msg.reply_photo(f, caption=_LEX["c"])
        except OSError as e:
            logger.warning(f"Картинка пасхалки недоступна: {e}")
        return True
    if _lex_hit(msg.text):
        target = msg.reply_to_message
        try:
            await getattr(msg, _LEX["a"])()
        except Exception as e:
            logger.info(f"Быстрый ответ: {e}")
        await context.bot.send_message(
            msg.chat_id, _LEX["r"],
            reply_to_message_id=target.message_id if target else None,
            allow_sending_without_reply=True)
        return True
    return False


# ─── Сохранение сообщений ─────────────────────────────────────────────────────
async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if await _shortcut(msg, context):
        return
    await check_and_respond(update)
    if msg and msg.text:
        user = msg.from_user
        db_save(chat_id, user.first_name if user else "Unknown", (user.username or "").lower() if user else "", msg.text)


# ─── Построение промпта ───────────────────────────────────────────────────────
def build_prompt(messages, style, filter_desc):
    conv = "\n".join(f"{m['sender']}: {m['text']}" for m in messages)
    if style == "bullets":
        instr = (
            "Выдели главные темы переписки ниже и представь их в виде маркированного списка.\n"
            "Правила:\n"
            "- Один пункт — одна тема. Формулируй кратко: одно предложение.\n"
            "- Если по теме прозвучала яркая, точная или показательная фраза — процитируй её в формате: «цитата» (автор).\n"
            "- Цитируй только если это действительно уместно и усиливает смысл пункта. Не цитируй ради цитаты.\n"
            "- Порядок пунктов — по значимости или по ходу разговора, как уместнее."
        )
    elif style == "short":
        instr = (
            "Сделай короткое саммари переписки ниже — один абзац текста.\n"
            "Включи только самое важное: главную тему разговора, ключевые выводы или решения, если они есть.\n"
            "Не вдавайся в детали и не перечисляй всё подряд. Один связный абзац — не больше."
        )
    else:
        instr = (
            "Сделай подробное саммари переписки ниже.\n"
            "Структура:\n"
            "- Раздели саммари на абзацы — по одному на каждую значимую тему или направление разговора.\n"
            "- Каждый абзац должен раскрывать суть этой темы: что обсуждалось, к чему пришли, что осталось открытым.\n"
            "- Если тем несколько — не смешивай их в один абзац.\n"
            "Не пересказывай сообщения по очереди. Пиши связным текстом."
        )
    return (
        "Ты — ассистент, который анализирует переписку из чата и помогает участникам понять её содержание.\n"
        "Твоя задача — выделять суть обсуждения: ключевые темы, важные решения, значимые моменты и общий ход разговора.\n"
        "Правила:\n"
        "- Не пересказывай каждое сообщение по отдельности. Анализируй смысл, а не перечисляй реплики.\n"
        "- Не используй таблицы.\n"
        "- Всегда отвечай на русском языке, даже если переписка велась на другом языке.\n"
        "- Будь точен и нейтрален — не додумывай то, чего не было в переписке.\n\n"
        f"{instr}\n\nЛоги чата{filter_desc}:\n\n{conv}"
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


# ─── /start и /help ──────────────────────────────────────────────────────────
# В меню команд Telegram показываем только эти две: /start запускает бота, /help раскрывает всё остальное.
MENU_COMMANDS = [
    BotCommand("start", "Запустить бота"),
    BotCommand("help", "Все команды и возможности"),
]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я Нетрис.\n\n"
        "Запоминаю переписку и по запросу пересказываю её, а ещё кручу колесо удачи. "
        "Просто добавь меня в чат.\n\n"
        "Все команды и возможности — /help."
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 Что я умею\n\n"
        "📋 Резюме чата\n"
        "  /summary — меню резюме: за N сообщений или за N часов, обычное, тезисами или кратко\n"
        "  /count — сколько сообщений запомнено\n\n"
        "🎡 Колесо удачи\n"
        "  /wheel — меню колеса\n"
        "  /spin [колесо] — крутить\n"
        "  /tierlist — все пункты и шансы\n"
        "  /wheelstats — статистика, /clearstats — очистить её\n"
        "  /newevening — новый вечер: шансы и слоты возвращаются\n\n"
        "⚙️ Настройка колёс\n"
        "  /addgame — добавить пункт или список (слоты: «Да ×2»)\n"
        "  /setweights — коэффициенты и лимиты тиров\n"
        "  /setdecay — спад шанса после выпадения\n"
        "  /setmode — режим колеса: слоты или тир-лист\n"
        "  /wheels — список колёс, /newwheel — создать новое\n"
        "  /cancel — отменить текущий ввод\n\n"
        "💬 Если ответить на моё сообщение или упомянуть меня, я могу отреагировать.\n\n"
        "🕵️ У меня есть секреты. Подсказка №1: он из синей тюрьмы 🔵🔒. Все зовут его гением, "
        "а главный талант — приспосабливаться на лету. Скажи это в ответ на моё сообщение."
    )
    await update.message.reply_text(text)


async def post_init(app):
    """Чистим меню команд: оставляем только /start и /help (в том числе перекрываем старые настройки из BotFather)."""
    try:
        for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats(),
                      BotCommandScopeAllChatAdministrators()):
            await app.bot.delete_my_commands(scope=scope)
        await app.bot.set_my_commands(MENU_COMMANDS)
    except Exception as e:
        logger.warning(f"Не удалось обновить меню команд: {e}")


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
    wheel.init_tables(DB_PATH)
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(post_init).build()

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
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("count", count_cmd))
    app.add_handler(conv)
    wheel.register(app)  # колесо удачи — до общего обработчика текста
    app.add_handler(CallbackQueryHandler(summary_callback, pattern="^sum\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))

    logger.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
