import asyncio
import logging
import os
import random
import sqlite3
import string
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "KasperGiftSupport").lstrip("@")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "kasper.sqlite3"))
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
WORKER_IDS = {int(x.strip()) for x in os.getenv("WORKER_IDS", "").split(",") if x.strip().isdigit()}

CURRENCIES = ["Stars", "TON", "RUB", "USDT"]
TOPUP_CURRENCIES = ["Stars", "TON", "RUB", "USDT", "UAH", "USD", "BYN"]

logging.basicConfig(
    filename="kasper.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger("kasper")

router = Router()


class OrderState(StatesGroup):
    choosing_role = State()
    choosing_item = State()
    choosing_currency = State()
    waiting_recipient = State()
    waiting_amount = State()
    waiting_description = State()
    binding_requisite = State()


@dataclass
class Order:
    tag: str
    creator_id: int
    creator_username: str
    role: str
    item_type: str
    currency: str
    recipient: str
    amount: float
    description: str
    buyer_username: str
    status: str


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                lang TEXT NOT NULL DEFAULT 'ru',
                rating REAL NOT NULL DEFAULT 0,
                success_orders INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS requisites (
                user_id INTEGER PRIMARY KEY,
                usdt TEXT,
                ton TEXT,
                card TEXT,
                stars_username TEXT
            );

            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, currency)
            );

            CREATE TABLE IF NOT EXISTS orders (
                tag TEXT PRIMARY KEY,
                creator_id INTEGER NOT NULL,
                creator_username TEXT NOT NULL,
                role TEXT NOT NULL,
                item_type TEXT NOT NULL,
                currency TEXT NOT NULL,
                recipient TEXT NOT NULL,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                buyer_username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for table, columns in {
            "users": {
                "lang": "TEXT NOT NULL DEFAULT 'ru'",
                "rating": "REAL NOT NULL DEFAULT 0",
                "success_orders": "INTEGER NOT NULL DEFAULT 0",
            },
            "requisites": {"stars_username": "TEXT"},
        }.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        conn.commit()


def remember_user(user) -> None:
    if not user:
        return
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO users(id, username, first_name)
            VALUES(?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user.id, user.username or "", user.first_name or ""),
        )
        conn.execute("INSERT OR IGNORE INTO requisites(user_id) VALUES(?)", (user.id,))
        conn.commit()


def get_lang(user_id: int | None) -> str:
    if not user_id:
        return "ru"
    with closing(db()) as conn:
        row = conn.execute("SELECT lang FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["lang"] if row and row["lang"] in {"ru", "en"} else "ru"


def set_lang(user_id: int, lang: str) -> None:
    remember_user(type("User", (), {"id": user_id, "username": "", "first_name": ""})())
    with closing(db()) as conn:
        conn.execute("UPDATE users SET lang = ? WHERE id = ?", (lang, user_id))
        conn.commit()


def get_requisites(user_id: int) -> sqlite3.Row:
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO requisites(user_id) VALUES(?)", (user_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM requisites WHERE user_id = ?", (user_id,)).fetchone()
    return row


def set_requisite(user_id: int, field: str, value: str) -> None:
    if field not in {"usdt", "ton", "card", "stars_username"}:
        return
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO requisites(user_id) VALUES(?)", (user_id,))
        conn.execute(f"UPDATE requisites SET {field} = ? WHERE user_id = ?", (value, user_id))
        conn.commit()


def missing_requisite(user_id: int, currency: str) -> tuple[str, str] | None:
    req = get_requisites(user_id)
    mapping = {
        "Stars": ("stars_username", "получатель звезд"),
        "TON": ("ton", "TON кошелек"),
        "USDT": ("usdt", "USDT (TON) кошелек"),
        "RUB": ("card", "карта/СПБ"),
    }
    field, label = mapping.get(currency, ("", ""))
    if field and not req[field]:
        return field, label
    return None


