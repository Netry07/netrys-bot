"""
Колесо удачи для netrys_bot.

• в каждом чате можно держать несколько колёс (по умолчанию — «Jackbox» с тир-листом)
• у каждого колеса свои тиры и свои коэффициенты (вес = относительный шанс пункта)
• пункты добавляются по одному или списком, переносятся между тирами, удаляются
• колесо рисуется на лету (Pillow) и уходит в чат GIF-анимацией
• у каждого тира лимит выпадений за вечер; после каждого выпадения шанс пункта падает на n п.п.
  от исходного, при исчерпании лимита пункт выходит из колеса до «нового вечера»
• есть статистика и её очистка (отдельно по каждому колесу)
• «Крутить заново» — по голосованию (в группе нужно REROLL_VOTES голосов)

Подключение в bot.py:
    import wheel
    wheel.init_tables(DB_PATH)      # после init_db()
    wheel.register(app)             # ДО обработчика store_message
"""
import asyncio
import functools
import html
import io
import logging
import math
import os
import random
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ─── Настройки ────────────────────────────────────────────────────────────────
JACKBOX_NAME = "Jackbox"
# стартовые тиры Jackbox: (название, коэффициент, лимит выпадений за вечер); меняются через /setweights
JACKBOX_TIERS = [("GOAT", 16, 5), ("Peak", 8, 4), ("Mid", 4, 3), ("Meh", 2, 2), ("Slop", 1, 1)]
# тир-лист друзей (цвета в таблице сверху вниз: GOAT → Slop)
JACKBOX_GAMES = {
    "GOAT": ["Бредовуха: Мы всё про вас знаем", "Хламотопия"],
    "Peak": ["Выжить в интернете", "Смертельная вечеринка 2", "Анализ роли", "Смехлист 3",
             "Бредовуха 4", "Творим патенты", "Дьяволы в деталях"],
    "Mid": ["Раздели комнату", "Колесо невероятных масштабов", "За работой", "Монстр Ищет Монстра",
            "Бредовуха 3", "Корабль смеха", "Несуразум", "Скоросорт", "Квартиранг"],
    "Meh": ["Подземения", "Панччемпионат", "Гражданский холст", "А голову ты не забыл?",
            "Жми на кнопку", "Рисовач: Анимач", "Город злых рифм", "На пальцах",
            "Преступление и рисование"],
    "Slop": ["Словариум", "Купол Зипол"],
}
DEFAULT_TIER = ("Обычный", 1, 1)   # единственный тир у нового колеса: (название, коэф., лимит)
DEFAULT_DECAY = 20                 # на сколько п.п. от исходного шанса падает пункт после выпадения
KNOWN_LIMITS = {"goat": 5, "peak": 4, "mid": 3, "meh": 2, "slop": 1}   # для миграции старых баз

MAX_WHEELS = 10
MAX_TIERS = 10
MAX_ITEMS = 300
MAX_NAME_LEN = 40
MAX_TIER_NAME = 20
MAX_WEIGHT = 1000
MAX_LIMIT = 100
MAX_SLOTS = 20
MODE_TIERS = "tiers"   # тир-лист: лимит выпадений за вечер + спад шанса
MODE_SLOTS = "slots"   # слоты: у пункта N копий на колесе, выпал — одна копия убралась
REROLL_VOTES = 2          # голосов для «Крутить заново» в группе
PAGE_SIZE = 10

KNOWN_EMOJI = {"goat": "🐐", "peak": "🔥", "mid": "👌", "meh": "😐", "slop": "💩"}
KNOWN_RGB = {
    "goat": (255, 127, 127), "peak": (255, 191, 127), "mid": (255, 223, 127),
    "meh": (255, 255, 127), "slop": (191, 255, 127),
}
PALETTE = [(255, 127, 127), (255, 191, 127), (255, 223, 127), (191, 255, 127),
           (127, 223, 255), (191, 159, 255), (255, 159, 223), (205, 205, 205)]
PALETTE_EMOJI = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬜"]

# Анимация
IMG_SIZE = 420
WHEEL_D = 372
SS = 2
FRAMES = 40
FRAME_MS = 80
HOLD_MS = 2500
SPIN_SECONDS = FRAMES * FRAME_MS / 1000 + 1.0

ADD_TEXT, ADD_TIER, WTS_TEXT, NEW_NAME, DECAY_TEXT = range(5)

_db_path = "messages.db"
_spin_locks: dict[int, asyncio.Lock] = {}


