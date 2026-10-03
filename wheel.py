"""
Колесо удачи для netrys_bot.

• в каждом чате можно держать несколько колёс (по умолчанию — «Jackbox» с тир-листом)
• у каждого колеса свои тиры и свои коэффициенты (вес = относительный шанс пункта)
• пункты добавляются по одному или списком, переносятся между тирами, удаляются
• колесо рисуется на лету (Pillow) и уходит в чат GIF-анимацией
• выпавшие пункты не повторяются до «нового вечера», есть статистика
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
from telegram.error import BadRequest
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
# стартовые тиры Jackbox: (название, коэффициент); меняются потом через /setweights
JACKBOX_TIERS = [("GOAT", 16), ("Peak", 8), ("Mid", 4), ("Meh", 2), ("Slop", 1)]
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
DEFAULT_TIER = ("Обычный", 1)      # единственный тир у нового колеса

MAX_WHEELS = 10
MAX_TIERS = 10
MAX_ITEMS = 300
MAX_NAME_LEN = 40
MAX_TIER_NAME = 20
MAX_WEIGHT = 1000
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

ADD_TEXT, ADD_TIER, WTS_TEXT, NEW_NAME = range(4)

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
                UNIQUE(chat_id, name_key)
            );
            CREATE TABLE IF NOT EXISTS wh_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wheel_id INTEGER NOT NULL REFERENCES wh_wheels(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                weight REAL NOT NULL,
                pos INTEGER NOT NULL,
                UNIQUE(wheel_id, name_key)
            );
            CREATE TABLE IF NOT EXISTS wh_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wheel_id INTEGER NOT NULL REFERENCES wh_wheels(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                tier_id INTEGER NOT NULL REFERENCES wh_tiers(id),
                played INTEGER NOT NULL DEFAULT 0,
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
        """)
        c.commit()


# ─── колёса ───
def _new_wheel(c, chat_id: int, name: str, tiers: list[tuple[str, float]]):
    cur = c.execute("INSERT INTO wh_wheels (chat_id, name, name_key) VALUES (?, ?, ?)",
                    (chat_id, name, name.casefold()))
    wid = cur.lastrowid
    for pos, (tname, w) in enumerate(tiers):
        c.execute("INSERT INTO wh_tiers (wheel_id, name, name_key, weight, pos) VALUES (?, ?, ?, ?, ?)",
                  (wid, tname, tname.casefold(), w, pos))
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
        r = c.execute("SELECT id, name FROM wh_wheels WHERE id = ? AND chat_id = ?",
                      (wheel_id, chat_id)).fetchone()
    return {"id": r[0], "name": r[1]} if r else None


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
            wid = _new_wheel(c, chat_id, name, [DEFAULT_TIER])
            c.commit()
            return wid
    except sqlite3.IntegrityError:
        return None


def wheel_delete(chat_id: int, wheel_id: int):
    with closing(_conn()) as c:
        c.execute("DELETE FROM wh_spins WHERE wheel_id = ? AND chat_id = ?", (wheel_id, chat_id))
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
            "SELECT t.id, t.name, t.weight, t.pos, (SELECT COUNT(*) FROM wh_items i WHERE i.tier_id = t.id) "
            "FROM wh_tiers t WHERE t.wheel_id = ? ORDER BY t.pos, t.id", (wheel_id,)).fetchall()
    out = []
    for r in rows:
        rgb, emoji = tier_style(r[1], r[3])
        out.append({"id": r[0], "name": r[1], "weight": r[2], "pos": r[3], "count": r[4],
                    "rgb": rgb, "emoji": emoji})
    return out


def tiers_apply(wheel_id: int, spec: list[tuple[str, float]]):
    """Заменяет набор тиров. Существующие (по названию) обновляются, лишние удаляются,
    только если в них нет пунктов. -> текст ошибки | None"""
    cur_tiers = {t["name"].casefold(): t for t in tiers_get(wheel_id)}
    new_keys = {n.casefold() for n, _ in spec}
    blocked = [t["name"] for k, t in cur_tiers.items() if k not in new_keys and t["count"] > 0]
    if blocked:
        return ("Нельзя убрать тиры, в которых есть пункты: " + ", ".join(blocked) +
                ". Сначала перенеси оттуда пункты (✏️ Управление) или оставь тир в списке.")
    with closing(_conn()) as c:
        for k, t in cur_tiers.items():
            if k not in new_keys:
                c.execute("DELETE FROM wh_tiers WHERE id = ?", (t["id"],))
        for pos, (name, w) in enumerate(spec):
            t = cur_tiers.get(name.casefold())
            if t:
                c.execute("UPDATE wh_tiers SET weight = ?, pos = ? WHERE id = ?", (w, pos, t["id"]))
            else:
                c.execute("INSERT INTO wh_tiers (wheel_id, name, name_key, weight, pos) VALUES (?, ?, ?, ?, ?)",
                          (wheel_id, name, name.casefold(), w, pos))
        c.commit()
    return None