def add_balance(user_id: int, amount: float, currency: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO balances(user_id, currency, amount)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, currency) DO UPDATE SET amount = amount + excluded.amount
            """,
            (user_id, currency, amount),
        )
        conn.commit()


def get_balance(user_id: int, currency: str) -> float:
    with closing(db()) as conn:
        row = conn.execute("SELECT amount FROM balances WHERE user_id = ? AND currency = ?", (user_id, currency)).fetchone()
    return float(row["amount"]) if row else 0.0


def balance_lines(user_id: int) -> str:
    return "\n".join(f"• {cur}: {money(get_balance(user_id, cur))}" for cur in CURRENCIES)


def create_tag() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def save_order(data: dict, user) -> Order:
    tag = create_tag()
    order = Order(
        tag=tag,
        creator_id=user.id,
        creator_username=user.username or str(user.id),
        role=data["role"],
        item_type=data["item_type"],
        currency=data["currency"],
        recipient=data["recipient"],
        amount=float(data["amount"]),
        description=data["description"],
        buyer_username=data.get("buyer_username", data["recipient"].lstrip("@")),
        status="created",
    )
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO orders(tag, creator_id, creator_username, role, item_type, currency, recipient, amount, description, buyer_username, status)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.tag,
                order.creator_id,
                order.creator_username,
                order.role,
                order.item_type,
                order.currency,
                order.recipient,
                order.amount,
                order.description,
                order.buyer_username,
                order.status,
            ),
        )
        conn.commit()
    logger.info(
        "order_created tag=%s creator_id=%s username=%s role=%s item=%s currency=%s amount=%s recipient=%s",
        order.tag,
        order.creator_id,
        order.creator_username,
        order.role,
        order.item_type,
        order.currency,
        order.amount,
        order.recipient,
    )
    return order


def find_order(tag: str) -> Order | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM orders WHERE tag = ?", (tag,)).fetchone()
    if not row:
        return None
    return Order(
        tag=row["tag"],
        creator_id=row["creator_id"],
        creator_username=row["creator_username"],
        role=row["role"],
        item_type=row["item_type"],
        currency=row["currency"],
        recipient=row["recipient"],
        amount=row["amount"],
        description=row["description"],
        buyer_username=row["buyer_username"],
        status=row["status"],
    )


def recent_orders(user_id: int, limit: int = 5) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT tag, amount, currency, status FROM orders WHERE creator_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def pay_order(tag: str, user_id: int, lang: str) -> tuple[bool, str]:
    order = find_order(tag)
    if not order:
        return False, "Order not found." if lang == "en" else "Ордер не найден."
    if order.status == "paid":
        return False, "Order is already paid." if lang == "en" else "Ордер уже оплачен."
    current = get_balance(user_id, order.currency)
    if current < order.amount:
        if lang == "en":
            return False, f"Not enough funds.\n\nRequired: {money(order.amount)} {order.currency}\nAvailable: {money(current)} {order.currency}\n\nBalances:\n{balance_lines(user_id)}"
        return False, f"Недостаточно средств.\n\nНужно: {money(order.amount)} {order.currency}\nДоступно: {money(current)} {order.currency}\n\nБалансы:\n{balance_lines(user_id)}"
    add_balance(user_id, -order.amount, order.currency)
    with closing(db()) as conn:
        conn.execute("UPDATE orders SET status = 'paid' WHERE tag = ?", (tag,))
        conn.commit()
    logger.info("order_paid tag=%s payer_id=%s amount=%s currency=%s", tag, user_id, order.amount, order.currency)
    return True, f"✅ Order #{tag} paid." if lang == "en" else f"✅ Ордер #{tag} оплачен."


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in WORKER_IDS


def money(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def b(text: str, *, callback_data: str | None = None, url: str | None = None, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url, style=style)


def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b("🇷🇺 Русский", callback_data="lang:ru"),
                b("🇺🇸 English", callback_data="lang:en"),
            ]
        ]
    )


def main_keyboard(lang: str) -> InlineKeyboardMarkup:
    if lang == "en":
        rows = [
            [b("📦 Create order", callback_data="order:create", style="success")],
            [b("💎 Balance", callback_data="balance:menu"), b("💳 Requisites", callback_data="requisites")],
            [b("🌐 Language", callback_data="language"), b("🚘 Profile", callback_data="profile")],
            [b("🪬 Referrals", callback_data="refs"), b("💼 My orders", callback_data="orders:mine")],
            [b("🔗 Support", url=f"https://t.me/{SUPPORT_USERNAME}", style="danger"), b("🗝 Security", callback_data="security", style="danger")],
        ]
    else:
        rows = [
            [b("📦 Создать ордер", callback_data="order:create", style="success")],
            [b("💎 Баланс", callback_data="balance:menu"), b("💳 Реквизиты", callback_data="requisites")],
            [b("🌐 Язык", callback_data="language"), b("🚘 Профиль", callback_data="profile")],
            [b("🪬 Рефералы", callback_data="refs"), b("💼 Мои ордеры", callback_data="orders:mine")],
            [b("🔗 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}", style="danger"), b("🗝 Безопасность", callback_data="security", style="danger")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard(lang: str, target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[b("↟ Menu" if lang == "en" else "↟ В меню", callback_data=target)]])


def role_keyboard(lang: str) -> InlineKeyboardMarkup:
    seller = "🎁 I am seller" if lang == "en" else "🎁 Я продавец"
    buyer = "🎁 I am buyer" if lang == "en" else "🎁 Я покупатель"
    back = "↟ Back" if lang == "en" else "↟ Назад"
    return InlineKeyboardMarkup(inline_keyboard=[[b(seller, callback_data="role:seller"), b(buyer, callback_data="role:buyer")], [b(back, callback_data="menu")]])


def item_keyboard(lang: str) -> InlineKeyboardMarkup:
    back = "↟ Back" if lang == "en" else "↟ Назад"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b("▱ NFT-Gift", callback_data="item:nft"), b("▱ NFT-username", callback_data="item:username")],
            [b("★ Stars", callback_data="item:stars"), b("🔽 TON", callback_data="item:ton")],
            [b("✈ Telegram Premium", callback_data="item:premium")],
            [b(back, callback_data="order:create")],
        ]
    )


def currency_keyboard(lang: str) -> InlineKeyboardMarkup:
    back = "↟ Back" if lang == "en" else "↟ Назад"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b("★ Звезды" if lang == "ru" else "★ Stars", callback_data="currency:Stars", style="success"), b("🔽 TON", callback_data="currency:TON")],
            [b("₽ Карта/СПБ" if lang == "ru" else "₽ Card", callback_data="currency:RUB"), b("₮ USDT (TON)", callback_data="currency:USDT", style="success")],
            [b(back, callback_data="order:item")],
        ]
    )


def requisite_keyboard(lang: str) -> InlineKeyboardMarkup:
    menu = "↟ Menu" if lang == "en" else "↟ В меню"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b("Ⓢ Bind USDT" if lang == "en" else "Ⓢ Привязать USDT", callback_data="bind:usdt", style="success")],
            [b("Ⓣ Bind TON" if lang == "en" else "Ⓣ Привязать TON", callback_data="bind:ton")],
            [b("▭ Bind Card" if lang == "en" else "▭ Привязать Карту/СПБ", callback_data="bind:card", style="success")],
            [b("★ Bind Username" if lang == "en" else "★ Привязать Username", callback_data="bind:stars_username")],
            [b(menu, callback_data="menu")],
        ]
    )


def balance_keyboard(lang: str) -> InlineKeyboardMarkup:
    if lang == "en":
        return InlineKeyboardMarkup(inline_keyboard=[[b("💎 Withdraw balance", callback_data="balance:withdraw"), b("💎 Top up balance", callback_data="balance:topup", style="success")], [b("↟ Menu", callback_data="menu")]])
    return InlineKeyboardMarkup(inline_keyboard=[[b("💎 Вывести баланс", callback_data="balance:withdraw"), b("💎 Пополнить баланс", callback_data="balance:topup", style="success")], [b("↟ В меню", callback_data="menu")]])


def balance_currency_keyboard(lang: str, action: str) -> InlineKeyboardMarkup:
    back = "↟ Back" if lang == "en" else "↟ Назад"
    rows = [
        [b("★ Звезды" if lang == "ru" else "★ Stars", callback_data=f"{action}:Stars", style="success"), b("🔽 TON", callback_data=f"{action}:TON")],
        [b("₽ RUB", callback_data=f"{action}:RUB"), b("₮ USDT (TON)", callback_data=f"{action}:USDT", style="success")],
        [b("₴ UAH", callback_data=f"{action}:UAH"), b("◼ USD", callback_data=f"{action}:USD"), b("💰 BYN", callback_data=f"{action}:BYN")],
        [b(back, callback_data="balance:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_pay_keyboard(order: Order, lang: str) -> InlineKeyboardMarkup:
    if lang == "en":
        rows = [[b("💎 Pay order", callback_data=f"pay:{order.tag}", style="success")], [b("🔗 Support", url=f"https://t.me/{SUPPORT_USERNAME}")]]
    else:
        rows = [[b("💎 Оплатить ордер", callback_data=f"pay:{order.tag}", style="success")], [b("🔗 Тех. Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_created_keyboard(order: Order, lang: str) -> InlineKeyboardMarkup:
    share_text = f"https://t.me/{BOT_USERNAME}?start=order_{order.tag}" if BOT_USERNAME else ""
    support = "🔗 Tech support" if lang == "en" else "🔗 Тех. Поддержка"
    cancel = "‼ Cancel order" if lang == "en" else "‼ Отменить ордер"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b("☚ Share order" if lang == "en" else "☚ Поделиться ордером", url=f"https://t.me/share/url?url={share_text}")],
            [b(support, url=f"https://t.me/{SUPPORT_USERNAME}")],
            [b(cancel, callback_data=f"cancel:{order.tag}", style="danger")],
        ]
    )


def welcome_caption(lang: str) -> str:
    if lang == "en":
        return (
            "👋 <b>Welcome!</b>\n\n"
            "<blockquote>🛡 Kasper is a specialized service for secure off-exchange deals.\n"
            "web3 Automated execution algorithm. Fast withdrawals.</blockquote>\n\n"
            "<blockquote>① Automatic deals with NFT and gifts.</blockquote>\n"
            "<blockquote>② Full protection for both sides.</blockquote>\n"
            "<blockquote>③ Funds are frozen until confirmation.</blockquote>\n"
            f"<blockquote>④ Transfer through manager: @{SUPPORT_USERNAME}</blockquote>\n\n"
            "<blockquote>Choose an action below ◇</blockquote>"
        )
    return (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "<blockquote>🛡 Kasper — специализированный сервис по обеспечению безопасности внебиржевых сделок.\n"
        "web3 Автоматизированный алгоритм исполнения. Удобный и быстрый вывод средств</blockquote>\n\n"
        "<blockquote>① Автоматические сделки с NFT и подарками.</blockquote>\n"
        "<blockquote>② Полная защита обеих сторон.</blockquote>\n"
        "<blockquote>③ Средства заморожены до подтверждения.</blockquote>\n"
        f"<blockquote>④ Передача через менеджера: @{SUPPORT_USERNAME}</blockquote>\n\n"
        "<blockquote>Выберите действие ниже ◇</blockquote>"
    )


async def send_language(message: Message) -> None:
    await message.answer("<b>Kasper</b>\n\nВыберите язык / Choose language", reply_markup=lang_keyboard(), parse_mode=ParseMode.HTML)


async def send_menu(message: Message, lang: str) -> None:
    image = Path("kasper.jpg")
    if image.exists():
        await message.answer_photo(FSInputFile(image), caption=welcome_caption(lang), reply_markup=main_keyboard(lang), parse_mode=ParseMode.HTML)
    else:
        await message.answer(welcome_caption(lang), reply_markup=main_keyboard(lang), parse_mode=ParseMode.HTML)


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    remember_user(message.from_user)
    args = command.args or ""
    lang = get_lang(message.from_user.id if message.from_user else None)
    if args.startswith("order_"):
        await show_order(message, args.removeprefix("order_"))
        return
    if not command.args:
        await send_language(message)
        return
    await send_menu(message, lang)


@router.callback_query(F.data.startswith("lang:"))
async def choose_language(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    lang = callback.data.split(":", 1)[1]
    set_lang(callback.from_user.id, lang)
    await callback.answer("Language saved" if lang == "en" else "Язык сохранен")
    await send_menu(callback.message, lang)


@router.callback_query(F.data == "language")
async def language(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Выберите язык / Choose language", reply_markup=lang_keyboard())


@router.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await send_menu(callback.message, get_lang(callback.from_user.id))


@router.callback_query(F.data == "order:create")
async def order_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    await state.clear()
    await state.set_state(OrderState.choosing_role)
    title = "Order creation" if lang == "en" else "Создание ордера"
    text = "Choose your role in the deal:" if lang == "en" else "Выберите, кто вы в сделке:"
    await callback.message.answer(f"<b>{title}</b>\n\n{text}", reply_markup=role_keyboard(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("role:"))
async def role_selected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    await state.update_data(role=callback.data.split(":", 1)[1])
    await state.set_state(OrderState.choosing_item)
    await callback.message.answer(
        ("<b>What is the order about?</b>\n\nChoose item or service:" if lang == "en" else "<b>В чем заключается ордер?</b>\n\nВыберите товар или услугу:"),
        reply_markup=item_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "order:item")
async def order_item_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    await state.set_state(OrderState.choosing_item)
    await callback.message.answer(
        ("<b>What is the order about?</b>\n\nChoose item or service:" if lang == "en" else "<b>В чем заключается ордер?</b>\n\nВыберите товар или услугу:"),
        reply_markup=item_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("item:"))
async def item_selected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    await state.update_data(item_type=callback.data.split(":", 1)[1])
    await state.set_state(OrderState.choosing_currency)
    await callback.message.answer(
        ("<b>Choose payment currency</b>\n\nAfter choosing currency, specify recipient and amount." if lang == "en" else "<b>Выберите валюту оплаты</b>\n\nПосле выбора валюты укажите получателя и сумму."),
        reply_markup=currency_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("currency:"))
async def currency_selected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    currency = callback.data.split(":", 1)[1]
    missing = missing_requisite(callback.from_user.id, currency)
    if missing:
        field, label = missing
        await state.update_data(pending_bind_field=field)
        if lang == "en":
            text = f"<b>You have no {label} linked</b>\n\nFor an order in {currency}, add receiving details.\n\nBind requisites and create the order again."
        else:
            text = f"<b>У вас не привязан {label}</b>\n\nДля ордера в {currency} нужно указать аккаунт получения.\n\nПривяжите реквизиты и повторите создание ордера."
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b("Реквизиты" if lang == "ru" else "Requisites", callback_data="requisites")], [b("↟ Назад" if lang == "ru" else "↟ Back", callback_data="order:create")]]), parse_mode=ParseMode.HTML)
        return
    await state.update_data(currency=currency)
    await state.set_state(OrderState.waiting_recipient)
    await callback.message.answer(
        ("<b>NFT-Gift recipient</b>\n\nEnter recipient username" if lang == "en" else "<b>Получатель NFT-Gift</b>\n\nУкажите username получателя"),
        reply_markup=back_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.message(OrderState.waiting_recipient)
async def recipient_entered(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    text = (message.text or "").strip()
    if not text.startswith("@"):
        await message.answer("Enter username like @username" if lang == "en" else "Укажите username в формате @username")
        return
    await state.update_data(recipient=text)
    data = await state.get_data()
    await state.set_state(OrderState.waiting_amount)
    min_line = "\n\n<blockquote>★ Minimum: 100 Stars</blockquote>" if data.get("currency") == "Stars" and lang == "en" else ""
    if data.get("currency") == "Stars" and lang == "ru":
        min_line = "\n\n<blockquote>★ Минимум: 100 звезд</blockquote>"
    await message.answer(
        (f"<b>Enter order price</b>{min_line}" if lang == "en" else f"<b>Укажите цену ордера</b>{min_line}"),
        reply_markup=back_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.message(OrderState.waiting_amount)
async def amount_entered(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    try:
        amount = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("Enter amount as a number." if lang == "en" else "Введите сумму числом.")
        return
    data = await state.get_data()
    if data.get("currency") == "Stars" and amount < 100:
        await message.answer("Minimum: 100 Stars" if lang == "en" else "Минимум: 100 звезд")
        return
    if amount <= 0:
        await message.answer("Amount must be greater than zero." if lang == "en" else "Сумма должна быть больше нуля.")
        return
    await state.update_data(amount=amount)
    await state.set_state(OrderState.waiting_description)
    await message.answer(
        ("<b>Enter description or link</b>\n\nExample: https://t.me/nft/example" if lang == "en" else "<b>Укажите описание или ссылку</b>\n\nПример: https://t.me/nft/example"),
        reply_markup=back_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.message(OrderState.waiting_description)
async def description_entered(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    data["description"] = (message.text or "").strip()
    order = save_order(data, message.from_user)
    await state.clear()
    link = f"https://t.me/{BOT_USERNAME}?start=order_{order.tag}" if BOT_USERNAME else f"/start order_{order.tag}"
    if lang == "en":
        text = (
            f"🔗 order created: <code>{order.tag}</code>\n\n"
            f"Username buyer: {order.recipient}\n"
            f"Amount: <b>{money(order.amount)} {order.currency}</b>\n"
            f"Description: {order.description}\n\n"
            f"🔗 Link for buyer:\n\n{link}\n\n"
            f"<blockquote>◎ Important: gift transfer is performed through manager @{SUPPORT_USERNAME}\n"
            "Always check the order tag!</blockquote>"
        )
    else:
        text = (
            f"🔗 ордер создан: <code>{order.tag}</code>\n\n"
            f"Username покупателя: {order.recipient}\n"
            f"Сумма: <b>{money(order.amount)} {order.currency}</b>\n"
            f"Описание: {order.description}\n\n"
            f"🔗 Ссылка для покупателя:\n\n{link}\n\n"
            f"<blockquote>◎ Важно: передача подарка выполняется через менеджера @{SUPPORT_USERNAME}\n"
            "Обязательно сверяйте тег ордера!</blockquote>"
        )
    await message.answer(text, reply_markup=order_created_keyboard(order, lang), parse_mode=ParseMode.HTML)


async def show_order(message: Message, tag: str) -> None:
    lang = get_lang(message.from_user.id if message.from_user else None)
    order = find_order(tag)
    if not order:
        await message.answer("Order not found." if lang == "en" else "Ордер не найден.")
        return
    if lang == "en":
        text = f"<b>Order #{order.tag}</b>\n\nItem: {order.item_type}\nAmount: <b>{money(order.amount)} {order.currency}</b>\nSeller: @{order.creator_username}\nBuyer: @{order.buyer_username}\n\nPayment is made from your Kasper balance."
    else:
        text = f"<b>Ордер #{order.tag}</b>\n\nТовар: {order.item_type}\nСумма: <b>{money(order.amount)} {order.currency}</b>\nПродавец: @{order.creator_username}\nПокупатель: @{order.buyer_username}\n\nОплата выполняется с баланса Kasper."
    await message.answer(text, reply_markup=order_pay_keyboard(order, lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("pay:"))
async def pay(callback: CallbackQuery) -> None:
    lang = get_lang(callback.from_user.id)
    tag = callback.data.split(":", 1)[1]
    ok, result = pay_order(tag, callback.from_user.id, lang)
    await callback.answer(result, show_alert=True)
    if ok:
        await callback.message.answer(result)


@router.callback_query(F.data == "requisites")
async def requisites(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    req = get_requisites(callback.from_user.id)
    if lang == "en":
        text = (
            "💳 <b>Your current requisites:</b>\n\n"
            f"₮ <b>USDT (TON):</b>\n{req['usdt'] or 'not specified'}\n\n"
            f"🔽 <b>TON:</b>\n{req['ton'] or 'not specified'}\n\n"
            f"₽ <b>Card:</b>\n{req['card'] or 'not specified'}\n\n"
            f"★ <b>Stars username:</b>\n{req['stars_username'] or 'not specified'}\n\n"
            "🔗 Send requisites:\nChoose what you want to bind."
        )
    else:
        text = (
            "💳 <b>Ваши текущие реквизиты:</b>\n\n"
            f"₮ <b>USDT (TON):</b>\n{req['usdt'] or 'не указана'}\n\n"
            f"🔽 <b>TON:</b>\n{req['ton'] or 'не указана'}\n\n"
            f"₽ <b>Карта/СПБ:</b>\n{req['card'] or 'не указана'}\n\n"
            f"★ <b>Username для звезд:</b>\n{req['stars_username'] or 'не указана'}\n\n"
            "🔗 <b>Отправьте реквизиты:</b>\n<i>Выберите, что хотите привязать.</i>"
        )
    await callback.message.answer(text, reply_markup=requisite_keyboard(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("bind:"))
async def bind_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    field = callback.data.split(":", 1)[1]
    await state.update_data(bind_field=field)
    await state.set_state(OrderState.binding_requisite)
    names = {"usdt": "USDT (TON)", "ton": "TON", "card": "Карта/СПБ", "stars_username": "Stars username"}
    await callback.message.answer(
        (f"Send {names[field]} requisites:" if lang == "en" else f"Отправьте реквизит {names[field]}:"),
        reply_markup=back_keyboard(lang),
    )


@router.message(OrderState.binding_requisite)
async def bind_save(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    data = await state.get_data()
    field = data.get("bind_field")
    set_requisite(message.from_user.id, field, (message.text or "").strip())
    await state.clear()
    await message.answer("✅ Saved." if lang == "en" else "✅ Сохранено.")
    await requisites(type("Cb", (), {"answer": lambda *a, **k: asyncio.sleep(0), "from_user": message.from_user, "message": message})())


@router.callback_query(F.data == "balance:menu")
async def balance_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    await callback.message.answer(
        "Choose balance action" if lang == "en" else "Выберите подходящий вариант использования",
        reply_markup=balance_keyboard(lang),
    )


@router.callback_query(F.data.in_({"balance:topup", "balance:withdraw"}))
async def balance_action(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    action = "topup" if callback.data.endswith("topup") else "withdraw"
    text = "Choose currency to top up balance" if lang == "en" and action == "topup" else "Choose currency to withdraw balance" if lang == "en" else "Выберите валюту для пополнения баланса" if action == "topup" else "Выберите валюту для вывода баланса"
    await callback.message.answer(text, reply_markup=balance_currency_keyboard(lang, action))


@router.callback_query(F.data.startswith(("topup:", "withdraw:")))
async def balance_currency_chosen(callback: CallbackQuery) -> None:
    await callback.answer("Soon")
    lang = get_lang(callback.from_user.id)
    await callback.message.answer("Скоро будет доступно. Обратитесь в поддержку." if lang == "ru" else "Coming soon. Contact support.", reply_markup=back_keyboard(lang))


@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    req = get_requisites(callback.from_user.id)
    filled = any(req[key] for key in ["usdt", "ton", "card", "stars_username"])
    user = callback.from_user
    if lang == "en":
        text = f"🚘 <b>Profile</b>\n\nUsername: @{user.username or 'none'}\n\nID: <code>{user.id}</code>\n\nRating: 0.0\n\nSuccessful orders: 0\n\nRequisites: {'filled' if filled else 'not filled'}"
    else:
        text = f"🚘 <b>Профиль</b>\n\nUsername: @{user.username or 'нет'}\n\nID: <code>{user.id}</code>\n\nРейтинг: 0.0\n\nУспешных ордеров: 0\n\nРеквизиты: {'заполнены' if filled else 'не заполнены'}"
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[b("☎ Check balance" if lang == "en" else "☎ Проверить баланс", callback_data="balance:menu", style="success")], [b("↟ Menu" if lang == "en" else "↟ В меню", callback_data="menu")]]), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "orders:mine")
async def my_orders(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    rows = recent_orders(callback.from_user.id)
    if not rows:
        text = "No orders yet." if lang == "en" else "Ордеров пока нет."
    else:
        head = "Last 5 orders:" if lang == "en" else "Последние 5 ордеров:"
        lines = [head, ""]
        for idx, row in enumerate(rows, 1):
            lines.append(f"{idx}  #{row['tag']} — {money(float(row['amount']))} {row['currency']}")
        text = "\n".join(lines)
    await callback.message.answer(text, reply_markup=back_keyboard(lang))


@router.callback_query(F.data == "security")
async def security(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    if lang == "en":
        text = f"🗝 <b>Security</b>\n\n① Transfer goods only to manager:\n@{SUPPORT_USERNAME}\n\n② Do not send goods directly to buyer.\nTransfer goes through the service.\n\n③ Check amount and order tag in payment comment.\n\n④ After verification, buyer confirms receipt and order closes."
    else:
        text = f"🗝 <b>Безопасность</b>\n\n① Передавайте товар только менеджеру:\n@{SUPPORT_USERNAME}\n\n② Не отправляйте товар напрямую покупателю.\nПередача идет через сервис.\n\n③ Сверяйте сумму и тег ордера в комментарии к платежу.\n\n④ После проверки покупатель подтверждает получение и ордер закрывается."
    await callback.message.answer(text, reply_markup=back_keyboard(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "refs")
async def refs(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_lang(callback.from_user.id)
    await callback.message.answer("Реферальная система скоро будет доступна." if lang == "ru" else "Referral system will be available soon.", reply_markup=back_keyboard(lang))


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_order(callback: CallbackQuery) -> None:
    await callback.answer("Canceled")
    lang = get_lang(callback.from_user.id)
    await callback.message.answer("Ордер отменен." if lang == "ru" else "Order canceled.", reply_markup=back_keyboard(lang))


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("/addbalance <telegram_id> <amount> [Stars|TON|USDT|RUB]\n/balance <telegram_id>")


@router.message(Command("addbalance"))
async def admin_add_balance(message: Message, command: CommandObject) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /addbalance <telegram_id> <amount> [Stars|TON|USDT|RUB]")
        return
    user_id = int(parts[0])
    amount = float(parts[1].replace(",", "."))
    currency = parts[2] if len(parts) > 2 else "Stars"
    add_balance(user_id, amount, currency)
    logger.info("balance_added admin_id=%s target=%s amount=%s currency=%s", message.from_user.id, user_id, amount, currency)
    await message.answer(f"✅ Баланс <code>{user_id}</code> пополнен на {money(amount)} {currency}.", parse_mode=ParseMode.HTML)


@router.message(Command("balance"))
async def admin_balance(message: Message, command: CommandObject) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    try:
        user_id = int(command.args or "")
    except ValueError:
        await message.answer("Формат: /balance <telegram_id>")
        return
    await message.answer(f"Балансы <code>{user_id}</code>:\n{balance_lines(user_id)}", parse_mode=ParseMode.HTML)


async def main() -> None:
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Add it to .env or Railway Variables.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