# ═══ База данных ══════════════════════════════════════════════════════════════
def _conn():
    c = sqlite3.connect(_db_path)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_tables(db_path: str):
    global _db_path
    _db_path = db_path
    with closing(_conn()) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS wh_wheels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                decay REAL NOT NULL DEFAULT 20,
                mode TEXT NOT NULL DEFAULT 'tiers',
                UNIQUE(chat_id, name_key)
            );
            CREATE TABLE IF NOT EXISTS wh_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wheel_id INTEGER NOT NULL REFERENCES wh_wheels(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                weight REAL NOT NULL,
                pos INTEGER NOT NULL,
                max_plays INTEGER NOT NULL DEFAULT 1,
                UNIQUE(wheel_id, name_key)
            );
            CREATE TABLE IF NOT EXISTS wh_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wheel_id INTEGER NOT NULL REFERENCES wh_wheels(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                tier_id INTEGER NOT NULL REFERENCES wh_tiers(id),
                plays INTEGER NOT NULL DEFAULT 0,
                slots INTEGER NOT NULL DEFAULT 1,
                UNIQUE(wheel_id, name_key)
            );
            CREATE TABLE IF NOT EXISTS wh_spins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                wheel_id INTEGER NOT NULL,
                item_id INTEGER,
                item_name TEXT NOT NULL,
                tier_name TEXT NOT NULL,
                user_name TEXT NOT NULL,
                ts TEXT NOT NULL,
                rejected INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_wh_spins_chat ON wh_spins(chat_id, wheel_id);
            CREATE TABLE IF NOT EXISTS wh_chat (
                chat_id INTEGER PRIMARY KEY,
                active_wheel_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS wh_last (
                chat_id INTEGER NOT NULL,
                wheel_id INTEGER NOT NULL,
                gif_id INTEGER,
                text_id INTEGER,
                PRIMARY KEY (chat_id, wheel_id)
            );
        """)
        _migrate(c)
        c.commit()
    logger.info("wheel.py: БД готова (слоты, лимиты тиров, спад шанса, уборка прокрутов)")


def _columns(c, table: str) -> set:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _migrate(c):
    """Старые базы: добавляем спад шанса и лимиты тиров, played -> plays."""
    if "decay" not in _columns(c, "wh_wheels"):
        c.execute(f"ALTER TABLE wh_wheels ADD COLUMN decay REAL NOT NULL DEFAULT {DEFAULT_DECAY}")
    if "mode" not in _columns(c, "wh_wheels"):
        c.execute("ALTER TABLE wh_wheels ADD COLUMN mode TEXT NOT NULL DEFAULT 'tiers'")
    if "max_plays" not in _columns(c, "wh_tiers"):
        c.execute("ALTER TABLE wh_tiers ADD COLUMN max_plays INTEGER NOT NULL DEFAULT 1")
        for key, lim in KNOWN_LIMITS.items():
            c.execute("UPDATE wh_tiers SET max_plays = ? WHERE name_key = ? "
                      "AND wheel_id IN (SELECT id FROM wh_wheels WHERE name_key = ?)",
                      (lim, key, JACKBOX_NAME.casefold()))
    cols = _columns(c, "wh_items")
    if "plays" not in cols:
        c.execute("ALTER TABLE wh_items ADD COLUMN plays INTEGER NOT NULL DEFAULT 0")
        if "played" in cols:
            c.execute("UPDATE wh_items SET plays = played")
    if "slots" not in cols:
        c.execute("ALTER TABLE wh_items ADD COLUMN slots INTEGER NOT NULL DEFAULT 1")


# ─── колёса ───
def _new_wheel(c, chat_id: int, name: str, tiers: list[tuple[str, float, int]], mode: str = MODE_TIERS):
    cur = c.execute("INSERT INTO wh_wheels (chat_id, name, name_key, mode) VALUES (?, ?, ?, ?)",
                    (chat_id, name, name.casefold(), mode))
    wid = cur.lastrowid
    for pos, (tname, w, lim) in enumerate(tiers):
        c.execute("INSERT INTO wh_tiers (wheel_id, name, name_key, weight, pos, max_plays) VALUES (?, ?, ?, ?, ?, ?)",
                  (wid, tname, tname.casefold(), w, pos, lim))
    return wid


def ensure_chat(chat_id: int):
    """Первое обращение чата: создаём колесо Jackbox со стартовым тир-листом."""
    with closing(_conn()) as c:
        if c.execute("SELECT 1 FROM wh_chat WHERE chat_id = ?", (chat_id,)).fetchone():
            return
        wid = _new_wheel(c, chat_id, JACKBOX_NAME, JACKBOX_TIERS)
        for tname, names in JACKBOX_GAMES.items():
            tid = c.execute("SELECT id FROM wh_tiers WHERE wheel_id = ? AND name_key = ?",
                            (wid, tname.casefold())).fetchone()[0]
            for n in names:
                c.execute("INSERT OR IGNORE INTO wh_items (wheel_id, name, name_key, tier_id) VALUES (?, ?, ?, ?)",
                          (wid, n, n.casefold(), tid))
        c.execute("INSERT INTO wh_chat (chat_id, active_wheel_id) VALUES (?, ?)", (chat_id, wid))
        c.commit()


def wheels_list(chat_id: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute("""
            SELECT w.id, w.name, (SELECT COUNT(*) FROM wh_items i WHERE i.wheel_id = w.id)
            FROM wh_wheels w WHERE w.chat_id = ? ORDER BY w.id""", (chat_id,)).fetchall()
    return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]


def wheel_get(chat_id: int, wheel_id: int):
    with closing(_conn()) as c:
        r = c.execute("SELECT id, name, decay, mode FROM wh_wheels WHERE id = ? AND chat_id = ?",
                      (wheel_id, chat_id)).fetchone()
    return {"id": r[0], "name": r[1], "decay": r[2], "mode": r[3]} if r else None


def set_decay(chat_id: int, wheel_id: int, decay: float):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_wheels SET decay = ? WHERE id = ? AND chat_id = ?", (decay, wheel_id, chat_id))
        c.commit()


def set_mode(chat_id: int, wheel_id: int, mode: str):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_wheels SET mode = ? WHERE id = ? AND chat_id = ?", (mode, wheel_id, chat_id))
        c.commit()


def wheel_find(chat_id: int, text: str):
    key = " ".join(text.split()).casefold()
    ws = wheels_list(chat_id)
    for w in ws:
        if w["name"].casefold() == key:
            return w
    starts = [w for w in ws if w["name"].casefold().startswith(key)]
    return starts[0] if len(starts) == 1 else None


def active_wheel(chat_id: int):
    ensure_chat(chat_id)
    with closing(_conn()) as c:
        r = c.execute("SELECT active_wheel_id FROM wh_chat WHERE chat_id = ?", (chat_id,)).fetchone()
    w = wheel_get(chat_id, r[0]) if r and r[0] else None
    if w:
        return w
    ws = wheels_list(chat_id)
    if ws:
        set_active(chat_id, ws[0]["id"])
        return wheel_get(chat_id, ws[0]["id"])
    # колёс не осталось — пересоздаём пустое
    with closing(_conn()) as c:
        wid = _new_wheel(c, chat_id, JACKBOX_NAME, [DEFAULT_TIER])
        c.execute("UPDATE wh_chat SET active_wheel_id = ? WHERE chat_id = ?", (wid, chat_id))
        c.commit()
    return wheel_get(chat_id, wid)


def set_active(chat_id: int, wheel_id: int):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_chat SET active_wheel_id = ? WHERE chat_id = ?", (wheel_id, chat_id))
        c.commit()


def wheel_create(chat_id: int, name: str):
    """-> id | None (если такое название уже есть)"""
    try:
        with closing(_conn()) as c:
            wid = _new_wheel(c, chat_id, name, [DEFAULT_TIER], MODE_SLOTS)
            c.commit()
            return wid
    except sqlite3.IntegrityError:
        return None


def wheel_delete(chat_id: int, wheel_id: int):
    with closing(_conn()) as c:
        c.execute("DELETE FROM wh_spins WHERE wheel_id = ? AND chat_id = ?", (wheel_id, chat_id))
        c.execute("DELETE FROM wh_last WHERE wheel_id = ? AND chat_id = ?", (wheel_id, chat_id))
        c.execute("DELETE FROM wh_items WHERE wheel_id = ? AND wheel_id IN (SELECT id FROM wh_wheels WHERE chat_id = ?)",
                  (wheel_id, chat_id))
        c.execute("DELETE FROM wh_tiers WHERE wheel_id = ? AND wheel_id IN (SELECT id FROM wh_wheels WHERE chat_id = ?)",
                  (wheel_id, chat_id))
        c.execute("DELETE FROM wh_wheels WHERE id = ? AND chat_id = ?", (wheel_id, chat_id))
        c.commit()


# ─── тиры ───
def tier_style(name: str, pos: int):
    k = name.casefold()
    rgb = KNOWN_RGB.get(k) or PALETTE[pos % len(PALETTE)]
    emoji = KNOWN_EMOJI.get(k) or PALETTE_EMOJI[pos % len(PALETTE_EMOJI)]
    return rgb, emoji


def tiers_get(wheel_id: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT t.id, t.name, t.weight, t.pos, (SELECT COUNT(*) FROM wh_items i WHERE i.tier_id = t.id), "
            "t.max_plays FROM wh_tiers t WHERE t.wheel_id = ? ORDER BY t.pos, t.id", (wheel_id,)).fetchall()
    out = []
    for r in rows:
        rgb, emoji = tier_style(r[1], r[3])
        out.append({"id": r[0], "name": r[1], "weight": r[2], "pos": r[3], "count": r[4],
                    "limit": r[5], "rgb": rgb, "emoji": emoji})
    return out


def tiers_apply(wheel_id: int, spec: list[tuple[str, float, int | None]]):
    """Заменяет набор тиров. Существующие (по названию) обновляются, лишние удаляются,
    только если в них нет пунктов. Лимит None: у существующего тира остаётся прежний, у нового = 1.
    -> текст ошибки | None"""
    cur_tiers = {t["name"].casefold(): t for t in tiers_get(wheel_id)}
    new_keys = {n.casefold() for n, *_ in spec}
    blocked = [t["name"] for k, t in cur_tiers.items() if k not in new_keys and t["count"] > 0]
    if blocked:
        return ("Нельзя убрать тиры, в которых есть пункты: " + ", ".join(blocked) +
                ". Сначала перенеси оттуда пункты (✏️ Управление) или оставь тир в списке.")
    with closing(_conn()) as c:
        for k, t in cur_tiers.items():
            if k not in new_keys:
                c.execute("DELETE FROM wh_tiers WHERE id = ?", (t["id"],))
        for pos, (name, w, lim) in enumerate(spec):
            t = cur_tiers.get(name.casefold())
            if t:
                c.execute("UPDATE wh_tiers SET weight = ?, pos = ?, max_plays = ? WHERE id = ?",
                          (w, pos, lim if lim is not None else t["limit"], t["id"]))
            else:
                c.execute("INSERT INTO wh_tiers (wheel_id, name, name_key, weight, pos, max_plays) "
                          "VALUES (?, ?, ?, ?, ?, ?)",
                          (wheel_id, name, name.casefold(), w, pos, lim if lim is not None else 1))
        c.commit()
    return None


# ─── пункты ───
def eff_weight(base: float, plays: int, decay: float) -> float:
    """Текущий вес: исходный минус decay п.п. от исходного за каждое выпадение, не ниже 0."""
    mult = 1.0 - decay * plays / 100.0
    return base * mult if mult > 1e-9 else 0.0


def _item_row(r):
    rgb, emoji = tier_style(r[3], r[5])
    plays, limit, decay, mode, slots = r[6], r[8], r[9], r[10], r[11]
    if mode == MODE_SLOTS:
        # слоты: вес = коэффициент × число оставшихся слотов; «лимит» = число слотов
        limit = slots
        eff = r[4] * (slots - plays) if slots > plays else 0.0
    else:
        eff = 0.0 if plays >= limit else eff_weight(r[4], plays, decay)
    return {"id": r[0], "name": r[1], "tier_id": r[2], "tier": r[3], "weight": r[4],
            "pos": r[5], "plays": plays, "limit": limit, "eff": eff, "available": eff > 0,
            "slots": slots, "mode": mode, "rgb": rgb, "emoji": emoji, "wheel_id": r[7]}


_ITEM_SELECT = ("SELECT i.id, i.name, i.tier_id, t.name, t.weight, t.pos, i.plays, i.wheel_id, "
                "t.max_plays, w.decay, w.mode, i.slots "
                "FROM wh_items i JOIN wh_tiers t ON t.id = i.tier_id JOIN wh_wheels w ON w.id = i.wheel_id ")


def items_all(wheel_id: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute(_ITEM_SELECT + "WHERE i.wheel_id = ?", (wheel_id,)).fetchall()
    items = [_item_row(r) for r in rows]
    items.sort(key=lambda g: (g["pos"], g["name"].casefold()))
    return items


def item_get(chat_id: int, item_id: int):
    with closing(_conn()) as c:
        r = c.execute(_ITEM_SELECT + "WHERE i.id = ? AND w.chat_id = ?",
                      (item_id, chat_id)).fetchone()
    return _item_row(r) if r else None


def item_add(wheel_id: int, name: str, tier_id: int, slots: int = 1) -> bool:
    with closing(_conn()) as c:
        cur = c.execute("INSERT OR IGNORE INTO wh_items (wheel_id, name, name_key, tier_id, slots) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (wheel_id, name, name.casefold(), tier_id, slots))
        c.commit()
        return cur.rowcount > 0


def item_count(wheel_id: int) -> int:
    with closing(_conn()) as c:
        return c.execute("SELECT COUNT(*) FROM wh_items WHERE wheel_id = ?", (wheel_id,)).fetchone()[0]


def item_move(chat_id: int, item_id: int, tier_id: int):
    it = item_get(chat_id, item_id)
    if not it:
        return
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET tier_id = ? WHERE id = ? AND "
                  "? IN (SELECT id FROM wh_tiers WHERE wheel_id = wh_items.wheel_id)",
                  (tier_id, item_id, tier_id))
        c.commit()


def item_slots_change(chat_id: int, item_id: int, delta: int):
    if not item_get(chat_id, item_id):
        return
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET slots = MAX(1, MIN(?, slots + ?)) WHERE id = ?", (MAX_SLOTS, delta, item_id))
        c.commit()


def item_delete(chat_id: int, item_id: int):
    if not item_get(chat_id, item_id):
        return
    with closing(_conn()) as c:
        c.execute("DELETE FROM wh_items WHERE id = ?", (item_id,))
        c.commit()


def inc_plays(item_id: int):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET plays = plays + 1 WHERE id = ?", (item_id,))
        c.commit()


def dec_plays(item_id: int):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET plays = MAX(0, plays - 1) WHERE id = ?", (item_id,))
        c.commit()


def reset_played(wheel_id: int):
    """«Новый вечер»: все счётчики выпадений в ноль, шансы возвращаются к исходным."""
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET plays = 0 WHERE wheel_id = ?", (wheel_id,))
        c.commit()


# ─── история ───
def spin_add(chat_id: int, wheel_id: int, item: dict, user_name: str) -> int:
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO wh_spins (chat_id, wheel_id, item_id, item_name, tier_name, user_name, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, wheel_id, item["id"], item["name"], item["tier"], user_name, datetime.now().isoformat()))
        c.commit()
        return cur.lastrowid


def spin_get(chat_id: int, spin_id: int):
    with closing(_conn()) as c:
        r = c.execute("SELECT id, wheel_id, item_id, item_name, rejected FROM wh_spins WHERE id = ? AND chat_id = ?",
                      (spin_id, chat_id)).fetchone()
    if not r:
        return None
    return {"id": r[0], "wheel_id": r[1], "item_id": r[2], "name": r[3], "rejected": bool(r[4])}


def spin_last_id(chat_id: int):
    with closing(_conn()) as c:
        r = c.execute("SELECT MAX(id) FROM wh_spins WHERE chat_id = ?", (chat_id,)).fetchone()
    return r[0] if r else None


def spin_reject(chat_id: int, spin_id: int):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_spins SET rejected = 1 WHERE id = ? AND chat_id = ?", (spin_id, chat_id))
        c.commit()


def last_get(chat_id: int, wheel_id: int):
    with closing(_conn()) as c:
        r = c.execute("SELECT gif_id, text_id FROM wh_last WHERE chat_id = ? AND wheel_id = ?",
                      (chat_id, wheel_id)).fetchone()
    return (r[0], r[1]) if r else None


def last_set(chat_id: int, wheel_id: int, gif_id: int, text_id: int):
    with closing(_conn()) as c:
        c.execute("INSERT OR REPLACE INTO wh_last (chat_id, wheel_id, gif_id, text_id) VALUES (?, ?, ?, ?)",
                  (chat_id, wheel_id, gif_id, text_id))
        c.commit()


def spins_count(chat_id: int, wheel_id: int) -> int:
    with closing(_conn()) as c:
        return c.execute("SELECT COUNT(*) FROM wh_spins WHERE chat_id = ? AND wheel_id = ? AND rejected = 0",
                         (chat_id, wheel_id)).fetchone()[0]


def stats_clear(chat_id: int, wheel_id: int) -> int:
    """Удаляет историю выпадений колеса (и, как следствие, топ). Счётчики выпадений пунктов не трогает."""
    with closing(_conn()) as c:
        cur = c.execute("DELETE FROM wh_spins WHERE chat_id = ? AND wheel_id = ?", (chat_id, wheel_id))
        c.commit()
        return cur.rowcount


def stats_data(chat_id: int, wheel_id: int):
    with closing(_conn()) as c:
        total = c.execute("SELECT COUNT(*) FROM wh_spins WHERE chat_id = ? AND wheel_id = ? AND rejected = 0",
                          (chat_id, wheel_id)).fetchone()[0]
        top = c.execute(
            "SELECT item_name, COUNT(*) AS n FROM wh_spins WHERE chat_id = ? AND wheel_id = ? AND rejected = 0 "
            "GROUP BY item_name ORDER BY n DESC, item_name LIMIT 5", (chat_id, wheel_id)).fetchall()
        last = c.execute(
            "SELECT item_name, user_name, ts FROM wh_spins WHERE chat_id = ? AND wheel_id = ? AND rejected = 0 "
            "ORDER BY id DESC LIMIT 5", (chat_id, wheel_id)).fetchall()
    return total, top, last


# ═══ Разбор текста ════════════════════════════════════════════════════════════
def esc(s: str) -> str:
    return html.escape(s, quote=False)


def fmt_w(w: float) -> str:
    return f"{w:g}"


def clean_name(raw: str, limit: int = MAX_NAME_LEN):
    """-> (name | None, ошибка | None)"""
    name = " ".join(raw.split())
    if not name:
        return None, "пустое название"
    if len(name) > limit:
        return None, f"слишком длинное (максимум {limit} символов)"
    return name, None


_BULLET = re.compile(r"^\s*(?:[-•*–—]+|\d+[.)])\s+")
_SLOTS_RE = re.compile(r"(?:\s+[xх]|\s*[×*])\s*(\d+)\s*$", re.I)   # «Да ×2», «Да x2», «Да*2»
_WEIGHT_RE = re.compile(r"^(.+?)\s*[=:]\s*(\d+(?:[.,]\d+)?)(?:\s*/\s*(\d+))?$")


def parse_weights(text: str):
    """«GOAT=16/5, Peak=8/4» (коэффициент/лимит за вечер) / по строке на тир -> (spec, ошибка)"""
    parts = [p.strip() for p in re.split(r"\n|;|,\s+", text) if p.strip()]
    if not parts:
        return None, "Пустой ввод."
    if len(parts) > MAX_TIERS:
        return None, f"Слишком много тиров (максимум {MAX_TIERS})."
    spec, seen = [], set()
    for p in parts:
        m = _WEIGHT_RE.match(p)
        if not m:
            return None, (f"Не понял «{p}». Формат: Название=коэффициент/лимит (лимит можно не указывать). "
                          "Разделяй тиры переносом строки или «, » (запятая + пробел).")
        name, err = clean_name(m.group(1), MAX_TIER_NAME)
        if err or "|" in name:
            return None, f"Тир «{m.group(1).strip()}»: {err or 'символ | нельзя'}."
        w = float(m.group(2).replace(",", "."))
        if not (0 < w <= MAX_WEIGHT):
            return None, f"Коэффициент для «{name}» должен быть больше 0 и не больше {MAX_WEIGHT}."
        lim = int(m.group(3)) if m.group(3) else None
        if lim is not None and not (1 <= lim <= MAX_LIMIT):
            return None, f"Лимит для «{name}» должен быть от 1 до {MAX_LIMIT}."
        if name.casefold() in seen:
            return None, f"Тир «{name}» указан дважды."
        seen.add(name.casefold())
        spec.append((name, w, lim))
    return spec, None


def parse_items(text: str, tiers: list[dict]):
    """Список пунктов по строкам. -> (ready[(name, tier_id, slots)], pending[(name, slots)], errors[str])"""
    by_key = {t["name"].casefold(): t for t in tiers}
    ready, pending, errors, seen = [], [], [], set()
    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line:
            continue
        tier = None
        if "|" in line:
            raw_name, _, raw_tier = line.rpartition("|")
            tier = by_key.get(raw_tier.strip().casefold())
            line_name = raw_name
            if not tier:
                name, err = clean_name(raw_name)
                errors.append(f"«{name or raw_name.strip()}»: тир «{raw_tier.strip()}» не найден" if not err
                              else f"«{line}»: {err}")
                continue
        else:
            line_name = line
        slots = 1
        sm = _SLOTS_RE.search(line_name)
        if sm:
            slots = int(sm.group(1))
            line_name = line_name[:sm.start()]
            if not (1 <= slots <= MAX_SLOTS):
                errors.append(f"«{line_name.strip()[:25]}»: слотов должно быть от 1 до {MAX_SLOTS}")
                continue
        name, err = clean_name(line_name)
        if err:
            errors.append(f"«{line[:25]}»: {err}")
            continue
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        if tier:
            ready.append((name, tier["id"], slots))
        elif len(tiers) == 1:
            ready.append((name, tiers[0]["id"], slots))
        else:
            pending.append((name, slots))
    return ready, pending, errors


def commit_items(wheel_id: int, ready: list[tuple[str, int]], errors: list[str]) -> str:
    added, dups = [], []
    room = MAX_ITEMS - item_count(wheel_id)
    overflow = 0
    for name, tid, slots in ready:
        if len(added) >= room:
            overflow += 1
            continue
        label = f"{name} ×{slots}" if slots > 1 else name
        (added if item_add(wheel_id, name, tid, slots) else dups).append(label)
    lines = []
    if added:
        shown = ", ".join(esc(n) for n in added[:20]) + (f" и ещё {len(added) - 20}" if len(added) > 20 else "")
        lines.append(f"✅ Добавлено ({len(added)}): {shown}")
    if dups:
        lines.append(f"↪️ Уже были ({len(dups)}): " + ", ".join(esc(n) for n in dups[:10]))
    if overflow:
        lines.append(f"⚠️ Не влезло {overflow} шт. — лимит {MAX_ITEMS} пунктов на колесо.")
    if errors:
        lines.append("⚠️ Пропущено:\n" + "\n".join(f"• {esc(e)}" for e in errors[:10]))
    return "\n".join(lines) or "Ничего не добавлено."


async def _safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ═══ Рисование колеса ═════════════════════════════════════════════════════════
@functools.lru_cache(maxsize=1)
def _font_path():
    candidates = [
        os.environ.get("WHEEL_FONT"),
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold:lang=ru"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if out:
            candidates.append(out)
    except Exception:
        pass
    for p in candidates:
        if p and os.path.exists(p):
            return p
    logger.warning("Не найден шрифт с кириллицей — подписи на колесе отключены. "
                   "Установи ttf-dejavu (Arch) / fonts-dejavu-core (Debian/Ubuntu) "
                   "или задай WHEEL_FONT=/путь/к/шрифту.ttf")
    return None


def _sector_color(index: int, rgb):
    if index % 2:  # чередуем оттенок, чтобы соседние пункты одного тира различались
        return tuple(int(v * 0.86) for v in rgb)
    return tuple(rgb)


def _layout(pool: list[dict]):
    total = sum(g["eff"] for g in pool)
    a = -90.0  # у PIL 0° — «3 часа», по часовой; начинаем сверху
    sectors = []
    for g in pool:
        sweep = 360.0 * g["eff"] / total
        sectors.append({"item": g, "a0": a, "a1": a + sweep, "sweep": sweep})
        a += sweep
    return sectors


def _draw_label(img, text, mid_deg, sweep, r_in, r_out, font_path):
    r_mid_px = (r_in + r_out) / 2 / SS
    cap = int(r_mid_px * math.radians(sweep) * 0.8)       # высота шрифта, влезающая по дуге
    sizes = [s for s in (15, 13, 11, 10, 9, 8) if s <= cap]
    if not sizes:
        return
    avail = int(r_out - r_in)
    font = None
    for size in sizes:
        font = ImageFont.truetype(font_path, size * SS)
        if font.getlength(text) <= avail:
            break
    while font.getlength(text) > avail and len(text) > 3:
        text = text[:-2].rstrip() + "…"
    layer = Image.new("RGBA", (avail, int(font.size * 1.5)), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((avail / 2, layer.height / 2), text, font=font, fill=(35, 35, 35, 255), anchor="mm")
    flip = math.cos(math.radians(mid_deg)) < 0
    layer = layer.rotate(-(mid_deg + (180 if flip else 0)), expand=True, resample=Image.BICUBIC)
    c = img.width / 2
    rc = (r_in + r_out) / 2
    x = c + rc * math.cos(math.radians(mid_deg))
    y = c + rc * math.sin(math.radians(mid_deg))
    img.paste(layer, (int(x - layer.width / 2), int(y - layer.height / 2)), layer)


def _build_wheel(sectors) -> Image.Image:
    big = WHEEL_D * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bbox = (0, 0, big - 1, big - 1)
    font_path = _font_path()
    for i, s in enumerate(sectors):
        color = _sector_color(i, s["item"]["rgb"])
        if len(sectors) == 1:
            d.ellipse(bbox, fill=color)
        else:
            d.pieslice(bbox, s["a0"], s["a1"], fill=color, outline=(255, 255, 255), width=2 * SS)
    d.ellipse(bbox, outline=(255, 255, 255), width=4 * SS)
    if font_path:
        r = big / 2
        for s in sectors:
            if s["sweep"] >= 7:
                _draw_label(img, s["item"]["name"], (s["a0"] + s["a1"]) / 2, s["sweep"], r * 0.22, r * 0.94, font_path)
    c = big // 2
    hub = 16 * SS
    d.ellipse((c - hub, c - hub, c + hub, c + hub), fill=(40, 40, 44), outline=(255, 255, 255), width=3 * SS)
    return img.resize((WHEEL_D, WHEEL_D), Image.LANCZOS)


def _compose_frame(wheel: Image.Image, rot: float) -> Image.Image:
    frame = Image.new("RGB", (IMG_SIZE, IMG_SIZE), (30, 31, 34))
    top = (IMG_SIZE - WHEEL_D) // 2 + 10
    left = (IMG_SIZE - WHEEL_D) // 2
    rotated = wheel.rotate(-rot, resample=Image.BICUBIC)
    frame.paste(rotated, (left, top), rotated)
    cx = IMG_SIZE // 2
    ImageDraw.Draw(frame).polygon([(cx - 15, top - 14), (cx + 15, top - 14), (cx, top + 26)],
                                  fill=(235, 60, 60), outline=(255, 255, 255))
    return frame


def _spin_plan(sector) -> float:
    """Угол поворота по часовой, после которого под стрелкой окажется нужный сектор."""
    target = sector["a0"] + sector["sweep"] * random.uniform(0.2, 0.8)
    return ((-90.0 - target) % 360.0) + 360.0 * random.randint(3, 4)


def render_wheel_gif(pool: list[dict], winner_idx: int) -> bytes:
    sectors = _layout(pool)
    wheel = _build_wheel(sectors)
    final_rot = _spin_plan(sectors[winner_idx])
    frames = []
    for k in range(FRAMES + 1):
        t = k / FRAMES
        frames.append(_compose_frame(wheel, final_rot * (1 - (1 - t) ** 2.5)))
    palette = frames[-1].quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    q = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    buf = io.BytesIO()
    q[0].save(buf, format="GIF", save_all=True, append_images=q[1:],
              duration=[FRAME_MS] * FRAMES + [HOLD_MS], loop=0, optimize=False)
    return buf.getvalue()


# ═══ Тексты и клавиатуры ══════════════════════════════════════════════════════
def menu_text(wheel: dict) -> str:
    items = items_all(wheel["id"])
    left = sum(1 for g in items if g["available"])
    if wheel["mode"] == MODE_SLOTS:
        slots_left = sum(g["slots"] - g["plays"] for g in items if g["available"])
        tail = f"Режим: слоты · слотов на колесе: {slots_left}"
    else:
        tail = f"Режим: тир-лист · спад шанса {fmt_w(wheel['decay'])} п.п. за выпадение"
    return (f"🎡 <b>{esc(wheel['name'])}</b>\n"
            f"Пунктов: {len(items)}, сейчас в игре: {left}\n{tail}")


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎡 Крутить колесо", callback_data="wh|spin")],
        [InlineKeyboardButton("📋 Тир-лист", callback_data="wh|list"),
         InlineKeyboardButton("📊 Статистика", callback_data="wh|stats")],
        [InlineKeyboardButton("➕ Добавить", callback_data="wh|add"),
         InlineKeyboardButton("✏️ Управление", callback_data="wh|mng|0")],
        [InlineKeyboardButton("⚖️ Коэффициенты", callback_data="wh|wts"),
         InlineKeyboardButton("📉 Спад шанса", callback_data="wh|decayedit")],
        [InlineKeyboardButton("🔀 Колёса", callback_data="wh|ws"),
         InlineKeyboardButton("♻️ Новый вечер", callback_data="wh|reset")],
        [InlineKeyboardButton("🎚 Режим колеса", callback_data="wh|mode")],
    ])


def tierlist_chunks(wheel: dict) -> list[str]:
    items = items_all(wheel["id"])
    tiers = tiers_get(wheel["id"])
    if not items:
        return [f"В колесе «{esc(wheel['name'])}» пока пусто. Добавь пункты: /addgame"]
    slots_mode = wheel["mode"] == MODE_SLOTS
    total = sum(g["weight"] * (g["slots"] if slots_mode else 1) for g in items)
    total_now = sum(g["eff"] for g in items)
    lines = [f"<b>🎡 {esc(wheel['name'])}</b>"]
    for t in tiers:
        gs = [g for g in items if g["tier_id"] == t["id"]]
        if not gs:
            continue
        lim = "" if slots_mode else f", лимит {t['limit']}"
        lines.append(f"\n{t['emoji']} <b>{esc(t['name'])}</b> (коэф. {fmt_w(t['weight'])}{lim}) — "
                     f"по {100 * t['weight'] / total:.1f}% на {'слот' if slots_mode else 'пункт'}")
        for g in gs:
            if slots_mode:
                left_s = g["slots"] - g["plays"]
                if not g["plays"]:
                    note = f" <i>(слотов: {g['slots']})</i>" if g["slots"] > 1 else ""
                elif g["available"]:
                    note = f" <i>(осталось {left_s}/{g['slots']}, сейчас {100 * g['eff'] / total_now:.1f}%)</i>"
                else:
                    note = " <i>(слоты закончились)</i>"
            elif not g["plays"]:
                note = ""
            elif g["available"]:
                note = f" <i>(выпадал {g['plays']}/{g['limit']}, сейчас {100 * g['eff'] / total_now:.1f}%)</i>"
            else:
                note = f" <i>(выпадал {g['plays']}/{g['limit']}, до нового вечера не выпадет)</i>"
            lines.append(f"• {esc(g['name'])}{note}")
    lines.append("\n<i>Проценты у тиров — для полного колеса в начале вечера.</i>")
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    chunks.append(cur)
    return chunks


def weights_text(wheel: dict) -> str:
    tiers = tiers_get(wheel["id"])
    lines = [f"⚖️ <b>Коэффициенты колеса «{esc(wheel['name'])}»</b>", ""]
    slots_mode = wheel["mode"] == MODE_SLOTS
    for t in tiers:
        extra = "" if slots_mode else f", лимит за вечер: <b>{t['limit']}</b>"
        lines.append(f"{t['emoji']} {esc(t['name'])} = <b>{fmt_w(t['weight'])}</b>{extra} ({t['count']} шт.)")
    if slots_mode:
        lines.append("\nРежим «слоты»: шанс пункта = коэффициент × число его оставшихся слотов / сумма по всем "
                     "пунктам. Выпал — один слот убрался. Лимиты и спад шанса здесь не действуют.")
        return "\n".join(lines)
    lines.append(f"\n📉 Спад шанса: <b>{fmt_w(wheel['decay'])}</b> п.п. от исходного за каждое выпадение")
    lines.append("\nЧем больше коэффициент, тем чаще выпадают пункты тира. "
                 "Шанс пункта = его текущий вес / сумма текущих весов всех пунктов на колесе. "
                 "Текущий вес = коэффициент × (1 − спад × число выпадений / 100), но не ниже 0. "
                 "Когда пункт выпал столько раз, сколько позволяет лимит тира, он выходит из колеса "
                 "до «Нового вечера».")
    return "\n".join(lines)


def weights_prompt(wheel: dict) -> str:
    cur = ", ".join(f"{t['name']}={fmt_w(t['weight'])}/{t['limit']}" for t in tiers_get(wheel["id"]))
    return (weights_text(wheel) + "\n\n✏️ Пришли новый набор тиров по порядку (от лучшего к худшему), "
            "по одному на строке или через «, ». Формат: <code>Название=коэффициент/лимит</code>:\n"
            f"<code>{esc(cur)}</code>\n"
            "Лимит можно не указывать: у существующего тира он останется прежним, у нового будет 1. "
            "Новые тиры создадутся, существующие обновятся, пустые лишние удалятся. Отмена — /cancel.")


def stats_text(wheel: dict) -> str:
    total, top, last = stats_data(wheel["chat_id"], wheel["id"])
    if not total:
        return f"📊 Колесо «{esc(wheel['name'])}» ещё ни разу не крутили. Начни с /spin!"
    lines = [f"📊 <b>Статистика: {esc(wheel['name'])}</b>\nВсего принятых результатов: {total}",
             "\n<b>Чаще всего выпадало:</b>"]
    lines += [f"• {esc(n)} — {c}×" for n, c in top]
    lines.append("\n<b>Последние:</b>")
    for name, user, ts in last:
        try:
            when = datetime.fromisoformat(ts).strftime("%d.%m %H:%M")
        except ValueError:
            when = ts
        lines.append(f"• {when} — {esc(name)} ({esc(user)})")
    return "\n".join(lines)


def stats_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🧹 Очистить статистику", callback_data="wh|csl")]])


def clear_picker_view(chat_id: int):
    rows = [[InlineKeyboardButton(f"{w['name']} ({spins_count(chat_id, w['id'])})"[:40],
                                  callback_data=f"wh|cs|{w['id']}")] for w in wheels_list(chat_id)]
    rows.append([InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")])
    return ("🧹 <b>Статистику какого колеса очистить?</b>\nВ скобках — число записанных результатов.",
            InlineKeyboardMarkup(rows))


def clear_confirm_view(chat_id: int, target: dict):
    n = spins_count(chat_id, target["id"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧹 Да, очистить", callback_data=f"wh|csyes|{target['id']}"),
                                InlineKeyboardButton("Отмена", callback_data="wh|csl")]])
    return (f"Очистить статистику колеса «{esc(target['name'])}»? Будет удалена вся история выпадений "
            f"({n} шт.) и топ. Текущие шансы и лимиты пунктов не изменятся: они сбрасываются "
            "кнопкой «Новый вечер».", kb)


def mode_view(wheel: dict):
    cur = wheel["mode"]
    text = (f"🎚 <b>Режим колеса «{esc(wheel['name'])}»</b>\n\n"
            "🎰 <b>Слоты</b> — у пункта несколько копий на колесе («Да ×2»). Выпал — одна копия убралась и "
            "шансы пересчитались: 2 «да» + 2 «нет» → выпало «да» → осталось 1 «да» и 2 «нет», шансы 33/67. "
            "Слоты кончились — пункта нет до «Нового вечера».\n\n"
            "🏆 <b>Тир-лист</b> — шанс зависит от тира, после каждого выпадения он падает на n п.п. "
            "(/setdecay), а у тира есть лимит выпадений за вечер (/setweights).\n\n"
            "<i>При смене режима счётчики вечера сбрасываются.</i>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(("✔ " if cur == MODE_SLOTS else "") + "🎰 Слоты", callback_data=f"wh|setmode|{MODE_SLOTS}"),
         InlineKeyboardButton(("✔ " if cur == MODE_TIERS else "") + "🏆 Тир-лист", callback_data=f"wh|setmode|{MODE_TIERS}")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")]])
    return text, kb


def wheels_view(chat_id: int):
    ws = wheels_list(chat_id)
    act = active_wheel(chat_id)["id"]
    rows = []
    for w in ws:
        rows.append([InlineKeyboardButton(f"{'▶ ' if w['id'] == act else ''}{w['name']} ({w['count']})"[:40],
                                          callback_data=f"wh|sel|{w['id']}"),
                     InlineKeyboardButton("🗑", callback_data=f"wh|wdel|{w['id']}")])
    rows.append([InlineKeyboardButton("🆕 Новое колесо", callback_data="wh|new"),
                 InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")])
    return "🔀 <b>Колёса этого чата</b> — выбери активное:", InlineKeyboardMarkup(rows)


def result_markup(spin_id: int, votes: int, need: int) -> InlineKeyboardMarkup:
    label = "🔁 Крутить заново" + (f" ({votes}/{need})" if need > 1 else "")
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"wh|rr|{spin_id}"),
                                  InlineKeyboardButton("✅ Играем!", callback_data=f"wh|ok|{spin_id}")]])


def _tier_rows(prefix: str, tiers: list[dict], current_id=None, extra: str = ""):
    btns = [InlineKeyboardButton(("✔ " if t["id"] == current_id else "") + t["name"],
                                 callback_data=f"{prefix}|{t['id']}{extra}") for t in tiers]
    return [btns[i:i + 4] for i in range(0, len(btns), 4)]


# ═══ Вращение колеса ══════════════════════════════════════════════════════════
def spin_entries(pool: list[dict], mode: str) -> list[dict]:
    """Секторы колеса. В режиме слотов каждый оставшийся слот — отдельный сектор;
    слоты раскладываются по кругу, чтобы одинаковые пункты не слипались."""
    if mode != MODE_SLOTS:
        return list(pool)
    left = {g["id"]: g["slots"] - g["plays"] for g in pool}
    out, rnd = [], 0
    while any(v > rnd for v in left.values()):
        for g in pool:
            if left[g["id"]] > rnd:
                out.append({**g, "eff": g["weight"]})
        rnd += 1
    return out


async def _delete_last(bot, chat_id: int, wheel_id: int):
    """Убираем прошлую гифку и результат этого колеса (нет прав / сообщение старое — молча пропускаем)."""
    for mid in last_get(chat_id, wheel_id) or ():
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except TelegramError:
                pass


async def run_spin(bot, chat_id: int, wheel_id: int, user_name: str, private: bool):
    lock = _spin_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await bot.send_message(chat_id, "🎡 Колесо уже крутится, дождись результата!")
        return
    async with lock:
        wheel = wheel_get(chat_id, wheel_id)
        if not wheel:
            await bot.send_message(chat_id, "Такого колеса уже нет.")
            return
        everything = items_all(wheel_id)
        if not everything:
            await bot.send_message(chat_id, f"В колесе «{wheel['name']}» пока нет пунктов. Добавь: /addgame")
            return
        note = ""
        pool = [g for g in everything if g["available"]]
        if not pool:
            reset_played(wheel_id)
            pool = items_all(wheel_id)
            note = "♻️ Все пункты исчерпаны — начинаем новый круг!\n\n"
        entries = spin_entries(pool, wheel["mode"])
        weights = [e["eff"] for e in entries]
        winner_idx = random.choices(range(len(entries)), weights=weights)[0]
        winner = entries[winner_idx]
        pct = 100.0 * sum(e["eff"] for e in entries if e["id"] == winner["id"]) / sum(weights)

        gif = await asyncio.to_thread(render_wheel_gif, entries, winner_idx)
        await _delete_last(bot, chat_id, wheel_id)
        gif_msg = await bot.send_animation(chat_id, animation=gif, filename="wheel.gif",
                                           width=IMG_SIZE, height=IMG_SIZE)
        await asyncio.sleep(SPIN_SECONDS)

        spin_id = spin_add(chat_id, wheel_id, winner, user_name)
        inc_plays(winner["id"])
        plays = winner["plays"] + 1
        if wheel["mode"] == MODE_SLOTS:
            slots_left = winner["slots"] - plays
            info = (f"Слотов осталось: {slots_left} из {winner['slots']}" if slots_left > 0
                    else "Слоты закончились — до нового вечера не выпадет")
        else:
            if plays >= winner["limit"] or eff_weight(winner["weight"], plays, wheel["decay"]) <= 0:
                nxt = "лимит исчерпан, до нового вечера не выпадет"
            else:
                nxt = f"шанс теперь {100 * eff_weight(1.0, plays, wheel['decay']):.0f}% от исходного"
            info = f"Выпадал за вечер: {plays}/{winner['limit']} — {nxt}"
        if winner["tier"] == DEFAULT_TIER[0]:
            chance_line = f"Шанс был {pct:.1f}%"
        else:
            chance_line = f"Тир: {esc(winner['tier'])} · шанс был {pct:.1f}%"
        left = sum(1 for g in items_all(wheel_id) if g["available"])
        need = 1 if private else REROLL_VOTES
        text = (f"{note}🎡 <b>{esc(wheel['name'])}</b> — выпало:\n"
                f"{winner['emoji']} <b>{esc(winner['name'])}</b>\n"
                f"{chance_line}\n"
                f"{info}\n"
                f"Крутил(а): {esc(user_name)}\n\n"
                f"<i>Пунктов ещё в игре: {left}</i>")
        res_msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                         reply_markup=result_markup(spin_id, 0, need))
        last_set(chat_id, wheel_id, gif_msg.message_id, res_msg.message_id)


# ═══ Команды ══════════════════════════════════════════════════════════════════
def _user_name(update: Update) -> str:
    u = update.effective_user
    return (u.first_name if u else None) or "Аноним"


def _cmd_text(update: Update) -> str:
    parts = (update.message.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def cmd_wheel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    await update.message.reply_text(menu_text(w), reply_markup=menu_markup(), parse_mode=ParseMode.HTML)


async def cmd_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    arg = _cmd_text(update)
    if arg:
        w = wheel_find(chat.id, arg)
        if not w:
            await update.message.reply_text("Не нашёл такое колесо. Список: /wheels")
            return
    else:
        w = active_wheel(chat.id)
    context.application.create_task(run_spin(context.bot, chat.id, w["id"], _user_name(update), chat.type == "private"))
    try:   # команду убираем, чтобы не засорять чат (нужно право админа «Удалять сообщения»)
        await update.message.delete()
    except TelegramError:
        pass


async def cmd_tierlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for chunk in tierlist_chunks(active_wheel(update.effective_chat.id)):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    w["chat_id"] = update.effective_chat.id
    await update.message.reply_text(stats_text(w), parse_mode=ParseMode.HTML, reply_markup=stats_markup())


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    reset_played(w["id"])
    await update.message.reply_text(f"♻️ Новый вечер! Все пункты колеса «{w['name']}» снова в игре, шансы вернулись к исходным.")


async def cmd_wheels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = wheels_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_weights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    await update.message.reply_text(weights_text(w), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup([[
                                        InlineKeyboardButton("✏️ Изменить", callback_data="wh|wtsedit"),
                                        InlineKeyboardButton("📉 Спад шанса", callback_data="wh|decayedit")]]))


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = mode_view(active_wheel(update.effective_chat.id))
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_clearstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active_wheel(chat_id)
    arg = _cmd_text(update)
    if arg:
        target = wheel_find(chat_id, arg)
        if not target:
            await update.message.reply_text("Не нашёл такое колесо. Список: /wheels")
            return
        text, kb = clear_confirm_view(chat_id, target)
    else:
        text, kb = clear_picker_view(chat_id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ═══ Диалоги: добавление пунктов / коэффициенты / новое колесо ════════════════
def _cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="wh|cancel")]])


def _add_prompt(wheel, tiers) -> str:
    names = ", ".join(t["name"] for t in tiers)
    tier_hint = (f"\nЧтобы сразу указать тир, пиши «Название | Тир» (тиры: {esc(names)})."
                 if len(tiers) > 1 else "")
    return (f"✏️ Колесо «{esc(wheel['name'])}». Пришли название или целый список — по одному пункту на строке."
            f"{tier_hint}\nЧтобы пункт был на колесе несколько раз (слоты), допиши «×N»: «Да ×2».\nОтмена — /cancel.")


async def _after_add_text(message, context, text: str, wheel: dict):
    tiers = tiers_get(wheel["id"])
    ready, pending, errors = parse_items(text, tiers)
    if not ready and not pending:
        await message.reply_text("⚠️ Не нашёл ни одного названия. Пришли ещё раз или /cancel.\n" +
                                 ("\n".join(f"• {e}" for e in errors[:10])))
        return ADD_TEXT
    if not pending:
        await message.reply_text(commit_items(wheel["id"], ready, errors), parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    context.user_data["wh_ready"] = ready
    context.user_data["wh_pending"] = pending
    context.user_data["wh_errors"] = errors
    if len(pending) == 1:
        what = f"«{esc(pending[0][0])}»"
    else:
        what = f"{len(pending)} пунктов без тира"
    kb = InlineKeyboardMarkup(_tier_rows("wh|addtier", tiers) + [[InlineKeyboardButton("✖ Отмена", callback_data="wh|cancel")]])
    await message.reply_text(f"В какой тир положить {what}?", reply_markup=kb, parse_mode=ParseMode.HTML)
    return ADD_TIER


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    context.user_data["wh_target"] = w["id"]
    text = _cmd_text(update)
    if text:
        return await _after_add_text(update.message, context, text, w)
    await update.message.reply_text(_add_prompt(w, tiers_get(w["id"])), parse_mode=ParseMode.HTML)
    return ADD_TEXT


async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    w = active_wheel(query.message.chat_id)
    context.user_data["wh_target"] = w["id"]
    await context.bot.send_message(query.message.chat_id, _add_prompt(w, tiers_get(w["id"])),
                                   parse_mode=ParseMode.HTML)
    return ADD_TEXT


async def add_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = wheel_get(chat_id, context.user_data.get("wh_target", 0)) or active_wheel(chat_id)
    context.user_data["wh_target"] = w["id"]
    return await _after_add_text(update.message, context, update.message.text or "", w)


async def add_got_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    w = wheel_get(chat_id, context.user_data.get("wh_target", 0))
    tid = int(query.data.split("|")[2])
    pending = context.user_data.pop("wh_pending", None)
    ready = context.user_data.pop("wh_ready", [])
    errors = context.user_data.pop("wh_errors", [])
    if not w or pending is None or tid not in {t["id"] for t in tiers_get(w["id"])}:
        await _safe_edit(query, "⚠️ Что-то пошло не так, начни заново: /addgame")
        return ConversationHandler.END
    await _safe_edit(query, commit_items(w["id"], ready + [(n, tid, s) for n, s in pending], errors))
    return ConversationHandler.END


async def wts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    context.user_data["wh_target"] = w["id"]
    text = _cmd_text(update)
    if text:
        return await _apply_weights(update.message, context, text, w)
    await update.message.reply_text(weights_prompt(w), parse_mode=ParseMode.HTML)
    return WTS_TEXT


async def wts_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    w = active_wheel(query.message.chat_id)
    context.user_data["wh_target"] = w["id"]
    await context.bot.send_message(query.message.chat_id, weights_prompt(w), parse_mode=ParseMode.HTML)
    return WTS_TEXT


async def wts_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = wheel_get(chat_id, context.user_data.get("wh_target", 0)) or active_wheel(chat_id)
    return await _apply_weights(update.message, context, update.message.text or "", w)


async def _apply_weights(message, context, text: str, wheel: dict):
    spec, err = parse_weights(text)
    if not err:
        err = tiers_apply(wheel["id"], spec)
    if err:
        await message.reply_text(f"⚠️ {err}\nПопробуй ещё раз или /cancel.")
        return WTS_TEXT
    await message.reply_text("✅ Готово!\n\n" + weights_text(wheel), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


def parse_decay(text: str):
    """«20» / «20%» -> (n, ошибка)"""
    try:
        n = float(text.strip().rstrip("%").strip().replace(",", "."))
    except ValueError:
        return None, "Нужно число от 0 до 100, например 20."
    if not (0 <= n <= 100):
        return None, "Спад должен быть от 0 до 100 процентных пунктов."
    return n, None


def decay_prompt(wheel: dict) -> str:
    return (f"📉 Спад шанса на колесе «{esc(wheel['name'])}» сейчас: <b>{fmt_w(wheel['decay'])}</b> п.п. за выпадение.\n"
            "Пришли новое число от 0 до 100 — на столько процентных пунктов от исходного шанса пункт "
            "теряет после каждого выпадения (при 20: 100% → 80% → 60% …, ниже 0 не опускается). "
            "0 — шанс не меняется, работает только лимит тира. Отмена — /cancel.")


async def _apply_decay(message, context, text: str, wheel: dict):
    n, err = parse_decay(text)
    if err:
        await message.reply_text(f"⚠️ {err}\nПопробуй ещё раз или /cancel.")
        return DECAY_TEXT
    set_decay(message.chat_id, wheel["id"], n)
    w = wheel_get(message.chat_id, wheel["id"])
    await message.reply_text("✅ Готово!\n\n" + weights_text(w), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def decay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    context.user_data["wh_target"] = w["id"]
    text = _cmd_text(update)
    if text:
        return await _apply_decay(update.message, context, text, w)
    await update.message.reply_text(decay_prompt(w), parse_mode=ParseMode.HTML)
    return DECAY_TEXT


async def decay_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    w = active_wheel(query.message.chat_id)
    context.user_data["wh_target"] = w["id"]
    await context.bot.send_message(query.message.chat_id, decay_prompt(w), parse_mode=ParseMode.HTML)
    return DECAY_TEXT


async def decay_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = wheel_get(chat_id, context.user_data.get("wh_target", 0)) or active_wheel(chat_id)
    return await _apply_decay(update.message, context, update.message.text or "", w)


async def _create_wheel_reply(message, chat_id: int, raw: str):
    name, err = clean_name(raw, 30)
    if err:
        await message.reply_text(f"⚠️ Название: {err}. Попробуй ещё раз или /cancel:")
        return NEW_NAME
    if len(wheels_list(chat_id)) >= MAX_WHEELS:
        await message.reply_text(f"⚠️ В чате уже {MAX_WHEELS} колёс — это максимум. Удали ненужное в /wheels.")
        return ConversationHandler.END
    wid = wheel_create(chat_id, name)
    if not wid:
        await message.reply_text(f"⚠️ Колесо «{name}» уже есть. Выбери другое название или /cancel:")
        return NEW_NAME
    set_active(chat_id, wid)
    w = wheel_get(chat_id, wid)
    await message.reply_text(
        menu_text(w) + "\n\nКолесо создано в режиме слотов и выбрано активным. Добавь пункты (➕); чтобы пункт "
                       "был на колесе несколько раз, пиши «Да ×2». Режим меняется кнопкой 🎚.",
        reply_markup=menu_markup(), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_chat(update.effective_chat.id)
    text = _cmd_text(update)
    if text:
        return await _create_wheel_reply(update.message, update.effective_chat.id, text)
    await update.message.reply_text("🆕 Как назвать новое колесо? (отмена — /cancel)")
    return NEW_NAME


async def new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(query.message.chat_id, "🆕 Как назвать новое колесо? (отмена — /cancel)")
    return NEW_NAME


async def new_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _create_wheel_reply(update.message, update.effective_chat.id, update.message.text or "")


async def dialog_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ("wh_ready", "wh_pending", "wh_errors", "wh_target"):
        context.user_data.pop(k, None)
    if update.callback_query:
        await update.callback_query.answer()
        await _safe_edit(update.callback_query, "❌ Отменено.")
    else:
        await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ═══ Кнопки меню ══════════════════════════════════════════════════════════════
def _manage_page(wheel: dict, page: int):
    items = items_all(wheel["id"])
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")]])
    if not items:
        return f"В колесе «{esc(wheel['name'])}» пока пусто. Добавь пункты: /addgame", back
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    rows, row = [], []
    for g in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        lbl = f"{g['emoji']} {g['name']}" + (f" ×{g['slots']}" if g["mode"] == MODE_SLOTS and g["slots"] > 1 else "")
        row.append(InlineKeyboardButton(lbl[:30], callback_data=f"wh|g|{g['id']}|{page}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"wh|mng|{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"wh|mng|{page + 1}"))
    rows.append(nav)
    return f"✏️ <b>{esc(wheel['name'])}</b> — выбери пункт (стр. {page + 1}/{pages}):", InlineKeyboardMarkup(rows)


def _item_card(item: dict, page: int):
    tiers = tiers_get(item["wheel_id"])
    slots_mode = item["mode"] == MODE_SLOTS
    total = sum(g["weight"] * (g["slots"] if slots_mode else 1) for g in items_all(item["wheel_id"]))
    share = item["weight"] * (item["slots"] if slots_mode else 1)
    used = (f"Слотов: {item['slots']} (использовано {item['plays']})" if slots_mode
            else f"Выпадал за вечер: {item['plays']}/{item['limit']}")
    text = (f"<b>{esc(item['name'])}</b>\n"
            f"Тир: {item['emoji']} {esc(item['tier'])} (коэф. {fmt_w(item['weight'])}) · "
            f"шанс {100 * share / total:.1f}% в начале вечера\n"
            f"{used}\n\nПеренести в другой тир:")
    rows = _tier_rows("wh|mv", tiers, item["tier_id"], extra="")
    # в callback нужен id пункта и страница: wh|mv|<item>|<tier>|<page>
    rows = [[InlineKeyboardButton(b.text, callback_data=f"wh|mv|{item['id']}|{b.callback_data.split('|')[2]}|{page}")
             for b in r] for r in rows]
    if slots_mode:
        rows.append([InlineKeyboardButton("➖ слот", callback_data=f"wh|sl|{item['id']}|-1|{page}"),
                     InlineKeyboardButton("➕ слот", callback_data=f"wh|sl|{item['id']}|1|{page}")])
    rows.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"wh|del|{item['id']}|{page}"),
                 InlineKeyboardButton("◀ К списку", callback_data=f"wh|mng|{page}")])
    return text, InlineKeyboardMarkup(rows)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    chat_id = query.message.chat_id
    private = query.message.chat.type == "private"
    who = query.from_user.first_name or "Аноним"

    if action != "rr":
        await query.answer()
    w = active_wheel(chat_id)
    w["chat_id"] = chat_id

    if action == "menu":
        await _safe_edit(query, menu_text(w), menu_markup())

    elif action == "spin":
        context.application.create_task(run_spin(context.bot, chat_id, w["id"], who, private))

    elif action == "list":
        for chunk in tierlist_chunks(w):
            await context.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)

    elif action == "stats":
        await context.bot.send_message(chat_id, stats_text(w), parse_mode=ParseMode.HTML,
                                       reply_markup=stats_markup())

    elif action == "reset":
        reset_played(w["id"])
        await _safe_edit(query, menu_text(w) + "\n\n♻️ Новый вечер! Все пункты снова в игре, шансы исходные.", menu_markup())

    elif action == "wts":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Изменить", callback_data="wh|wtsedit"),
                                    InlineKeyboardButton("📉 Спад шанса", callback_data="wh|decayedit")],
                                   [InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")]])
        await _safe_edit(query, weights_text(w), kb)

    elif action == "mng":
        await _safe_edit(query, *_manage_page(w, int(parts[2])))

    elif action == "g":
        item = item_get(chat_id, int(parts[2]))
        if item:
            await _safe_edit(query, *_item_card(item, int(parts[3])))
        else:
            await _safe_edit(query, *_manage_page(w, int(parts[3])))

    elif action == "mv":
        iid, tid, page = int(parts[2]), int(parts[3]), int(parts[4])
        item_move(chat_id, iid, tid)
        item = item_get(chat_id, iid)
        await _safe_edit(query, *(_item_card(item, page) if item else _manage_page(w, page)))

    elif action == "del":
        iid, page = int(parts[2]), int(parts[3])
        item = item_get(chat_id, iid)
        if not item:
            await _safe_edit(query, *_manage_page(w, page))
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"wh|delyes|{iid}|{page}"),
                                    InlineKeyboardButton("Отмена", callback_data=f"wh|g|{iid}|{page}")]])
        await _safe_edit(query, f"Удалить «{esc(item['name'])}» из колеса?", kb)

    elif action == "delyes":
        item_delete(chat_id, int(parts[2]))
        await _safe_edit(query, *_manage_page(w, int(parts[3])))

    elif action == "ws":
        await _safe_edit(query, *wheels_view(chat_id))

    elif action == "sel":
        target = wheel_get(chat_id, int(parts[2]))
        if target:
            set_active(chat_id, target["id"])
            await _safe_edit(query, menu_text(target), menu_markup())

    elif action == "wdel":
        target = wheel_get(chat_id, int(parts[2]))
        if not target:
            await _safe_edit(query, *wheels_view(chat_id))
        elif len(wheels_list(chat_id)) <= 1:
            await _safe_edit(query, "⚠️ Нельзя удалить последнее колесо — должно остаться хотя бы одно.",
                             InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="wh|ws")]]))
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"wh|wdelyes|{target['id']}"),
                                        InlineKeyboardButton("Отмена", callback_data="wh|ws")]])
            await _safe_edit(query, f"Удалить колесо «{esc(target['name'])}» вместе со всеми пунктами и историей?", kb)

    elif action == "wdelyes":
        if len(wheels_list(chat_id)) > 1:
            wheel_delete(chat_id, int(parts[2]))
        await _safe_edit(query, *wheels_view(chat_id))

    elif action == "mode":
        await _safe_edit(query, *mode_view(w))

    elif action == "setmode":
        if parts[2] in (MODE_SLOTS, MODE_TIERS):
            if parts[2] != w["mode"]:
                set_mode(chat_id, w["id"], parts[2])
                reset_played(w["id"])
            await _safe_edit(query, *mode_view(wheel_get(chat_id, w["id"])))

    elif action == "sl":
        iid, delta, page = int(parts[2]), int(parts[3]), int(parts[4])
        item_slots_change(chat_id, iid, delta)
        item = item_get(chat_id, iid)
        await _safe_edit(query, *(_item_card(item, page) if item else _manage_page(w, page)))

    elif action == "csl":
        await _safe_edit(query, *clear_picker_view(chat_id))

    elif action == "cs":
        target = wheel_get(chat_id, int(parts[2]))
        await _safe_edit(query, *(clear_confirm_view(chat_id, target) if target else clear_picker_view(chat_id)))

    elif action == "csyes":
        target = wheel_get(chat_id, int(parts[2]))
        if target:
            n = stats_clear(chat_id, target["id"])
            await _safe_edit(query,
                             f"🧹 Статистика колеса «{esc(target['name'])}» очищена (записей: {n}). "
                             "Шансы и лимиты пунктов не тронуты.",
                             InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")]]))
        else:
            await _safe_edit(query, *clear_picker_view(chat_id))

    elif action == "ok":
        spin = spin_get(chat_id, int(parts[2]))
        if spin and not spin["rejected"]:
            await _safe_edit(query, query.message.text_html + "\n\n✅ <b>Играем!</b>", None)

    elif action == "rr":
        await _handle_reroll(query, context, chat_id, int(parts[2]), private)
    # остальные wh|... (addtier, cancel, чужие кнопки диалога) — тихо игнорируем


async def _handle_reroll(query, context, chat_id: int, spin_id: int, private: bool):
    spin = spin_get(chat_id, spin_id)
    if not spin or spin["rejected"]:
        await query.answer("Этот результат уже неактуален")
        return
    if spin_id != spin_last_id(chat_id):
        await query.answer("Есть более свежий результат")
        return
    votes = context.bot_data.setdefault("wh_rr", {})
    voters = votes.setdefault((chat_id, spin_id), set())
    uid = query.from_user.id
    if uid in voters:
        await query.answer("Ты уже проголосовал")
        return
    voters.add(uid)
    need = 1 if private else REROLL_VOTES
    if len(voters) >= need:
        await query.answer("Крутим заново!")
        votes.pop((chat_id, spin_id), None)
        spin_reject(chat_id, spin_id)
        if spin["item_id"]:
            dec_plays(spin["item_id"])
        await _safe_edit(query, f"❌ <s>{esc(spin['name'])}</s> — не зашло, крутим заново", None)
        context.application.create_task(
            run_spin(context.bot, chat_id, spin["wheel_id"], query.from_user.first_name or "Аноним", private))
    else:
        await query.answer("Голос принят")
        await query.edit_message_reply_markup(result_markup(spin_id, len(voters), need))


# ═══ Регистрация ══════════════════════════════════════════════════════════════
def register(app):
    text_filter = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[
            CommandHandler(["addgame", "addgames"], add_cmd),
            CommandHandler("setweights", wts_cmd),
            CommandHandler("newwheel", new_cmd),
            CommandHandler("setdecay", decay_cmd),
            CallbackQueryHandler(add_entry, pattern=r"^wh\|add$"),
            CallbackQueryHandler(wts_entry, pattern=r"^wh\|wtsedit$"),
            CallbackQueryHandler(new_entry, pattern=r"^wh\|new$"),
            CallbackQueryHandler(decay_entry, pattern=r"^wh\|decayedit$"),
        ],
        states={
            ADD_TEXT: [MessageHandler(text_filter, add_got_text)],
            ADD_TIER: [CallbackQueryHandler(add_got_tier, pattern=r"^wh\|addtier\|")],
            WTS_TEXT: [MessageHandler(text_filter, wts_got_text)],
            NEW_NAME: [MessageHandler(text_filter, new_got_name)],
            DECAY_TEXT: [MessageHandler(text_filter, decay_got_text)],
        },
        fallbacks=[
            CommandHandler("cancel", dialog_cancel),
            CallbackQueryHandler(dialog_cancel, pattern=r"^wh\|cancel$"),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("wheel", cmd_wheel))
    app.add_handler(CommandHandler("spin", cmd_spin))
    app.add_handler(CommandHandler("tierlist", cmd_tierlist))
    app.add_handler(CommandHandler("wheels", cmd_wheels))
    app.add_handler(CommandHandler("weights", cmd_weights))
    app.add_handler(CommandHandler("setmode", cmd_mode))
    app.add_handler(CommandHandler("clearstats", cmd_clearstats))
    app.add_handler(CommandHandler("wheelstats", cmd_stats))
    app.add_handler(CommandHandler("newevening", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^wh\|"))