# ─── пункты ───
def _item_row(r):
    rgb, emoji = tier_style(r[3], r[5])
    return {"id": r[0], "name": r[1], "tier_id": r[2], "tier": r[3], "weight": r[4],
            "pos": r[5], "played": bool(r[6]), "rgb": rgb, "emoji": emoji, "wheel_id": r[7]}


_ITEM_SELECT = ("SELECT i.id, i.name, i.tier_id, t.name, t.weight, t.pos, i.played, i.wheel_id "
                "FROM wh_items i JOIN wh_tiers t ON t.id = i.tier_id ")


def items_all(wheel_id: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute(_ITEM_SELECT + "WHERE i.wheel_id = ?", (wheel_id,)).fetchall()
    items = [_item_row(r) for r in rows]
    items.sort(key=lambda g: (g["pos"], g["name"].casefold()))
    return items


def item_get(chat_id: int, item_id: int):
    with closing(_conn()) as c:
        r = c.execute(_ITEM_SELECT + "JOIN wh_wheels w ON w.id = i.wheel_id WHERE i.id = ? AND w.chat_id = ?",
                      (item_id, chat_id)).fetchone()
    return _item_row(r) if r else None


def item_add(wheel_id: int, name: str, tier_id: int) -> bool:
    with closing(_conn()) as c:
        cur = c.execute("INSERT OR IGNORE INTO wh_items (wheel_id, name, name_key, tier_id) VALUES (?, ?, ?, ?)",
                        (wheel_id, name, name.casefold(), tier_id))
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


def item_delete(chat_id: int, item_id: int):
    if not item_get(chat_id, item_id):
        return
    with closing(_conn()) as c:
        c.execute("DELETE FROM wh_items WHERE id = ?", (item_id,))
        c.commit()


def set_played(item_id: int, played: bool):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET played = ? WHERE id = ?", (1 if played else 0, item_id))
        c.commit()


def reset_played(wheel_id: int):
    with closing(_conn()) as c:
        c.execute("UPDATE wh_items SET played = 0 WHERE wheel_id = ?", (wheel_id,))
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
_WEIGHT_RE = re.compile(r"^(.+?)\s*[=:]\s*(\d+(?:[.,]\d+)?)$")


def parse_weights(text: str):
    """«GOAT=16, Peak=8» / по строке на тир -> (spec, ошибка)"""
    parts = [p.strip() for p in re.split(r"\n|;|,\s+", text) if p.strip()]
    if not parts:
        return None, "Пустой ввод."
    if len(parts) > MAX_TIERS:
        return None, f"Слишком много тиров (максимум {MAX_TIERS})."
    spec, seen = [], set()
    for p in parts:
        m = _WEIGHT_RE.match(p)
        if not m:
            return None, (f"Не понял «{p}». Формат: Название=число. "
                          "Разделяй тиры переносом строки или «, » (запятая + пробел).")
        name, err = clean_name(m.group(1), MAX_TIER_NAME)
        if err or "|" in name:
            return None, f"Тир «{m.group(1).strip()}»: {err or 'символ | нельзя'}."
        w = float(m.group(2).replace(",", "."))
        if not (0 < w <= MAX_WEIGHT):
            return None, f"Коэффициент для «{name}» должен быть больше 0 и не больше {MAX_WEIGHT}."
        if name.casefold() in seen:
            return None, f"Тир «{name}» указан дважды."
        seen.add(name.casefold())
        spec.append((name, w))
    return spec, None


def parse_items(text: str, tiers: list[dict]):
    """Список пунктов по строкам. -> (ready[(name, tier_id)], pending[name], errors[str])"""
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
        name, err = clean_name(line_name)
        if err:
            errors.append(f"«{line[:25]}»: {err}")
            continue
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        if tier:
            ready.append((name, tier["id"]))
        elif len(tiers) == 1:
            ready.append((name, tiers[0]["id"]))
        else:
            pending.append(name)
    return ready, pending, errors


def commit_items(wheel_id: int, ready: list[tuple[str, int]], errors: list[str]) -> str:
    added, dups = [], []
    room = MAX_ITEMS - item_count(wheel_id)
    overflow = 0
    for name, tid in ready:
        if len(added) >= room:
            overflow += 1
            continue
        (added if item_add(wheel_id, name, tid) else dups).append(name)
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
    total = sum(g["weight"] for g in pool)
    a = -90.0  # у PIL 0° — «3 часа», по часовой; начинаем сверху
    sectors = []
    for g in pool:
        sweep = 360.0 * g["weight"] / total
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
    left = sum(1 for g in items if not g["played"])
    return (f"🎡 <b>{esc(wheel['name'])}</b>\n"
            f"Пунктов: {len(items)}, ещё не выпадали в этот вечер: {left}")


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎡 Крутить колесо", callback_data="wh|spin")],
        [InlineKeyboardButton("📋 Тир-лист", callback_data="wh|list"),
         InlineKeyboardButton("📊 Статистика", callback_data="wh|stats")],
        [InlineKeyboardButton("➕ Добавить", callback_data="wh|add"),
         InlineKeyboardButton("✏️ Управление", callback_data="wh|mng|0")],
        [InlineKeyboardButton("⚖️ Коэффициенты", callback_data="wh|wts"),
         InlineKeyboardButton("🔀 Колёса", callback_data="wh|ws")],
        [InlineKeyboardButton("♻️ Новый вечер", callback_data="wh|reset")],
    ])


