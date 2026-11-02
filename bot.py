import asyncio
import html
import logging
import os
import re

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PollAnswer,
    ReplyParameters,
    Update,
)
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────── Настройки (.env) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
PAYMENTS_CHAT_ID = int(os.environ["PAYMENTS_CHAT_ID"])  # группа с проверкой оплат
ORDERS_CHAT_ID = int(os.environ["ORDERS_CHAT_ID"])      # группа с заказами и опросами
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
PAYMENT_DETAILS = os.getenv("PAYMENT_DETAILS", "Реквизиты не указаны").replace("\\n", "\n")
DB_PATH = os.getenv("DB_PATH", "orders.db")
# Часовой пояс для статистики «за сегодня» (смещение от UTC; Москва = 3)
UTC_OFFSET_HOURS = int(os.getenv("UTC_OFFSET_HOURS", "3"))


def _int_or_none(name: str):
    value = os.getenv(name, "").strip()
    return int(value) if value else None


# ID тем (если в группах включены темы). Пусто = писать в общую ленту.
# Узнать ID темы: написать /id внутри нужной темы.
PAYMENTS_THREAD_ID = _int_or_none("PAYMENTS_THREAD_ID")
ORDERS_THREAD_ID = _int_or_none("ORDERS_THREAD_ID")

# Стартовый прайс: количество: (звёзды, рубли).
# Используется ТОЛЬКО при самом первом запуске, дальше позициями управляет /admin.
DEFAULT_POSITIONS = {
    60: (15, 25), 100: (30, 45), 160: (45, 65), 200: (70, 95),
    260: (80, 110), 300: (110, 145), 360: (120, 160), 400: (130, 180),
    460: (150, 205), 500: (165, 225), 560: (180, 240), 600: (200, 265),
    660: (215, 280), 700: (225, 300), 760: (240, 315), 800: (265, 350),
}

# Тексты бота, которые можно менять в /admin: ключ -> (название, текст по умолчанию, доступные вставки)
DEFAULT_TEXTS = {
    "start_text": (
        "Выбор количества (после /start)",
        "Выберите количество (от {min} до {max}):",
        "{min}, {max}",
    ),
    "closed_text": (
        "Приём заказов приостановлен",
        "⏸ Приём заказов временно приостановлен. Загляните позже!",
        "",
    ),
    "ask_link": (
        "Просьба прислать ссылку",
        "🔗 Отправьте ссылку на пост (например, https://t.me/channel/12):",
        "",
    ),
    "ask_username": (
        "Просьба прислать юзернейм",
        "💬 Отправьте юзернейм (например, @username):",
        "",
    ),
    "review_text": (
        "После оформления заказа (ждёт принятия)",
        "🧾 Заказ #{order_id} отправлен на проверку. Как только его примут, я пришлю реквизиты для оплаты.",
        "{order_id}",
    ),
    "declined_text": (
        "Заказ не принят",
        "❌ Заказ #{order_id} не принят. Возможно, на эту публикацию уже идёт заказ. Свяжитесь с продавцом.",
        "{order_id}",
    ),
    "payment_text": (
        "Сообщение с реквизитами оплаты",
        "✅ Заказ #{order_id} принят.\n\nК оплате: {rub}₽ ({stars}⭐)\n\n" + html.escape(PAYMENT_DETAILS),
        "{order_id}, {rub}, {stars}",
    ),
    "payment_sent": (
        "После отправки скриншота оплаты",
        "Скриншот по заказу #{order_id} отправлен на проверку. Ожидайте подтверждения.",
        "{order_id}",
    ),
    "approved_text": (
        "Оплата подтверждена",
        "✅ Оплата по заказу #{order_id} подтверждена. Заказ принят в работу.",
        "{order_id}",
    ),
    "rejected_text": (
        "Оплата отклонена",
        "❌ Оплата по заказу #{order_id} не подтверждена. Если это ошибка — свяжитесь с продавцом.",
        "{order_id}",
    ),
    "done_text": (
        "Заказ выполнен (сообщение заказчику)",
        "✅ Ваш заказ #{order_id} выполнен: оттапано {tapped} из {quantity}.",
        "{order_id}, {tapped}, {quantity}",
    ),
}

# Варианты в опросе и сколько каждый вариант "весит" в общем счёте
POLL_OPTIONS = ["20", "40", "60", "80", "100", "тапали"]
POLL_VALUES = [20, 40, 60, 80, 100, 0]

LINK_RE = re.compile(r"^https?://t\.me/(c/\d+|[A-Za-z0-9_]{4,})(/\d+){1,2}/?(\?\S*)?$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")

id_router = Router()     # /id работает в любом чате, в т.ч. в группах
admin_router = Router()  # админ-панель, только для ADMIN_IDS в личке
router = Router()        # диалог с покупателем и кнопки в группах