def tierlist_chunks(wheel: dict) -> list[str]:
    items = items_all(wheel["id"])
    tiers = tiers_get(wheel["id"])
    if not items:
        return [f"В колесе «{esc(wheel['name'])}» пока пусто. Добавь пункты: /addgame"]
    total = sum(g["weight"] for g in items)
    lines = [f"<b>🎡 {esc(wheel['name'])}</b>"]
    for t in tiers:
        gs = [g for g in items if g["tier_id"] == t["id"]]
        if not gs:
            continue
        lines.append(f"\n{t['emoji']} <b>{esc(t['name'])}</b> (коэф. {fmt_w(t['weight'])}) — "
                     f"по {100 * t['weight'] / total:.1f}% на пункт")
        for g in gs:
            lines.append(f"• {esc(g['name'])}" + (" <i>(уже выпадал)</i>" if g["played"] else ""))
    lines.append("\n<i>Шансы указаны для полного колеса.</i>")
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
    for t in tiers:
        lines.append(f"{t['emoji']} {esc(t['name'])} = <b>{fmt_w(t['weight'])}</b> ({t['count']} шт.)")
    lines.append("\nЧем больше коэффициент, тем чаще выпадают пункты тира. "
                 "Шанс пункта = коэффициент его тира / сумма коэффициентов всех пунктов на колесе.")
    return "\n".join(lines)


def weights_prompt(wheel: dict) -> str:
    cur = ", ".join(f"{t['name']}={fmt_w(t['weight'])}" for t in tiers_get(wheel["id"]))
    return (weights_text(wheel) + "\n\n✏️ Пришли новый набор тиров по порядку (от лучшего к худшему), "
            "по одному на строке или через «, »:\n"
            f"<code>{esc(cur)}</code>\n"
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
        pool = [g for g in everything if not g["played"]]
        if not pool:
            reset_played(wheel_id)
            pool = items_all(wheel_id)
            note = "♻️ Все пункты уже выпадали — начинаем новый круг!\n\n"
        weights = [g["weight"] for g in pool]
        winner_idx = random.choices(range(len(pool)), weights=weights)[0]
        winner = pool[winner_idx]
        pct = 100.0 * weights[winner_idx] / sum(weights)

        gif = await asyncio.to_thread(render_wheel_gif, pool, winner_idx)
        await bot.send_animation(chat_id, animation=gif, filename="wheel.gif", width=IMG_SIZE, height=IMG_SIZE)
        await asyncio.sleep(SPIN_SECONDS)

        spin_id = spin_add(chat_id, wheel_id, winner, user_name)
        set_played(winner["id"], True)
        need = 1 if private else REROLL_VOTES
        text = (f"{note}🎡 <b>{esc(wheel['name'])}</b> — выпало:\n"
                f"{winner['emoji']} <b>{esc(winner['name'])}</b>\n"
                f"Тир: {esc(winner['tier'])} · шанс был {pct:.1f}%\n"
                f"Крутил(а): {esc(user_name)}\n\n"
                f"<i>Ещё не выпадали в этот вечер: {len(pool) - 1}</i>")
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                               reply_markup=result_markup(spin_id, 0, need))


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


async def cmd_tierlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for chunk in tierlist_chunks(active_wheel(update.effective_chat.id)):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    w["chat_id"] = update.effective_chat.id
    await update.message.reply_text(stats_text(w), parse_mode=ParseMode.HTML)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    reset_played(w["id"])
    await update.message.reply_text(f"♻️ Новый вечер! Все пункты колеса «{w['name']}» снова в игре.")


async def cmd_wheels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = wheels_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_weights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = active_wheel(update.effective_chat.id)
    await update.message.reply_text(weights_text(w), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup([[
                                        InlineKeyboardButton("✏️ Изменить", callback_data="wh|wtsedit")]]))


# ═══ Диалоги: добавление пунктов / коэффициенты / новое колесо ════════════════
def _cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="wh|cancel")]])


def _add_prompt(wheel, tiers) -> str:
    names = ", ".join(t["name"] for t in tiers)
    tier_hint = (f"\nЧтобы сразу указать тир, пиши «Название | Тир» (тиры: {esc(names)})."
                 if len(tiers) > 1 else "")
    return (f"✏️ Колесо «{esc(wheel['name'])}». Пришли название или целый список — по одному пункту на строке."
            f"{tier_hint}\nОтмена — /cancel.")


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
        what = f"«{esc(pending[0])}»"
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
    await _safe_edit(query, commit_items(w["id"], ready + [(n, tid) for n in pending], errors))
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
        menu_text(w) + "\n\nКолесо создано и выбрано активным. Добавь пункты (➕), а если нужны тиры "
                       "с разными шансами — настрой ⚖️ коэффициенты. Пока у всех пунктов равные шансы.",
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
        row.append(InlineKeyboardButton(f"{g['emoji']} {g['name']}"[:30], callback_data=f"wh|g|{g['id']}|{page}"))
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
    total = sum(g["weight"] for g in items_all(item["wheel_id"]))
    text = (f"<b>{esc(item['name'])}</b>\n"
            f"Тир: {item['emoji']} {esc(item['tier'])} (коэф. {fmt_w(item['weight'])}) · "
            f"шанс {100 * item['weight'] / total:.1f}%\n\nПеренести в другой тир:")
    rows = _tier_rows("wh|mv", tiers, item["tier_id"], extra="")
    # в callback нужен id пункта и страница: wh|mv|<item>|<tier>|<page>
    rows = [[InlineKeyboardButton(b.text, callback_data=f"wh|mv|{item['id']}|{b.callback_data.split('|')[2]}|{page}")
             for b in r] for r in rows]
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
        await context.bot.send_message(chat_id, stats_text(w), parse_mode=ParseMode.HTML)

    elif action == "reset":
        reset_played(w["id"])
        await _safe_edit(query, menu_text(w) + "\n\n♻️ Новый вечер! Все пункты снова в игре.", menu_markup())

    elif action == "wts":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Изменить", callback_data="wh|wtsedit"),
                                    InlineKeyboardButton("⬅️ Меню", callback_data="wh|menu")]])
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
            set_played(spin["item_id"], False)
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
            CallbackQueryHandler(add_entry, pattern=r"^wh\|add$"),
            CallbackQueryHandler(wts_entry, pattern=r"^wh\|wtsedit$"),
            CallbackQueryHandler(new_entry, pattern=r"^wh\|new$"),
        ],
        states={
            ADD_TEXT: [MessageHandler(text_filter, add_got_text)],
            ADD_TIER: [CallbackQueryHandler(add_got_tier, pattern=r"^wh\|addtier\|")],
            WTS_TEXT: [MessageHandler(text_filter, wts_got_text)],
            NEW_NAME: [MessageHandler(text_filter, new_got_name)],
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
    app.add_handler(CommandHandler("wheelstats", cmd_stats))
    app.add_handler(CommandHandler("newevening", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^wh\|"))