admin_router.message.filter(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
router.message.filter(F.chat.type == "private")

db: aiosqlite.Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_id INTEGER NOT NULL,
    buyer_username TEXT,
    quantity INTEGER NOT NULL,
    link TEXT NOT NULL,
    target_username TEXT NOT NULL,
    price_stars INTEGER NOT NULL,
    price_rub INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_payment',
    payment_msg_id INTEGER,
    poll_msg_id INTEGER,
    poll_id TEXT,
    tapped INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    pay_method TEXT,
    payments_chat INTEGER,
    payments_thread INTEGER,
    orders_chat INTEGER,
    orders_thread INTEGER
);
CREATE TABLE IF NOT EXISTS votes (
    poll_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    option_id INTEGER NOT NULL,
    PRIMARY KEY (poll_id, user_id)
);
CREATE TABLE IF NOT EXISTS positions (
    quantity INTEGER PRIMARY KEY,
    stars INTEGER NOT NULL,
    rub INTEGER NOT NULL,
    in_menu INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS texts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
# Статусы заказа:
# pending_approval -> awaiting_payment -> pending_review -> in_progress -> closed -> delivered
#        └-> declined                          └-> rejected


class Order(StatesGroup):
    custom_qty = State()
    link = State()
    username = State()
    confirm = State()


class Admin(StatesGroup):
    add_qty = State()
    add_price = State()
    edit_price = State()
    edit_text = State()
    broadcast_msg = State()
    broadcast_confirm = State()


# ───────────────────────── Вспомогательное ─────────────────────────
def e(value) -> str:
    return html.escape(str(value))


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def get_order(order_id: int):
    cur = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    return await cur.fetchone()


async def poll_total(poll_id: str) -> int:
    cur = await db.execute(
        "SELECT option_id, COUNT(*) FROM votes WHERE poll_id = ? GROUP BY option_id",
        (poll_id,),
    )
    rows = await cur.fetchall()
    return sum(POLL_VALUES[opt] * cnt for opt, cnt in rows)


async def get_positions() -> list[tuple[int, int, int]]:
    cur = await db.execute("SELECT quantity, stars, rub FROM positions ORDER BY quantity")
    return [(r["quantity"], r["stars"], r["rub"]) for r in await cur.fetchall()]


async def get_position(qty: int):
    cur = await db.execute("SELECT stars, rub FROM positions WHERE quantity = ?", (qty,))
    row = await cur.fetchone()
    return (row["stars"], row["rub"]) if row else None


async def get_menu_positions() -> list[tuple[int, int, int]]:
    """Позиции, которые показаны кнопками в меню /start."""
    cur = await db.execute("SELECT quantity, stars, rub FROM positions WHERE in_menu = 1 ORDER BY quantity")
    return [(r["quantity"], r["stars"], r["rub"]) for r in await cur.fetchall()]


async def menu_settings() -> tuple[int, bool, bool]:
    """Кнопок в ряду, показывать «Максимальное количество», показывать «Ввести своё количество»."""
    cols = min(max(int(await get_setting("menu_cols", "3") or 3), 1), 4)
    show_max = await get_setting("btn_max", "1") == "1"
    show_custom = await get_setting("btn_custom", "1") == "1"
    return cols, show_max, show_custom


async def get_text(key: str, **values) -> str:
    cur = await db.execute("SELECT value FROM texts WHERE key = ?", (key,))
    row = await cur.fetchone()
    text = row["value"] if row else DEFAULT_TEXTS[key][1]
    for name, val in values.items():
        text = text.replace("{" + name + "}", str(val))
    return text


async def qty_kb(positions) -> InlineKeyboardMarkup:
    """Меню /start. positions — только те позиции, что показаны кнопками."""
    cols, show_max, show_custom = await menu_settings()
    rows, row = [], []
    for qty, _, _ in positions:
        row.append(btn(str(qty), f"qty:{qty}"))
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows and not show_max and not show_custom:
        show_custom = True  # меню не должно быть пустым
    if show_max:
        rows.append([btn("Максимальное количество", "qty:max")])
    if show_custom:
        rows.append([btn("✍️ Ввести своё количество", "qty:custom")])
    return markup(*rows)


async def send_start(target: Message):
    if not await orders_open() and target.chat.id not in ADMIN_IDS:
        await target.answer(await get_text("closed_text"))
        return
    positions = await get_positions()
    if not positions:
        await target.answer("Сейчас приём заказов закрыт.")
        return
    text = await get_text("start_text", min=positions[0][0], max=positions[-1][0])
    await target.answer(text, reply_markup=await qty_kb(await get_menu_positions()))


def confirm_kb():
    return markup(
        [btn("✅ Всё верно", "confirm:yes")],
        [btn("✏️ Изменить", "confirm:edit"), btn("❌ Отменить", "confirm:cancel")],
    )


def review_kb(order_id: int):
    return markup(
        [btn("✅ Оплата в ₽", f"adm:approve:{order_id}:rub"), btn("✅ Оплата в ⭐", f"adm:approve:{order_id}:stars")],
        [btn("❌ Отклонить", f"adm:reject:{order_id}")],
    )


def progress_kb(order_id: int, with_close: bool = True):
    rows = [[btn("📣 Сообщить о выполнении", f"adm:notify:{order_id}")]]
    if with_close:
        rows.append([btn("🔒 Закрыть опрос", f"adm:close:{order_id}")])
    return markup(*rows)


def payment_caption(o, status_line: str | None = None) -> str:
    buyer = f"@{e(o['buyer_username'])}" if o["buyer_username"] else f"id{o['buyer_id']}"
    lines = [
        "💳 Скриншот оплаты",
        "",
        f"🧾 Заказ #{o['id']}",
        f"👤 {buyer}",
        f"📦 Количество: {o['quantity']}",
        f"🔗 Ссылка: {e(o['link'])}",
        f"💬 Юзернейм: {e(o['target_username'])}",
        f"💰 Цена: {o['price_stars']}⭐ / {o['price_rub']}₽",
    ]
    if status_line:
        lines += ["", status_line]
    return "\n".join(lines)


def admin_name(user) -> str:
    return f"@{e(user.username)}" if user.username else e(user.full_name)


# ───────────────────────── Настройки, статистика, пользователи ─────────────────────────
PAID = "status IN ('in_progress', 'closed', 'delivered')"  # заказы с одобренной оплатой
PAY_LABELS = {"rub": " · оплата в ₽", "stars": " · оплата в ⭐"}
PERIODS = [("today", "Сегодня"), ("7d", "7 дней"), ("30d", "30 дней"), ("all", "Всё время")]
_OFF = f"{UTC_OFFSET_HOURS:+d} hours"

STATUS_LABELS = {
    "pending_approval": ("🆕", "ждёт принятия"),
    "awaiting_payment": ("🕓", "ждёт оплаты"),
    "pending_review": ("⏳", "ждёт проверки оплаты"),
    "in_progress": ("🔄", "в работе (идёт опрос)"),
    "closed": ("🔒", "опрос закрыт"),
    "delivered": ("✅", "выполнен, заказчик уведомлён"),
    "rejected": ("❌", "оплата отклонена"),
    "declined": ("🚫", "не принят (до оплаты)"),
}

# Аудитории рассылки: ключ -> (название, условие SQL по таблице users)
AUDIENCES = {
    "all": ("всем", "1 = 1"),
    "buyers": ("покупателям", f"user_id IN (SELECT buyer_id FROM orders WHERE {PAID})"),
    "active": ("активным за 30 дней", "last_seen >= datetime('now', '-30 days')"),
}

broadcast = {"running": False, "stop": False}
background_tasks: set = set()


ACTIVE_STATUSES = ("pending_approval", "awaiting_payment", "pending_review", "in_progress")


async def get_dest(kind: str) -> tuple[int, int | None]:
    """Куда писать: 'payments' (оплаты и проверка заказов) или 'orders' (заказы и опросы).
    Место, заданное командой /set_payments или /set_orders, важнее значений из .env."""
    default_chat = PAYMENTS_CHAT_ID if kind == "payments" else ORDERS_CHAT_ID
    default_thread = PAYMENTS_THREAD_ID if kind == "payments" else ORDERS_THREAD_ID
    chat = await get_setting(f"{kind}_chat")
    if chat:
        thread = await get_setting(f"{kind}_thread")
        return int(chat), (int(thread) if thread else None)
    return default_chat, default_thread


async def order_dest(order, kind: str) -> tuple[int, int | None]:
    """Место, куда реально ушли сообщения этого заказа (на случай, если настройки потом поменяли)."""
    chat = order[f"{kind}_chat"]
    if chat:
        return chat, order[f"{kind}_thread"]
    return await get_dest(kind)


def norm_link(link: str) -> str:
    link = link.strip().lower().split("?")[0].rstrip("/")
    return re.sub(r"^https?://", "", link)


async def find_duplicates(order) -> list:
    """Другие активные заказы на ту же ссылку."""
    marks = ",".join("?" * len(ACTIVE_STATUSES))
    cur = await db.execute(
        f"SELECT id, link, target_username, status, quantity FROM orders "
        f"WHERE status IN ({marks}) AND id != ?",
        (*ACTIVE_STATUSES, order["id"]),
    )
    key = norm_link(order["link"])
    return [r for r in await cur.fetchall() if norm_link(r["link"]) == key]


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def in_period(column: str, period: str) -> str:
    if period == "today":
        return f"date({column}, '{_OFF}') = date('now', '{_OFF}')"
    if period == "7d":
        return f"{column} >= datetime('now', '-7 days')"
    if period == "30d":
        return f"{column} >= datetime('now', '-30 days')"
    return "1 = 1"


async def num(sql: str, params=()) -> int:
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return (row[0] or 0) if row else 0


async def get_setting(key: str, default: str = "") -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    await db.commit()


async def orders_open() -> bool:
    return await get_setting("orders_open", "1") == "1"


async def compute_stats(period: str) -> dict:
    p_created = in_period("created_at", period)
    p_paid = in_period("approved_at", period)
    s = {}
    s["created"] = await num(f"SELECT COUNT(*) FROM orders WHERE {p_created}")
    s["rejected"] = await num(
        f"SELECT COUNT(*) FROM orders WHERE status IN ('rejected', 'declined') AND {p_created}"
    )
    s["paid"] = await num(f"SELECT COUNT(*) FROM orders WHERE {PAID} AND {p_paid}")
    for method, col in (("rub", "price_rub"), ("stars", "price_stars")):
        where = f"{PAID} AND pay_method = '{method}' AND {p_paid}"
        s[method] = await num(f"SELECT SUM({col}) FROM orders WHERE {where}")
        s[method + "_cnt"] = await num(f"SELECT COUNT(*) FROM orders WHERE {where}")
    s["unknown"] = await num(f"SELECT COUNT(*) FROM orders WHERE {PAID} AND pay_method IS NULL AND {p_paid}")
    s["sold"] = await num(f"SELECT SUM(quantity) FROM orders WHERE {PAID} AND {p_paid}")
    s["tapped"] = await num(
        f"SELECT SUM(tapped) FROM orders WHERE status IN ('closed', 'delivered') AND {p_paid}"
    )
    s["users_total"] = await num("SELECT COUNT(*) FROM users")
    s["users_blocked"] = await num("SELECT COUNT(*) FROM users WHERE blocked = 1")
    s["users_new"] = await num(f"SELECT COUNT(*) FROM users WHERE {in_period('first_seen', period)}")
    s["users_active"] = await num(f"SELECT COUNT(*) FROM users WHERE {in_period('last_seen', period)}")
    s["buyers"] = await num(f"SELECT COUNT(DISTINCT buyer_id) FROM orders WHERE {PAID} AND {p_paid}")
    cur = await db.execute(
        f"SELECT quantity, COUNT(*) AS c FROM orders WHERE {PAID} AND {p_paid} "
        "GROUP BY quantity ORDER BY c DESC, quantity LIMIT 3"
    )
    s["top"] = [(r["quantity"], r["c"]) for r in await cur.fetchall()]
    s["pending"] = await num("SELECT COUNT(*) FROM orders WHERE status = 'pending_review'")
    s["in_progress"] = await num("SELECT COUNT(*) FROM orders WHERE status = 'in_progress'")
    s["awaiting"] = await num("SELECT COUNT(*) FROM orders WHERE status = 'awaiting_payment'")
    s["approval"] = await num("SELECT COUNT(*) FROM orders WHERE status = 'pending_approval'")
    return s


async def audience_ids(aud: str) -> list[int]:
    clause = AUDIENCES[aud][1]
    cur = await db.execute(f"SELECT user_id FROM users WHERE blocked = 0 AND {clause} ORDER BY user_id")
    return [r["user_id"] for r in await cur.fetchall()]


class TrackUsers(BaseMiddleware):
    """Запоминает всех, кто пишет боту в личку (для статистики и рассылки)."""

    async def __call__(self, handler, event: Update, data):
        user = None
        if event.message and event.message.chat.type == "private":
            user = event.message.from_user
        elif (
            event.callback_query
            and event.callback_query.message
            and event.callback_query.message.chat.type == "private"
        ):
            user = event.callback_query.from_user
        if user and not user.is_bot:
            try:
                await db.execute(
                    "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
                    "first_name = excluded.first_name, last_seen = CURRENT_TIMESTAMP, blocked = 0",
                    (user.id, user.username, user.first_name),
                )
                await db.commit()
            except Exception:
                logging.exception("Не удалось сохранить пользователя %s", user.id)
        return await handler(event, data)


# ───────────────────────── /id ─────────────────────────
@id_router.message(Command("id"))
async def cmd_id(message: Message):
    thread = message.message_thread_id if (message.is_topic_message or message.chat.is_forum) else None
    await message.reply(
        f"ID чата: <code>{message.chat.id}</code>\n"
        f"ID темы: <code>{thread if thread else 'нет (общая тема)'}</code>"
    )


@id_router.message(Command("set_orders", "set_payments"))
async def cmd_set_dest(message: Message):
    """Отправьте команду внутри нужной темы группы — бот запомнит это место."""
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Эту команду нужно отправить в группе, внутри той темы, куда должны приходить сообщения.")
        return
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    kind = "orders" if "set_orders" in message.text.split()[0] else "payments"
    thread = message.message_thread_id if (message.is_topic_message or message.chat.is_forum) else None
    await set_setting(f"{kind}_chat", str(message.chat.id))
    await set_setting(f"{kind}_thread", str(thread) if thread else "")
    what = "заказы и опросы" if kind == "orders" else "оплаты и подтверждение заказов"
    await message.reply(
        f"✅ Готово. Сюда будут приходить: {what}.\n"
        f"Чат: <code>{message.chat.id}</code>, тема: <code>{thread if thread else 'общая'}</code>"
    )


# ───────────────────────── Подтверждение заказа до оплаты ─────────────────────────
def approval_kb(order_id: int):
    return markup([btn("✅ Принять", f"adm:accept:{order_id}"), btn("❌ Отклонить", f"adm:decline:{order_id}")])


async def approval_text(order, status_line: str | None = None) -> str:
    buyer = f"@{e(order['buyer_username'])}" if order["buyer_username"] else f"id{order['buyer_id']}"
    lines = [
        f"🆕 <b>Новый заказ #{order['id']}</b> — на подтверждение",
        "",
        f"👤 {buyer}",
        f"📦 Количество: {order['quantity']}",
        f"🔗 Ссылка: {e(order['link'])}",
        f"💬 Юзернейм: {e(order['target_username'])}",
        f"💰 Цена: {order['price_stars']}⭐ / {order['price_rub']}₽",
    ]
    if status_line:
        lines += ["", status_line]
        return "\n".join(lines)
    dups = await find_duplicates(order)
    if dups:
        lines += ["", "⚠️ <b>На эту ссылку уже есть активные заказы:</b>"]
        for d in dups:
            label = STATUS_LABELS.get(d["status"], ("•", d["status"]))[1]
            lines.append(f"• #{d['id']} — {label}, {d['quantity']} → {e(d['target_username'])}")
    else:
        lines += ["", "✅ Активных заказов на эту ссылку нет."]
    return "\n".join(lines)


async def notify_admins(bot: Bot, text: str, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except TelegramAPIError:
            logging.warning("Не удалось написать админу %s", admin_id)


async def send_for_approval(bot: Bot, order):
    """Присылает новый заказ админам на подтверждение (в группу, а если не вышло — в личку)."""
    chat, thread = await get_dest("payments")
    text = await approval_text(order)
    try:
        await bot.send_message(chat, text, reply_markup=approval_kb(order["id"]), message_thread_id=thread)
    except TelegramAPIError as exc:
        logging.exception("Не удалось отправить заказ %s на подтверждение", order["id"])
        await notify_admins(
            bot,
            f"⚠️ Не удалось отправить заказ в группу: {e(exc.message)}\n"
            "Проверьте /admin → «🛠 Группы и темы». Заказ можно принять прямо здесь.\n\n" + text,
            reply_markup=approval_kb(order["id"]),
        )


# ───────────────────────── Диалог с покупателем ─────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await send_start(message)


@router.callback_query(F.data.startswith("qty:"))
async def on_qty(cb: CallbackQuery, state: FSMContext):
    if not await orders_open() and cb.from_user.id not in ADMIN_IDS:
        await cb.message.answer(await get_text("closed_text"))
        await cb.answer()
        return
    positions = await get_positions()
    if not positions:
        await cb.answer("Сейчас приём заказов закрыт", show_alert=True)
        return
    value = cb.data.split(":")[1]
    if value == "custom":
        await state.set_state(Order.custom_qty)
        await cb.message.answer(
            f"Введите количество из прайса (от {positions[0][0]} до {positions[-1][0]}):"
        )
        await cb.answer()
        return
    if value == "max":  # самая большая из позиций, показанных кнопками (если все скрыты — из всех)
        qty = ((await get_menu_positions()) or positions)[-1][0]
    else:
        qty = int(value)
    if not await get_position(qty):
        await cb.answer("Эта позиция больше недоступна", show_alert=True)
        return
    await state.update_data(quantity=qty)
    await state.set_state(Order.link)
    await cb.message.answer(await get_text("ask_link"))
    await cb.answer()


@router.message(Order.custom_qty, F.text)
async def on_custom_qty(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not await get_position(int(text)):
        available = ", ".join(str(q) for q, _, _ in await get_positions())
        await message.answer(f"Такого количества нет в прайсе. Доступно: {available}.\nПопробуйте ещё раз:")
        return
    await state.update_data(quantity=int(text))
    await state.set_state(Order.link)
    await message.answer(await get_text("ask_link"))


@router.message(Order.link, F.text)
async def on_link(message: Message, state: FSMContext):
    link = message.text.strip()
    if not LINK_RE.match(link):
        await message.answer("Это не похоже на ссылку на пост. Формат: https://t.me/channel/12")
        return
    await state.update_data(link=link)
    await state.set_state(Order.username)
    await message.answer(await get_text("ask_username"))


@router.message(Order.username, F.text)
async def on_username(message: Message, state: FSMContext):
    raw = message.text.strip()
    if not USERNAME_RE.match(raw):
        await message.answer("Юзернейм должен выглядеть так: @username (латиница, цифры, _, от 5 символов).")
        return
    target = "@" + raw.lstrip("@")
    data = await state.get_data()
    price = await get_position(data["quantity"])
    if not price:
        await state.clear()
        await message.answer("Эта позиция больше недоступна. Нажмите /start, чтобы выбрать заново.")
        return
    stars, rub = price
    await state.update_data(target=target, stars=stars, rub=rub)
    await state.set_state(Order.confirm)
    await message.answer(
        "Проверьте данные:\n\n"
        f"📦 Количество: {data['quantity']}\n"
        f"🔗 Ссылка: {e(data['link'])}\n"
        f"💬 Юзернейм: {e(target)}\n"
        f"💰 Цена: {stars}⭐ / {rub}₽\n\n"
        "Всё верно?",
        reply_markup=confirm_kb(),
    )


@router.callback_query(Order.confirm, F.data.startswith("confirm:"))
async def on_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    action = cb.data.split(":")[1]
    await cb.message.edit_reply_markup(reply_markup=None)

    if action == "cancel":
        await state.clear()
        await cb.message.answer("Заказ отменён. Чтобы начать заново, нажмите /start")
        await cb.answer()
        return

    if action == "edit":
        await state.clear()
        await send_start(cb.message)
        await cb.answer()
        return

    data = await state.get_data()
    if not all(k in data for k in ("quantity", "link", "target", "stars", "rub")):
        await cb.message.answer("Сессия устарела, начните заново: /start")
        await cb.answer()
        return

    cur = await db.execute(
        "INSERT INTO orders (buyer_id, buyer_username, quantity, link, target_username, price_stars, price_rub, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_approval')",
        (cb.from_user.id, cb.from_user.username, data["quantity"], data["link"], data["target"],
         data["stars"], data["rub"]),
    )
    await db.commit()
    order_id = cur.lastrowid
    await state.clear()

    await cb.message.answer(await get_text("review_text", order_id=order_id))
    await send_for_approval(bot, await get_order(order_id))
    await cb.answer()


@router.message(F.photo)
async def on_payment_screenshot(message: Message, bot: Bot):
    cur = await db.execute(
        "SELECT * FROM orders WHERE buyer_id = ? AND status = 'awaiting_payment' ORDER BY id DESC LIMIT 1",
        (message.from_user.id,),
    )
    order = await cur.fetchone()
    if not order:
        waiting = await num(
            "SELECT COUNT(*) FROM orders WHERE buyer_id = ? AND status = 'pending_approval'",
            (message.from_user.id,),
        )
        await message.answer(
            "Ваш заказ ещё не принят — реквизиты пришлю сразу после подтверждения."
            if waiting else "Нет заказа, ожидающего оплаты. Чтобы создать заказ, нажмите /start"
        )
        return

    chat, thread = await get_dest("payments")
    try:
        sent = await bot.send_photo(
            chat,
            photo=message.photo[-1].file_id,
            caption=payment_caption(order),
            reply_markup=review_kb(order["id"]),
            message_thread_id=thread,
        )
    except TelegramAPIError as exc:
        logging.exception("Не удалось отправить скриншот заказа %s", order["id"])
        await notify_admins(
            bot,
            f"⚠️ Не удалось отправить скриншот оплаты по заказу #{order['id']} в группу: {e(exc.message)}\n"
            "Проверьте /admin → «🛠 Группы и темы».",
        )
        await message.answer("Не получилось передать скриншот на проверку. Продавец уже знает, "
                             "попробуйте отправить его ещё раз чуть позже.")
        return
    await db.execute(
        "UPDATE orders SET status = 'pending_review', payment_msg_id = ?, payments_chat = ?, payments_thread = ? "
        "WHERE id = ?",
        (sent.message_id, chat, thread, order["id"]),
    )
    await db.commit()
    await message.answer(await get_text("payment_sent", order_id=order["id"]))


# ───────────────────────── Действия администратора над заказами ─────────────────────────
async def safe_notify(bot: Bot, user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        logging.warning("Не удалось написать пользователю %s", user_id)
        return False


@router.callback_query(F.data.startswith("adm:"))
async def on_admin_action(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return

    parts = cb.data.split(":")
    action, raw_id = parts[1], parts[2]
    method = parts[3] if len(parts) > 3 else None
    order = await get_order(int(raw_id))
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    oid = order["id"]

    # ---- Принять заказ (до оплаты) ----
    if action == "accept":
        res = await db.execute(
            "UPDATE orders SET status = 'awaiting_payment' WHERE id = ? AND status = 'pending_approval'", (oid,)
        )
        await db.commit()
        if res.rowcount == 0:
            await cb.answer("Заказ уже обработан", show_alert=True)
            return
        delivered = await safe_notify(
            bot, order["buyer_id"],
            await get_text("payment_text", order_id=oid, rub=order["price_rub"], stars=order["price_stars"]),
        )
        note = "реквизиты отправлены" if delivered else "⚠️ не удалось написать покупателю"
        await cb.message.edit_text(
            await approval_text(order, f"✅ Принят ({admin_name(cb.from_user)}) — {note}"), reply_markup=None
        )
        await cb.answer("Принято")
        return

    # ---- Отклонить заказ (до оплаты) ----
    if action == "decline":
        res = await db.execute(
            "UPDATE orders SET status = 'declined' WHERE id = ? AND status = 'pending_approval'", (oid,)
        )
        await db.commit()
        if res.rowcount == 0:
            await cb.answer("Заказ уже обработан", show_alert=True)
            return
        await safe_notify(bot, order["buyer_id"], await get_text("declined_text", order_id=oid))
        await cb.message.edit_text(
            await approval_text(order, f"❌ Отклонён ({admin_name(cb.from_user)})"), reply_markup=None
        )
        await cb.answer("Отклонено")
        return

    # ---- Одобрить оплату ----
    if action == "approve":
        res = await db.execute(
            "UPDATE orders SET status = 'in_progress', approved_at = CURRENT_TIMESTAMP, pay_method = ? "
            "WHERE id = ? AND status = 'pending_review'",
            (method if method in ("rub", "stars") else None, oid),
        )
        await db.commit()
        if res.rowcount == 0:
            await cb.answer("Заказ уже обработан", show_alert=True)
            return

        o_chat, o_thread = await get_dest("orders")
        info = None
        try:
            info = await bot.send_message(
                o_chat,
                f"🔗 {e(order['link'])}\n💬 {e(order['target_username'])}\n📦 {order['quantity']}",
                message_thread_id=o_thread,
            )
            poll_msg = await bot.send_poll(
                o_chat,
                question="сколько тапнули?",
                options=POLL_OPTIONS,
                is_anonymous=False,
                allows_multiple_answers=False,
                message_thread_id=o_thread,
            )
        except TelegramAPIError as exc:
            logging.exception("Не удалось отправить заказ %s в группу заказов", oid)
            if info:
                try:
                    await info.delete()
                except TelegramAPIError:
                    pass
            # откатываем, чтобы заказ можно было одобрить ещё раз после исправления настроек
            await db.execute(
                "UPDATE orders SET status = 'pending_review', approved_at = NULL, pay_method = NULL WHERE id = ?",
                (oid,),
            )
            await db.commit()
            await cb.answer(
                f"Не удалось отправить заказ в группу заказов: {exc.message}\n"
                "Проверьте /admin → Группы и темы.",
                show_alert=True,
            )
            return

        await db.execute(
            "UPDATE orders SET poll_msg_id = ?, poll_id = ?, orders_chat = ?, orders_thread = ? WHERE id = ?",
            (poll_msg.message_id, poll_msg.poll.id, o_chat, o_thread, oid),
        )
        await db.commit()
        await cb.message.edit_caption(
            caption=payment_caption(
                order, f"✅ Одобрено{PAY_LABELS.get(method, '')} ({admin_name(cb.from_user)})"
            ),
            reply_markup=progress_kb(oid),
        )
        await safe_notify(bot, order["buyer_id"], await get_text("approved_text", order_id=oid))
        await cb.answer("Одобрено")
        return

    # ---- Отклонить оплату ----
    if action == "reject":
        res = await db.execute(
            "UPDATE orders SET status = 'rejected' WHERE id = ? AND status = 'pending_review'", (oid,)
        )
        await db.commit()
        if res.rowcount == 0:
            await cb.answer("Заказ уже обработан", show_alert=True)
            return
        await cb.message.edit_caption(
            caption=payment_caption(order, f"❌ Отклонено ({admin_name(cb.from_user)})"),
            reply_markup=None,
        )
        await safe_notify(bot, order["buyer_id"], await get_text("rejected_text", order_id=oid))
        await cb.answer("Отклонено")
        return

    # ---- Закрыть опрос вручную ----
    if action == "close":
        if not await finish_order(bot, oid):
            await cb.answer("Опрос уже закрыт", show_alert=True)
            return
        await cb.answer("Опрос закрыт")
        return

    # ---- Сообщить заказчику о выполнении ----
    if action == "notify":
        if order["status"] == "in_progress":
            await cb.answer("Сначала закройте опрос (или дождитесь, пока наберётся нужная сумма)", show_alert=True)
            return
        res = await db.execute("UPDATE orders SET status = 'delivered' WHERE id = ? AND status = 'closed'", (oid,))
        await db.commit()
        if res.rowcount == 0:
            await cb.answer("Заказчик уже уведомлён", show_alert=True)
            return
        await safe_notify(
            bot, order["buyer_id"],
            await get_text("done_text", order_id=oid, tapped=order["tapped"], quantity=order["quantity"]),
        )
        await cb.message.edit_caption(
            caption=payment_caption(
                order, f"✅ Одобрено\n📬 Заказчик уведомлён ({admin_name(cb.from_user)})"
            ),
            reply_markup=None,
        )
        await cb.answer("Заказчик уведомлён")


# ───────────────────────── Опрос и завершение ─────────────────────────
async def finish_order(bot: Bot, order_id: int) -> bool:
    """Закрывает опрос и пишет итог в обе группы. Возвращает False, если уже закрыт."""
    res = await db.execute("UPDATE orders SET status = 'closed' WHERE id = ? AND status = 'in_progress'", (order_id,))
    await db.commit()
    if res.rowcount == 0:
        return False

    order = await get_order(order_id)
    tapped = min(await poll_total(order["poll_id"]), order["quantity"])
    await db.execute("UPDATE orders SET tapped = ? WHERE id = ?", (tapped, order_id))
    await db.commit()

    o_chat, o_thread = await order_dest(order, "orders")
    p_chat, p_thread = await order_dest(order, "payments")

    try:
        await bot.stop_poll(o_chat, order["poll_msg_id"])
    except TelegramAPIError:
        logging.warning("Не удалось остановить опрос заказа %s", order_id)

    verb = "выполнен" if tapped >= order["quantity"] else "закрыт"
    text = f"✅ Заказ #{order_id} {verb}: оттапано {tapped} из {order['quantity']}"

    for chat, thread, reply_to in (
        (o_chat, o_thread, order["poll_msg_id"]),
        (p_chat, p_thread, order["payment_msg_id"]),
    ):
        try:
            await bot.send_message(
                chat, text, message_thread_id=thread, reply_parameters=ReplyParameters(message_id=reply_to)
            )
        except TelegramAPIError:
            logging.exception("Не удалось отправить итог заказа %s в чат %s", order_id, chat)
    try:
        await bot.edit_message_reply_markup(
            p_chat, order["payment_msg_id"], reply_markup=progress_kb(order_id, with_close=False)
        )
    except TelegramAPIError:
        pass
    return True


@router.poll_answer()
async def on_poll_answer(answer: PollAnswer, bot: Bot):
    if answer.user is None:  # голос от имени канала/чата — игнорируем
        return
    cur = await db.execute(
        "SELECT id, quantity FROM orders WHERE poll_id = ? AND status = 'in_progress'", (answer.poll_id,)
    )
    order = await cur.fetchone()
    if not order:
        return

    if answer.option_ids:
        await db.execute(
            "INSERT OR REPLACE INTO votes (poll_id, user_id, option_id) VALUES (?, ?, ?)",
            (answer.poll_id, answer.user.id, answer.option_ids[0]),
        )
    else:  # человек отозвал голос
        await db.execute(
            "DELETE FROM votes WHERE poll_id = ? AND user_id = ?", (answer.poll_id, answer.user.id)
        )
    await db.commit()

    if await poll_total(answer.poll_id) >= order["quantity"]:
        await finish_order(bot, order["id"])


# ───────────────────────── Админ-панель (/admin) ─────────────────────────
def parse_price(text: str):
    nums = re.findall(r"\d+", text)
    if len(nums) != 2:
        return None
    stars, rub = int(nums[0]), int(nums[1])
    return (stars, rub) if stars > 0 and rub > 0 else None


async def show(cb: CallbackQuery, view):
    text, kb = view
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


async def home_view():
    s = await compute_stats("today")
    is_open = await orders_open()
    text = (
        "⚙️ <b>Админ-панель</b>\n\n"
        f"Сегодня: оплат {fmt(s['paid'])} · {fmt(s['rub'])} ₽ / {fmt(s['stars'])} ⭐\n"
        f"Ждут принятия: {s['approval']} · ждут проверки оплаты: {s['pending']}\n"
        f"Приём заказов: {'🟢 включён' if is_open else '⏸ приостановлен'}"
    )
    toggle = "⏸ Приостановить приём заказов" if is_open else "▶️ Возобновить приём заказов"
    return text, markup(
        [btn("📊 Статистика", "ap:st:today"), btn("📋 Заказы", "ap:ord:all")],
        [btn("📦 Позиции и цены", "ap:pos"), btn("✏️ Тексты бота", "ap:txt")],
        [btn("📣 Рассылка", "ap:bc"), btn("🛠 Группы и темы", "ap:dest")],
        [btn(toggle, "ap:tog")],
    )


async def stats_view(period: str):
    labels = dict(PERIODS)
    if period not in labels:
        period = "today"
    s = await compute_stats(period)
    avg = f", средний чек {fmt(s['rub'] / s['rub_cnt'])} ₽" if s["rub_cnt"] else ""
    top = ", ".join(f"{q} ({c})" for q, c in s["top"]) or "—"
    lines = [
        f"📊 <b>Статистика — {labels[period]}</b>",
        "",
        "🧾 <b>Заказы</b>",
        f"• Создано: {fmt(s['created'])}",
        f"• Оплачено: {fmt(s['paid'])} · отклонено: {fmt(s['rejected'])}",
        f"• Продано: {fmt(s['sold'])} тапов (выполнено {fmt(s['tapped'])})",
        "",
        "💰 <b>Выручка</b>",
        f"• {fmt(s['rub'])} ₽ (заказов: {s['rub_cnt']}{avg})",
        f"• {fmt(s['stars'])} ⭐ (заказов: {s['stars_cnt']})",
    ]
    if s["unknown"]:
        lines.append(f"• Способ оплаты не указан: {s['unknown']} (старые заказы)")
    lines += [
        "",
        "👥 <b>Пользователи</b>",
        f"• Всего в базе: {fmt(s['users_total'])} (заблокировали бота: {s['users_blocked']})",
        f"• Новых: {fmt(s['users_new'])}",
        f"• Активных: {fmt(s['users_active'])}",
        f"• Покупателей: {fmt(s['buyers'])}",
        "",
        f"🔥 <b>Популярные позиции:</b> {top}",
        "",
        "⏳ <b>Прямо сейчас</b>",
        f"• Ждут принятия: {s['approval']}",
        f"• Ждут проверки оплаты: {s['pending']}",
        f"• В работе: {s['in_progress']}",
        f"• Ждут оплаты от клиента: {s['awaiting']}",
    ]
    tabs = [btn(("• " if key == period else "") + label, f"ap:st:{key}") for key, label in PERIODS]
    return "\n".join(lines), markup(tabs[:2], tabs[2:], [btn("⬅️ Назад", "ap:home")])


async def orders_view(flt: str):
    where = "status IN ('pending_approval', 'pending_review')" if flt == "pending" else "1 = 1"
    cur = await db.execute(
        f"SELECT id, quantity, price_rub, status FROM orders WHERE {where} ORDER BY id DESC LIMIT 10"
    )
    orders = await cur.fetchall()
    rows = [
        [btn(f"{STATUS_LABELS.get(o['status'], ('•', ''))[0]} #{o['id']} · {o['quantity']} · {o['price_rub']}₽",
             f"ap:o:{o['id']}")]
        for o in orders
    ]
    rows.append([
        btn(("• " if flt != "pending" else "") + "Все", "ap:ord:all"),
        btn(("• " if flt == "pending" else "") + "⏳ Ждут решения", "ap:ord:pending"),
    ])
    rows.append([btn("⬅️ Назад", "ap:home")])
    title = "⏳ Ждут решения" if flt == "pending" else "📋 Последние заказы"
    text = f"<b>{title}</b>\n\nНажмите на заказ, чтобы увидеть подробности." if orders else f"<b>{title}</b>\n\nПусто."
    return text, markup(*rows)


async def order_card(oid: int):
    cur = await db.execute(
        "SELECT *, datetime(created_at, ?) AS c_local, datetime(approved_at, ?) AS a_local "
        "FROM orders WHERE id = ?",
        (_OFF, _OFF, oid),
    )
    o = await cur.fetchone()
    if not o:
        return await orders_view("all")
    icon, label = STATUS_LABELS.get(o["status"], ("•", o["status"]))
    buyer = f"@{e(o['buyer_username'])}" if o["buyer_username"] else "без юзернейма"
    method = {"rub": "₽", "stars": "⭐"}.get(o["pay_method"], "—")
    lines = [
        f"🧾 <b>Заказ #{o['id']}</b> — {icon} {label}",
        "",
        f"👤 Покупатель: {buyer} (id {o['buyer_id']})",
        f"📦 Количество: {o['quantity']}",
        f"🔗 Ссылка: {e(o['link'])}",
        f"💬 Юзернейм: {e(o['target_username'])}",
        f"💰 Цена: {o['price_stars']}⭐ / {o['price_rub']}₽",
        f"💳 Оплачено в: {method}",
        f"🎯 Оттапано: {o['tapped']} из {o['quantity']}",
        f"🕓 Создан: {o['c_local']}",
    ]
    if o["a_local"]:
        lines.append(f"✅ Одобрен: {o['a_local']}")
    return "\n".join(lines), markup([btn("⬅️ К списку", "ap:ord:all")])


async def broadcast_view():
    rows = []
    for key, (label, _) in AUDIENCES.items():
        count = len(await audience_ids(key))
        rows.append([btn(f"{label.capitalize()} ({count})", f"ap:ba:{key}")])
    rows.append([btn("⬅️ Назад", "ap:home")])
    note = "\n\n⚠️ Сейчас уже идёт другая рассылка." if broadcast["running"] else ""
    return (
        "📣 <b>Рассылка</b>\n\nКому отправить? В скобках — сколько получателей "
        "(заблокировавшие бота не учитываются)." + note,
        markup(*rows),
    )


async def dest_view(report: str = ""):
    lines = ["🛠 <b>Группы и темы</b>", ""]
    for kind, label in (("payments", "💳 Оплаты и подтверждение заказов"), ("orders", "📦 Заказы и опросы")):
        chat, thread = await get_dest(kind)
        source = "задано командой" if await get_setting(f"{kind}_chat") else "из .env"
        lines.append(f"{label}\nчат <code>{chat}</code>, тема <code>{thread or 'общая'}</code> ({source})\n")
    lines += [
        "Чтобы выбрать место, зайдите в нужную тему группы и отправьте там команду:",
        "<code>/set_payments</code> — оплаты и подтверждение заказов",
        "<code>/set_orders</code> — заказы и опросы",
    ]
    if report:
        lines += ["", report]
    return "\n".join(lines), markup([btn("🔍 Проверить отправку", "ap:dtest")], [btn("⬅️ Назад", "ap:home")])


async def test_dest(bot: Bot) -> str:
    out = []
    for kind, label in (("payments", "💳 Оплаты"), ("orders", "📦 Заказы")):
        chat, thread = await get_dest(kind)
        try:
            await bot.send_message(chat, f"🔧 Проверка: сюда приходят «{label[2:]}».", message_thread_id=thread)
            out.append(f"{label}: ✅ сообщение доставлено")
        except TelegramAPIError as exc:
            out.append(f"{label}: ❌ {e(exc.message)}")
    return "\n".join(out)


async def positions_view():
    cur = await db.execute("SELECT quantity, stars, rub, in_menu FROM positions ORDER BY quantity")
    positions = await cur.fetchall()
    rows = [
        [btn(("🔘 " if p["in_menu"] else "▫️ ") + f"{p['quantity']} — {p['stars']}⭐ / {p['rub']}₽",
             f"ap:p:{p['quantity']}")]
        for p in positions
    ]
    rows.append([btn("➕ Добавить позицию", "ap:pa"), btn("🧩 Вид меню /start", "ap:menu")])
    rows.append([btn("⬅️ Назад", "ap:home")])
    text = (
        "📦 <b>Позиции и цены</b>\n\n"
        "🔘 — кнопка есть в меню /start\n▫️ — скрыта (заказать можно только вводом количества вручную)\n\n"
        "Нажмите на позицию, чтобы изменить цену, скрыть/показать кнопку или удалить её."
        if positions else "📦 Позиций пока нет. Добавьте первую."
    )
    return text, markup(*rows)


async def position_view(qty: int):
    cur = await db.execute("SELECT stars, rub, in_menu FROM positions WHERE quantity = ?", (qty,))
    row = await cur.fetchone()
    if not row:
        return await positions_view()
    in_menu = row["in_menu"] == 1
    status = "🔘 показана кнопкой в меню /start" if in_menu else "▫️ скрыта из меню (только ввод вручную)"
    return (
        f"📦 Позиция: <b>{qty}</b>\nЦена: {row['stars']}⭐ / {row['rub']}₽\n{status}",
        markup(
            [btn("🙈 Скрыть из меню" if in_menu else "👁 Показать в меню", f"ap:pv:{qty}")],
            [btn("✏️ Изменить цену", f"ap:pe:{qty}"), btn("🗑 Удалить", f"ap:pd:{qty}")],
            [btn("⬅️ К списку", "ap:pos")],
        ),
    )


async def menu_view():
    menu = await get_menu_positions()
    shown = {q for q, _, _ in menu}
    hidden = [q for q, _, _ in await get_positions() if q not in shown]
    cols, show_max, show_custom = await menu_settings()
    on = lambda flag: "✅" if flag else "❌"
    text = (
        "🧩 <b>Вид меню /start</b>\n\n"
        f"Кнопки позиций ({len(menu)}): {', '.join(str(q) for q in sorted(shown)) or '—'}\n"
        f"Скрыты: {', '.join(str(q) for q in hidden) or 'нет'}\n"
        f"Кнопок в ряду: {cols}\n"
        f"«Максимальное количество»: {on(show_max)}\n"
        f"«Ввести своё количество»: {on(show_custom)}"
    )
    if hidden and not show_custom:
        text += "\n\n⚠️ Скрытые позиции сейчас недоступны клиентам: кнопка ввода количества выключена."
    if not menu and not show_max and not show_custom:
        text += "\n\nℹ️ Пустого меню не бывает — клиентам покажется кнопка ввода количества."
    return text, markup(
        [btn(f"Кнопок в ряду: {cols} (нажмите, чтобы сменить)", "ap:mcols")],
        [btn(f"«Максимальное количество»: {on(show_max)}", "ap:mmax")],
        [btn(f"«Ввести своё количество»: {on(show_custom)}", "ap:mcust")],
        [btn("👁 Показать все кнопки", "ap:mall:1"), btn("🙈 Скрыть все", "ap:mall:0")],
        [btn("👀 Показать как у клиента", "ap:mprev")],
        [btn("⬅️ К позициям", "ap:pos")],
    )


async def texts_view():
    cur = await db.execute("SELECT key FROM texts")
    changed = {r["key"] for r in await cur.fetchall()}
    rows = [
        [btn(("✏️ " if key in changed else "") + label, f"ap:t:{key}")]
        for key, (label, _, _) in DEFAULT_TEXTS.items()
    ]
    rows.append([btn("⬅️ Назад", "ap:home")])
    return (
        "✏️ <b>Тексты бота</b>\n\nВыберите, какой текст изменить. Значком ✏️ отмечены уже изменённые.",
        markup(*rows),
    )


async def text_view(key: str):
    label, _, placeholders = DEFAULT_TEXTS[key]
    cur = await db.execute("SELECT 1 FROM texts WHERE key = ?", (key,))
    changed = await cur.fetchone() is not None
    current = await get_text(key)
    rows = [[btn("✏️ Изменить", f"ap:te:{key}")]]
    if changed:
        rows.append([btn("↩️ Вернуть стандартный", f"ap:tr:{key}")])
    rows.append([btn("⬅️ К списку", "ap:txt")])
    return (
        f"✏️ <b>{label}</b>\n"
        f"Вставки: {e(placeholders) if placeholders else 'нет'}\n\n"
        f"Текущий текст:\n──────────\n{current}\n──────────",
        markup(*rows),
    )


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await home_view()
    await message.answer(text, reply_markup=kb)


@admin_router.message(CommandStart())
async def admin_start(message: Message, state: FSMContext):
    await state.clear()  # /start должен работать и посреди админских действий
    await send_start(message)


@admin_router.callback_query(F.data.startswith("ap:"))
async def on_panel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()  # нужен для подтверждения рассылки
    await state.clear()
    parts = cb.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None

    if action == "home":
        view = await home_view()
    elif action == "tog":  # пауза / возобновление приёма заказов
        await set_setting("orders_open", "0" if await orders_open() else "1")
        view = await home_view()
    elif action == "st":
        view = await stats_view(arg or "today")
    elif action == "ord":
        view = await orders_view(arg or "all")
    elif action == "o":
        view = await order_card(int(arg))
    elif action == "pos":
        view = await positions_view()
    elif action == "p":
        view = await position_view(int(arg))
    elif action == "pe":  # изменить цену
        await state.set_state(Admin.edit_price)
        await state.update_data(qty=int(arg))
        view = (
            f"Введите новую цену для позиции <b>{arg}</b> в формате: звёзды рубли\nНапример: <code>30 45</code>",
            markup([btn("↩️ Отмена", f"ap:p:{arg}")]),
        )
    elif action == "pd":  # подтверждение удаления
        view = (
            f"Удалить позицию <b>{arg}</b>? Уже созданные заказы не пострадают.",
            markup([btn("🗑 Да, удалить", f"ap:pdy:{arg}"), btn("↩️ Нет", f"ap:p:{arg}")]),
        )
    elif action == "pdy":
        await db.execute("DELETE FROM positions WHERE quantity = ?", (int(arg),))
        await db.commit()
        view = await positions_view()
    elif action == "pa":  # добавить позицию
        await state.set_state(Admin.add_qty)
        view = ("Введите количество новой позиции (число), например <code>160</code>:",
                markup([btn("↩️ Отмена", "ap:pos")]))
    elif action == "txt":
        view = await texts_view()
    elif action == "t":
        view = await text_view(arg)
    elif action == "te":  # изменить текст
        await state.set_state(Admin.edit_text)
        await state.update_data(key=arg)
        placeholders = DEFAULT_TEXTS[arg][2]
        hint = f"\n\nМожно использовать вставки: {e(placeholders)}" if placeholders else ""
        view = (
            "Отправьте новый текст одним сообщением. Форматирование (жирный, курсив, ссылки) сохранится."
            + hint,
            markup([btn("↩️ Отмена", f"ap:t:{arg}")]),
        )
    elif action == "tr":  # сбросить текст
        await db.execute("DELETE FROM texts WHERE key = ?", (arg,))
        await db.commit()
        view = await text_view(arg)
    elif action == "pv":  # показать/скрыть кнопку позиции в меню
        await db.execute("UPDATE positions SET in_menu = 1 - in_menu WHERE quantity = ?", (int(arg),))
        await db.commit()
        view = await position_view(int(arg))
    elif action == "menu":
        view = await menu_view()
    elif action == "mcols":  # 1 → 2 → 3 → 4 → 1
        cols, _, _ = await menu_settings()
        await set_setting("menu_cols", str(cols % 4 + 1))
        view = await menu_view()
    elif action == "mmax":
        await set_setting("btn_max", "0" if (await menu_settings())[1] else "1")
        view = await menu_view()
    elif action == "mcust":
        await set_setting("btn_custom", "0" if (await menu_settings())[2] else "1")
        view = await menu_view()
    elif action == "mall":  # показать / скрыть все позиции сразу
        await db.execute("UPDATE positions SET in_menu = ?", (1 if arg == "1" else 0,))
        await db.commit()
        view = await menu_view()
    elif action == "mprev":  # меню глазами клиента (кнопки рабочие)
        positions = await get_positions()
        if not positions:
            await cb.answer("Позиций пока нет", show_alert=True)
            return
        text = await get_text("start_text", min=positions[0][0], max=positions[-1][0])
        await cb.message.answer(text, reply_markup=await qty_kb(await get_menu_positions()))
        await cb.answer("Так меню выглядит у клиента")
        return
    elif action == "dest":
        view = await dest_view()
    elif action == "dtest":
        view = await dest_view(await test_dest(bot))
    elif action == "bc":  # рассылка: выбор аудитории
        view = await broadcast_view()
    elif action == "ba":  # аудитория выбрана — ждём сообщение
        if arg not in AUDIENCES:
            view = await broadcast_view()
        else:
            count = len(await audience_ids(arg))
            await state.set_state(Admin.broadcast_msg)
            await state.update_data(audience=arg)
            view = (
                f"📣 <b>Рассылка — {AUDIENCES[arg][0]}</b>\n\nПолучателей: <b>{count}</b>.\n\n"
                "Отправьте сообщение для рассылки: текст, фото, видео — что угодно. "
                "Оно уйдёт получателям в том же виде, включая форматирование.",
                markup([btn("↩️ Отмена", "ap:bc")]),
            )
    elif action == "bgo":  # подтверждение рассылки
        if not data.get("src_msg"):
            await cb.answer("Рассылка уже запущена или отменена", show_alert=True)
            return
        if broadcast["running"]:
            await cb.answer("Другая рассылка ещё идёт", show_alert=True)
            return
        ids = await audience_ids(data["audience"])
        broadcast["running"] = True
        broadcast["stop"] = False
        task = asyncio.create_task(
            run_broadcast(bot, cb.message.chat.id, ids, data["src_chat"], data["src_msg"])
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        view = ("📣 Рассылка запущена. Ход выполнения — в сообщении ниже.",
                markup([btn("⬅️ В панель", "ap:home")]))
    elif action == "bstop":
        broadcast["stop"] = True
        await cb.answer("Останавливаю…")
        return
    else:
        view = await home_view()

    await show(cb, view)
    await cb.answer()


@admin_router.message(Admin.add_qty, F.text)
async def on_add_qty(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 1_000_000):
        await message.answer("Нужно целое число, например 160. Попробуйте ещё раз:")
        return
    qty = int(text)
    if await get_position(qty):
        await message.answer("Такая позиция уже есть. Введите другое количество или нажмите «Отмена».")
        return
    await state.update_data(qty=qty)
    await state.set_state(Admin.add_price)
    await message.answer(
        f"Введите цену для <b>{qty}</b> в формате: звёзды рубли\nНапример: <code>45 65</code>",
        reply_markup=markup([btn("↩️ Отмена", "ap:pos")]),
    )


@admin_router.message(Admin.add_price, F.text)
async def on_add_price(message: Message, state: FSMContext):
    price = parse_price(message.text)
    if not price:
        await message.answer("Нужно два числа: звёзды и рубли, например: 45 65")
        return
    qty = (await state.get_data())["qty"]
    await db.execute("INSERT OR REPLACE INTO positions (quantity, stars, rub) VALUES (?, ?, ?)", (qty, *price))
    await db.commit()
    await state.clear()
    await message.answer(
        f"✅ Позиция <b>{qty}</b> добавлена: {price[0]}⭐ / {price[1]}₽\nКнопка уже в меню /start (скрыть её можно в карточке позиции).",
        reply_markup=markup([btn("📦 К списку позиций", "ap:pos")]),
    )


@admin_router.message(Admin.edit_price, F.text)
async def on_edit_price(message: Message, state: FSMContext):
    price = parse_price(message.text)
    if not price:
        await message.answer("Нужно два числа: звёзды и рубли, например: 45 65")
        return
    qty = (await state.get_data())["qty"]
    await db.execute("UPDATE positions SET stars = ?, rub = ? WHERE quantity = ?", (*price, qty))
    await db.commit()
    await state.clear()
    await message.answer(
        f"✅ Цена позиции <b>{qty}</b> обновлена: {price[0]}⭐ / {price[1]}₽",
        reply_markup=markup([btn("📦 К списку позиций", "ap:pos")]),
    )


@admin_router.message(Admin.edit_text, F.text)
async def on_edit_text(message: Message, state: FSMContext):
    if len(message.html_text) > 3500:
        await message.answer("Слишком длинный текст, сократите его и отправьте снова.")
        return
    key = (await state.get_data())["key"]
    await db.execute("INSERT OR REPLACE INTO texts (key, value) VALUES (?, ?)", (key, message.html_text))
    await db.commit()
    await state.clear()
    await message.answer(
        "✅ Текст сохранён.", reply_markup=markup([btn("⬅️ К тексту", f"ap:t:{key}")])
    )


# ───────────────────────── Рассылка ─────────────────────────
@admin_router.message(Admin.broadcast_msg)
async def on_broadcast_msg(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Рассылка отменена: команду нужно отправить отдельно, не в рассылку.")
        return
    audience = (await state.get_data())["audience"]
    count = len(await audience_ids(audience))
    if count == 0:
        await state.clear()
        await message.answer("В этой аудитории нет получателей.")
        return
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id)
    await state.set_state(Admin.broadcast_confirm)
    await message.reply(
        f"Отправить это сообщение {AUDIENCES[audience][0]}? Получателей: <b>{count}</b>.",
        reply_markup=markup([btn("✅ Отправить", "ap:bgo")], [btn("↩️ Отмена", "ap:bc")]),
    )


async def run_broadcast(bot: Bot, admin_chat: int, ids: list[int], src_chat: int, src_msg: int):
    """Копирует сообщение админа каждому получателю с паузами, чтобы не упереться в лимиты Telegram."""
    total = len(ids)
    sent = blocked = failed = 0
    stopped = False
    stop_kb = markup([btn("🛑 Остановить", "ap:bstop")])
    try:
        status = await bot.send_message(admin_chat, f"📣 Рассылка: 0/{total}", reply_markup=stop_kb)
        for i, uid in enumerate(ids, 1):
            if broadcast["stop"]:
                stopped = True
                break
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                sent += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.copy_message(uid, src_chat, src_msg)
                    sent += 1
                except TelegramAPIError:
                    failed += 1
            except TelegramForbiddenError:
                blocked += 1
                await db.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (uid,))
                await db.commit()
            except TelegramAPIError:
                failed += 1
            if i % 20 == 0:
                try:
                    await status.edit_text(f"📣 Рассылка: {i}/{total}", reply_markup=stop_kb)
                except TelegramBadRequest:
                    pass
            await asyncio.sleep(0.05)
    finally:
        broadcast["running"] = False
        broadcast["stop"] = False

    report = (
        f"{'🛑 Рассылка остановлена' if stopped else '✅ Рассылка завершена'}\n\n"
        f"Доставлено: {sent} из {total}\n"
        f"Заблокировали бота: {blocked}\n"
        f"Другие ошибки: {failed}"
    )
    try:
        await status.edit_text(report, reply_markup=markup([btn("⬅️ В панель", "ap:home")]))
    except TelegramAPIError:
        await bot.send_message(admin_chat, report)


@router.my_chat_member(F.chat.type == "private")
async def on_bot_blocked(event: ChatMemberUpdated):
    blocked = 1 if event.new_chat_member.status in ("kicked", "left") else 0
    await db.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (blocked, event.chat.id))
    await db.commit()


@router.message(F.text, StateFilter(None))
async def fallback(message: Message):
    await message.answer("Чтобы сделать заказ, нажмите /start")


# ───────────────────────── Запуск ─────────────────────────
async def init_db():
    await db.executescript(SCHEMA)
    # миграция баз от прошлых версий: новые колонки в orders
    cur = await db.execute("PRAGMA table_info(orders)")
    cols = {r["name"] for r in await cur.fetchall()}
    for col, col_type in (
        ("approved_at", "TEXT"), ("pay_method", "TEXT"),
        ("payments_chat", "INTEGER"), ("payments_thread", "INTEGER"),
        ("orders_chat", "INTEGER"), ("orders_thread", "INTEGER"),
    ):
        if col not in cols:
            await db.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type}")
    cur = await db.execute("PRAGMA table_info(positions)")
    if "in_menu" not in {r["name"] for r in await cur.fetchall()}:
        await db.execute("ALTER TABLE positions ADD COLUMN in_menu INTEGER NOT NULL DEFAULT 1")
    await db.execute(
        "UPDATE orders SET approved_at = created_at "
        "WHERE approved_at IS NULL AND status IN ('in_progress', 'closed', 'delivered')"
    )
    # прошлые покупатели попадают в базу пользователей (для статистики и рассылки)
    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_seen, last_seen) "
        "SELECT buyer_id, MAX(buyer_username), MIN(created_at), MAX(created_at) FROM orders GROUP BY buyer_id"
    )
    cur = await db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] == 0:  # первый запуск: заливаем стартовый прайс
        await db.executemany(
            "INSERT OR IGNORE INTO positions (quantity, stars, rub) VALUES (?, ?, ?)",
            [(q, s, r) for q, (s, r) in DEFAULT_POSITIONS.items()],
        )
        await db.execute("PRAGMA user_version = 1")
    await db.commit()


async def main():
    global db
    logging.basicConfig(level=logging.INFO)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await init_db()
    for kind in ("payments", "orders"):
        chat, thread = await get_dest(kind)
        logging.info("Куда пишет бот [%s]: чат %s, тема %s", kind, chat, thread or "общая")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(TrackUsers())
    dp.include_router(id_router)
    dp.include_router(admin_router)
    dp.include_router(router)
    try:
        await dp.start_polling(bot)  # poll_answer подключается автоматически
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
