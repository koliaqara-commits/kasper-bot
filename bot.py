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
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "KasperskyGiftSupport").lstrip("@")
PAYMENT_URL = os.getenv("PAYMENT_URL", f"https://t.me/{SUPPORT_USERNAME}")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "kasper.sqlite3"))
ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}
WORKER_IDS = {
    int(item.strip())
    for item in os.getenv("WORKER_IDS", "").split(",")
    if item.strip().isdigit()
}

DEAL_TYPES = {
    "nft": "🖼 NFT-подарок",
    "stars": "⭐ Звёзды",
    "username": "👤 NFT-username",
    "crypto": "฿ Крипта",
    "premium": "👑 Telegram Premium",
}

CURRENCIES = ["Stars", "TON", "USDT", "RUB"]

router = Router()

logging.basicConfig(
    filename="kasper.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger("kasper")


class DealState(StatesGroup):
    choosing_type = State()
    waiting_link = State()
    waiting_stars_count = State()
    choosing_currency = State()
    waiting_amount = State()
    waiting_buyer = State()


@dataclass
class Deal:
    tag: str
    seller_id: int
    seller_username: str
    buyer_username: str
    item_type: str
    item_link: str
    amount: float
    currency: str
    stars_count: int | None
    status: str


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(db()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                lang TEXT NOT NULL DEFAULT 'ru',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, currency)
            );

            CREATE TABLE IF NOT EXISTS deals (
                tag TEXT PRIMARY KEY,
                seller_id INTEGER NOT NULL,
                seller_username TEXT NOT NULL,
                buyer_username TEXT NOT NULL,
                item_type TEXT NOT NULL,
                item_link TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                stars_count INTEGER,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('payment_url', ?)",
            (PAYMENT_URL,),
        )
        user_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "lang" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru'")
        connection.commit()


def remember_user(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    with closing(db()) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, first_name)
            VALUES(?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user.id, user.username or "", user.first_name or ""),
        )
        connection.commit()


def get_user_lang(user_id: int | None) -> str:
    if not user_id:
        return "ru"
    with closing(db()) as connection:
        row = connection.execute("SELECT lang FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["lang"] if row and row["lang"] in {"ru", "en"} else "ru"


def set_user_lang(user_id: int, lang: str) -> None:
    if lang not in {"ru", "en"}:
        lang = "ru"
    with closing(db()) as connection:
        connection.execute(
            """
            INSERT INTO users(id, lang)
            VALUES(?, ?)
            ON CONFLICT(id) DO UPDATE SET lang = excluded.lang
            """,
            (user_id, lang),
        )
        connection.commit()


def get_setting(key: str, default: str = "") -> str:
    with closing(db()) as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(db()) as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        connection.commit()


def add_balance(user_id: int, amount: float, currency: str) -> None:
    with closing(db()) as connection:
        connection.execute(
            """
            INSERT INTO balances(user_id, currency, amount)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, currency) DO UPDATE SET amount = amount + excluded.amount
            """,
            (user_id, currency, amount),
        )
        connection.commit()


def get_balance(user_id: int, currency: str) -> float:
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT amount FROM balances WHERE user_id = ? AND currency = ?",
            (user_id, currency),
        ).fetchone()
    return float(row["amount"]) if row else 0.0


def pay_deal_from_balance(tag: str, user_id: int, lang: str = "ru") -> tuple[bool, str]:
    with closing(db()) as connection:
        row = connection.execute("SELECT * FROM deals WHERE tag = ?", (tag,)).fetchone()
        if not row:
            return False, "Deal not found." if lang == "en" else "Сделка не найдена."
        if row["status"] == "paid":
            return False, "This deal is already paid." if lang == "en" else "Эта сделка уже оплачена."

        balance = connection.execute(
            "SELECT amount FROM balances WHERE user_id = ? AND currency = ?",
            (user_id, row["currency"]),
        ).fetchone()
        current_balance = float(balance["amount"]) if balance else 0.0
        amount = float(row["amount"])
        if current_balance < amount:
            if lang == "en":
                return False, (
                    f"Not enough funds. Required: {money(amount)} {row['currency']}, "
                    f"available: {money(current_balance)} {row['currency']}.\n\n"
                    f"Your balances:\n{balance_lines(user_id)}"
                )
            return False, (
                f"Недостаточно средств. Нужно {money(amount)} {row['currency']}, "
                f"на балансе {money(current_balance)} {row['currency']}.\n\n"
                f"Ваши балансы:\n{balance_lines(user_id)}"
            )

        connection.execute(
            "UPDATE balances SET amount = amount - ? WHERE user_id = ? AND currency = ?",
            (amount, user_id, row["currency"]),
        )
        connection.execute(
            "UPDATE deals SET status = 'paid' WHERE tag = ?",
            (tag,),
        )
        connection.commit()

    return True, f"✅ Deal #{tag} paid from balance." if lang == "en" else f"✅ Сделка #{tag} оплачена с баланса."


def get_balances(user_id: int) -> list[sqlite3.Row]:
    with closing(db()) as connection:
        return connection.execute(
            "SELECT currency, amount FROM balances WHERE user_id = ? ORDER BY currency",
            (user_id,),
        ).fetchall()


def balance_lines(user_id: int) -> str:
    rows = {row["currency"]: float(row["amount"]) for row in get_balances(user_id)}
    return "\n".join(f"• {currency}: {money(rows.get(currency, 0.0))}" for currency in CURRENCIES)


def create_tag() -> str:
    return "MUM" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def save_deal(data: dict, seller_id: int, seller_username: str) -> Deal:
    tag = create_tag()
    deal = Deal(
        tag=tag,
        seller_id=seller_id,
        seller_username=seller_username,
        buyer_username=data["buyer_username"].lstrip("@"),
        item_type=data["item_type"],
        item_link=data["item_link"],
        amount=float(data["amount"]),
        currency=data["currency"],
        stars_count=data.get("stars_count"),
        status="created",
    )
    with closing(db()) as connection:
        connection.execute(
            """
            INSERT INTO deals(
                tag, seller_id, seller_username, buyer_username, item_type,
                item_link, amount, currency, stars_count, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal.tag,
                deal.seller_id,
                deal.seller_username,
                deal.buyer_username,
                deal.item_type,
                deal.item_link,
                deal.amount,
                deal.currency,
                deal.stars_count,
                deal.status,
            ),
        )
        connection.commit()
    logger.info(
        "deal_created tag=%s seller_id=%s seller_username=%s buyer_username=%s item_type=%s item_link=%s amount=%s currency=%s",
        deal.tag,
        deal.seller_id,
        deal.seller_username,
        deal.buyer_username,
        deal.item_type,
        deal.item_link,
        deal.amount,
        deal.currency,
    )
    return deal


def find_deal(tag: str) -> Deal | None:
    with closing(db()) as connection:
        row = connection.execute("SELECT * FROM deals WHERE tag = ?", (tag,)).fetchone()
    if not row:
        return None
    return Deal(
        tag=row["tag"],
        seller_id=row["seller_id"],
        seller_username=row["seller_username"],
        buyer_username=row["buyer_username"],
        item_type=row["item_type"],
        item_link=row["item_link"],
        amount=row["amount"],
        currency=row["currency"],
        stars_count=row["stars_count"],
        status=row["status"],
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_staff(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in WORKER_IDS


def money(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.2f}"


def button(text: str, *, style: str = "primary", **kwargs) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, style=style, **kwargs)


def main_menu(lang: str = "ru") -> InlineKeyboardMarkup:
    if lang == "en":
        rows = [
            [button("🆕 Create order", callback_data="deal:create", style="success")],
            [
                button("💳 Balance", callback_data="profile"),
                button("🔔 Security", callback_data="requisites"),
            ],
            [
                button("💙 Referrals", callback_data="refs"),
                button("📦 My deals", callback_data="deals:mine"),
            ],
            [
                button("💬 Support", url=f"https://t.me/{SUPPORT_USERNAME}"),
                button("🔵 Language", callback_data="language"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    rows = [
        [button("🆕 Создать ордер", callback_data="deal:create", style="success")],
        [
            button("💳 Баланс", callback_data="profile"),
            button("🔔 Безопасность", callback_data="requisites"),
        ],
        [
            button("💙 Рефералы", callback_data="refs"),
            button("📦 Мои сделки", callback_data="deals:mine"),
        ],
        [
            button("💬 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}"),
            button("🔵 Язык", callback_data="language"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deal_type_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("🖼 NFT Gift", callback_data="dealtype:nft"),
                button("⭐ Telegram Stars", callback_data="dealtype:stars"),
            ],
            [
                button("👤 Username", callback_data="dealtype:username"),
                button("💠 Crypto", callback_data="dealtype:crypto"),
            ],
            [button("👑 Telegram Premium", callback_data="dealtype:premium")],
            [button("⬅️ Back" if lang == "en" else "⬅️ Назад", callback_data="menu", style="danger")],
        ]
    )


def currency_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("⭐ Stars", callback_data="currency:Stars"),
                button("💎 TON", callback_data="currency:TON"),
                button("💵 USDT", callback_data="currency:USDT"),
                button("₽ RUB", callback_data="currency:RUB"),
            ],
            [button("⬅️ Back" if lang == "en" else "⬅️ Назад", callback_data="deal:create", style="danger")],
        ]
    )


def back_menu_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("◀️ Menu" if lang == "en" else "◀️ В меню", callback_data="menu")]]
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("🇷🇺 Русский", callback_data="lang:ru"),
                button("🇬🇧 English", callback_data="lang:en"),
            ],
            [button("◀️ В меню", callback_data="menu")],
        ]
    )


def buyer_keyboard(deal: Deal, user_id: int, username: str | None, lang: str = "ru") -> InlineKeyboardMarkup | None:
    allowed_buyer = bool(username and username.lower() == deal.buyer_username.lower())
    if not allowed_buyer and not is_staff(user_id):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("💎 Pay from balance" if lang == "en" else "💎 Оплатить с баланса", callback_data=f"deal:pay:{deal.tag}", style="success")],
            [button("💬 Support" if lang == "en" else "💬 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
        ]
    )


def welcome_text(lang: str = "ru") -> str:
    if lang == "en":
        return (
            "Welcome to <b>Kasper</b> 👋\n\n"
            "💜 <b>Kasper</b> is a neon-style service for secure deals with NFT gifts, Stars and Telegram assets.\n\n"
            "<pre>"
            "🟣 Automated order execution.\n"
            "🔵 Fast deal creation and verification.\n"
            "💎 Payment from Kasper internal balance."
            "</pre>\n\n"
            "<pre>"
            "• Service fee: 1%\n"
            "• Working mode: 24/7\n"
            f"• Support: @{SUPPORT_USERNAME}"
            "</pre>\n\n"
            "🛡 <b>Choose a section below:</b>"
        )

    return (
        "Добро пожаловать в <b>Kasper</b> 👋\n\n"
        "💜 <b>Kasper</b> — неоновый сервис безопасных сделок с NFT-подарками, Stars и Telegram-активами.\n\n"
        "<pre>"
        "🟣 Автоматизированное исполнение ордеров.\n"
        "🔵 Быстрое создание и проверка сделки.\n"
        "💎 Оплата с внутреннего баланса Kasper."
        "</pre>\n\n"
        "<pre>"
        "• Комиссия сервиса: 1%\n"
        "• Режим работы: 24/7\n"
        f"• Поддержка: @{SUPPORT_USERNAME}"
        "</pre>\n\n"
        "🛡 <b>Выберите нужный раздел ниже:</b>"
    )


async def send_menu(message: Message) -> None:
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    image_path = Path("kasper.jpg")
    if image_path.exists():
        await message.answer_photo(
            photo=FSInputFile(image_path),
            caption=welcome_text(lang),
            reply_markup=main_menu(lang),
            parse_mode=ParseMode.HTML,
        )
        return
    await message.answer(welcome_text(lang), reply_markup=main_menu(lang), parse_mode=ParseMode.HTML)


async def handle_admin_command_in_state(message: Message, state: FSMContext) -> bool:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return False

    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()

    if command == "/start":
        await state.clear()
        await send_menu(message)
        return True

    user = message.from_user
    if not user or not is_admin(user.id):
        await state.clear()
        await message.answer("Команда сброшена. Нажмите /start.")
        return True

    await state.clear()

    if command == "/admin":
        await send_admin_help(message)
        return True

    if command == "/addbalance":
        await add_balance_from_parts(message, parts[1:])
        return True

    if command == "/balance":
        await show_balance_from_parts(message, parts[1:])
        return True

    if command == "/setpaylink":
        await set_paylink_from_parts(message, parts[1:])
        return True

    await message.answer("Неизвестная команда. Нажмите /admin.")
    return True


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    remember_user(message)
    args = command.args or ""
    if args.startswith("deal_"):
        await show_deal(message, args.removeprefix("deal_"))
        return
    await send_menu(message)


@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    lang = get_user_lang(callback.from_user.id)
    await callback.message.answer(welcome_text(lang), reply_markup=main_menu(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "deal:create")
async def create_deal(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_user_lang(callback.from_user.id)
    await state.set_state(DealState.choosing_type)
    text = "💠 <b>Choose deal format</b>" if lang == "en" else "💠 <b>Выберите формат сделки</b>"
    await callback.message.answer(text, reply_markup=deal_type_keyboard(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("dealtype:"))
async def choose_type(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_user_lang(callback.from_user.id)
    item_type = callback.data.split(":", 1)[1]
    await state.clear()
    await state.update_data(item_type=item_type)
    if item_type == "stars":
        await state.set_state(DealState.waiting_stars_count)
        text = "⭐ <b>Enter Stars amount</b>\n\nExample: <code>500</code>" if lang == "en" else "⭐ <b>Введите количество Stars</b>\n\nПример: <code>500</code>"
        await callback.message.answer(text, parse_mode=ParseMode.HTML)
    else:
        await state.set_state(DealState.waiting_link)
        text = "🔗 <b>Send item link</b>\n\nExample: <code>https://t.me/nft/example</code>" if lang == "en" else "🔗 <b>Вставьте ссылку на товар</b>\n\nПример: <code>https://t.me/nft/example</code>"
        await callback.message.answer(text, parse_mode=ParseMode.HTML)


@router.message(DealState.waiting_stars_count)
async def stars_count(message: Message, state: FSMContext) -> None:
    if await handle_admin_command_in_state(message, state):
        return
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    if not message.text or not message.text.isdigit():
        text = "Enter a number, for example: <code>500</code>" if lang == "en" else "Введите число, например: <code>500</code>"
        await message.answer(text, parse_mode=ParseMode.HTML)
        return
    await state.update_data(stars_count=int(message.text), item_link="Telegram Stars")
    await state.set_state(DealState.waiting_link)
    text = "🔗 <b>Send item link</b>\n\nExample: <code>https://t.me/nft/example</code>" if lang == "en" else "🔗 <b>Вставьте ссылку на товар</b>\n\nПример: <code>https://t.me/nft/example</code>"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(DealState.waiting_link)
async def item_link(message: Message, state: FSMContext) -> None:
    if await handle_admin_command_in_state(message, state):
        return
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    text = (message.text or "").strip()
    data = await state.get_data()
    if text.startswith("@") and {"item_link", "currency", "amount"}.issubset(data):
        await finish_deal(message, state, text)
        return
    if not message.text or not message.text.startswith(("https://t.me/", "http://t.me/")):
        text = "Send a Telegram link, for example: <code>https://t.me/nft/example</code>" if lang == "en" else "Нужна ссылка Telegram, например: <code>https://t.me/nft/example</code>"
        await message.answer(text, parse_mode=ParseMode.HTML)
        return
    await state.update_data(item_link=text)
    await state.set_state(DealState.choosing_currency)
    prompt = "💎 <b>Choose payment currency</b>" if lang == "en" else "💎 <b>Выберите валюту оплаты</b>"
    await message.answer(prompt, reply_markup=currency_keyboard(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("currency:"))
async def choose_currency(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = get_user_lang(callback.from_user.id)
    data = await state.get_data()
    if "item_link" not in data:
        await state.set_state(DealState.choosing_type)
        text = "This old button has no deal data anymore. Choose the deal type again." if lang == "en" else "Эта старая кнопка уже без данных сделки. Выберите тип сделки заново."
        await callback.message.answer(text, reply_markup=deal_type_keyboard(lang))
        return
    currency = callback.data.split(":", 1)[1]
    await state.update_data(currency=currency)
    await state.set_state(DealState.waiting_amount)
    text = f"💰 <b>Enter amount in {currency}</b>\n\nExample: <code>100</code>" if lang == "en" else f"💰 <b>Введите сумму в {currency}</b>\n\nПример: <code>100</code>"
    await callback.message.answer(text, parse_mode=ParseMode.HTML)


@router.message(DealState.waiting_amount)
async def amount(message: Message, state: FSMContext) -> None:
    if await handle_admin_command_in_state(message, state):
        return
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    try:
        value = float((message.text or "").replace(",", "."))
    except ValueError:
        text = "Enter a numeric amount, for example: <code>1000</code>" if lang == "en" else "Введите сумму числом, например: <code>1000</code>"
        await message.answer(text, parse_mode=ParseMode.HTML)
        return
    if value <= 0:
        await message.answer("Amount must be greater than zero." if lang == "en" else "Сумма должна быть больше нуля.")
        return
    await state.update_data(amount=value)
    await state.set_state(DealState.waiting_buyer)
    text = "👤 <b>Enter buyer username</b>\n\nExample: <code>@username</code>" if lang == "en" else "👤 <b>Укажите username покупателя</b>\n\nПример: <code>@username</code>"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(DealState.waiting_buyer)
async def buyer(message: Message, state: FSMContext) -> None:
    if await handle_admin_command_in_state(message, state):
        return
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    text = (message.text or "").strip()
    if not text.startswith("@") or len(text) < 3:
        prompt = "Enter username in <code>@username</code> format." if lang == "en" else "Укажите username в формате <code>@username</code>."
        await message.answer(prompt, parse_mode=ParseMode.HTML)
        return
    await finish_deal(message, state, text)


async def finish_deal(message: Message, state: FSMContext, buyer_username: str) -> None:
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    data = await state.get_data()
    data["buyer_username"] = buyer_username
    seller = message.from_user
    deal = save_deal(data, seller.id, seller.username or str(seller.id))
    await state.clear()
    link = f"https://t.me/{BOT_USERNAME}?start=deal_{deal.tag}" if BOT_USERNAME else f"/start deal_{deal.tag}"
    if lang == "en":
        text = (
            "✅ <b>Deal #{} created</b>\n\n"
            "💎 Amount: <b>{} {}</b>\n"
            "📦 Item: {}\n"
            "👤 Buyer: @{}\n\n"
            "🔗 <b>Payment link:</b>\n"
            "<code>{}</code>\n\n"
            "🧾 Deal tag: <b>#{}</b>"
        )
    else:
        text = (
            "✅ <b>Сделка #{} создана</b>\n\n"
            "💎 Сумма: <b>{} {}</b>\n"
            "📦 Товар: {}\n"
            "👤 Покупатель: @{}\n\n"
            "🔗 <b>Ссылка для оплаты:</b>\n"
            "<code>{}</code>\n\n"
            "🧾 Тег сделки: <b>#{}</b>"
        )
    await message.answer(
        text.format(deal.tag, money(deal.amount), deal.currency, deal.item_link, deal.buyer_username, link, deal.tag),
        parse_mode=ParseMode.HTML,
    )


async def show_deal(message: Message, tag: str) -> None:
    lang = get_user_lang(message.from_user.id if message.from_user else None)
    deal = find_deal(tag)
    if not deal:
        await message.answer("Deal not found." if lang == "en" else "Сделка не найдена.")
        return
    user_id = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    keyboard = buyer_keyboard(deal, user_id, username, lang)
    if lang == "en":
        text = (
            f"🛡 <b>Kasper Deal #{deal.tag}</b>\n\n"
            f"📦 Item: {DEAL_TYPES.get(deal.item_type, deal.item_type)}\n"
            f"🔗 Link: {deal.item_link}\n"
            f"💎 Amount: <b>{money(deal.amount)} {deal.currency}</b>\n"
            f"👤 Seller: @{deal.seller_username}\n"
            f"👤 Buyer: @{deal.buyer_username}\n\n"
        )
    else:
        text = (
            f"🛡 <b>Kasper Deal #{deal.tag}</b>\n\n"
            f"📦 Товар: {DEAL_TYPES.get(deal.item_type, deal.item_type)}\n"
            f"🔗 Ссылка: {deal.item_link}\n"
            f"💎 Сумма: <b>{money(deal.amount)} {deal.currency}</b>\n"
            f"👤 Продавец: @{deal.seller_username}\n"
            f"👤 Покупатель: @{deal.buyer_username}\n\n"
        )
    if keyboard:
        text += "Payment is made from your internal Kasper balance." if lang == "en" else "Оплата выполняется с внутреннего баланса Kasper."
    else:
        text += "This deal is assigned to another buyer. Payment button is hidden." if lang == "en" else "Эта сделка предназначена другому покупателю. Кнопка оплаты скрыта."
    await message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("deal:pay:"))
async def pay_deal(callback: CallbackQuery) -> None:
    tag = callback.data.rsplit(":", 1)[1]
    lang = get_user_lang(callback.from_user.id)
    deal = find_deal(tag)
    if not deal:
        await callback.answer("Deal not found." if lang == "en" else "Сделка не найдена.", show_alert=True)
        return

    user = callback.from_user
    username = user.username or ""
    allowed_buyer = username.lower() == deal.buyer_username.lower()
    if not allowed_buyer and not is_staff(user.id):
        logger.warning(
            "deal_pay_denied tag=%s user_id=%s username=%s expected_buyer=%s",
            tag,
            user.id,
            username,
            deal.buyer_username,
        )
        await callback.answer("This deal is assigned to another buyer." if lang == "en" else "Эта сделка предназначена другому покупателю.", show_alert=True)
        return

    ok, result = pay_deal_from_balance(tag, user.id, lang)
    logger.info(
        "deal_pay_attempt tag=%s ok=%s payer_id=%s payer_username=%s seller_id=%s buyer_username=%s amount=%s currency=%s result=%s",
        tag,
        ok,
        user.id,
        username,
        deal.seller_id,
        deal.buyer_username,
        deal.amount,
        deal.currency,
        result.replace("\n", " "),
    )
    await callback.answer(result, show_alert=True)
    if ok:
        if lang == "en":
            buyer_text = (
                f"{result}\n\n"
                f"💰 Debited: <b>{money(deal.amount)} {deal.currency}</b>\n"
                f"📦 Deal: <b>#{deal.tag}</b>"
            )
        else:
            buyer_text = (
                f"{result}\n\n"
                f"💰 Списано: <b>{money(deal.amount)} {deal.currency}</b>\n"
                f"📦 Сделка: <b>#{deal.tag}</b>"
            )
        await callback.message.answer(buyer_text, parse_mode=ParseMode.HTML)
        seller_lang = get_user_lang(deal.seller_id)
        if seller_lang == "en":
            seller_text = (
                f"✅ <b>Deal #{deal.tag} paid!</b>\n"
                f"Transfer the item to manager @{SUPPORT_USERNAME}.\n"
                "After transfer, wait 3-7 minutes for withdrawal.\n"
                "<b>Always verify the manager username!</b>"
            )
        else:
            seller_text = (
                f"✅ <b>Сделка #{deal.tag} оплачена!</b>\n"
                f"Передайте товар менеджеру @{SUPPORT_USERNAME}.\n"
                "После передачи ожидайте вывод средств в течение 3-7 минут.\n"
                "<b>Сверяйте username менеджера!</b>"
            )
        await callback.bot.send_message(
            deal.seller_id,
            seller_text,
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery) -> None:
    await callback.answer()
    user = callback.from_user
    lang = get_user_lang(user.id)
    if lang == "en":
        text = f"💎 <b>Kasper Balance</b>\n\nID: <code>{user.id}</code>\nUsername: @{user.username or 'none'}\n\n<b>Balances:</b>\n{balance_lines(user.id)}"
    else:
        text = f"💎 <b>Баланс Kasper</b>\n\nID: <code>{user.id}</code>\nUsername: @{user.username or 'нет'}\n\n<b>Балансы:</b>\n{balance_lines(user.id)}"
    await callback.message.answer(
        text,
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "language")
async def language(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_user_lang(callback.from_user.id)
    title = "🌐 <b>Choose interface language</b>" if lang == "en" else "🌐 <b>Выберите язык интерфейса</b>"
    await callback.message.answer(
        title + "\n\n"
        "<blockquote>"
        "🇷🇺 Русский\n"
        "🇬🇧 English"
        "</blockquote>",
        reply_markup=language_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery) -> None:
    lang = "ru" if callback.data.endswith(":ru") else "en"
    set_user_lang(callback.from_user.id, lang)
    await callback.answer("Language saved" if lang == "en" else "Язык сохранён")
    language_name = "Русский" if lang == "ru" else "English"
    text = f"✅ Selected language: <b>{language_name}</b>" if lang == "en" else f"✅ Выбран язык: <b>{language_name}</b>"
    await callback.message.answer(
        text,
        reply_markup=back_menu_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )
    await callback.message.answer(welcome_text(lang), reply_markup=main_menu(lang), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "requisites")
async def security(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_user_lang(callback.from_user.id)
    if lang == "en":
        text = (
            "🛡 <b>Security rules</b>\n\n"
            f"<blockquote>• Transfer the gift only to manager @{SUPPORT_USERNAME}</blockquote>\n\n"
            "<blockquote>• Do not send directly to the buyer — transfer goes through the service</blockquote>\n\n"
            "<blockquote>• Check the amount and order tag in the payment comment</blockquote>\n\n"
            "<blockquote>• After verification, the buyer confirms receipt and the order is closed</blockquote>"
        )
    else:
        text = (
            "🛡 <b>Правила безопасности</b>\n\n"
            f"<blockquote>• Передавайте подарок только менеджеру @{SUPPORT_USERNAME}</blockquote>\n\n"
            "<blockquote>• Не отправляйте напрямую покупателю — передача идёт через сервис</blockquote>\n\n"
            "<blockquote>• Сверяйте сумму и тег ордера в комментарии к платежу</blockquote>\n\n"
            "<blockquote>• После проверки покупатель подтверждает получение и ордер закрывается</blockquote>"
        )
    await callback.message.answer(
        text,
        reply_markup=back_menu_keyboard(lang),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.in_({"topup", "deals:mine", "top:sellers", "refs"}))
async def placeholders(callback: CallbackQuery) -> None:
    await callback.answer()
    lang = get_user_lang(callback.from_user.id)
    if lang == "en":
        messages = {
            "topup": f"💳 <b>Your balances:</b>\n{balance_lines(callback.from_user.id)}\n\nTop-ups are made by a manager or administrator.",
            "deals:mine": "📦 Deal history will appear here soon.",
            "top:sellers": "🏆 Trader rating will open soon.",
            "refs": "🔗 Referral system will be available soon.",
        }
    else:
        messages = {
            "topup": f"💳 <b>Ваши балансы:</b>\n{balance_lines(callback.from_user.id)}\n\nПополнение выполняет менеджер или администратор.",
            "deals:mine": "📦 История сделок скоро появится в этом разделе.",
            "top:sellers": "🏆 Рейтинг трейдеров скоро будет открыт.",
            "refs": "🔗 Реферальная система скоро будет доступна.",
        }
    await callback.message.answer(messages[callback.data], parse_mode=ParseMode.HTML)


@router.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка уже неактуальна. Нажмите /start.", show_alert=False)


@router.message(F.text.startswith(("https://t.me/", "http://t.me/", "@")))
async def stale_deal_step(message: Message) -> None:
    await message.answer(
        "Эта форма сделки уже неактуальна после перезапуска бота.\n\n"
        "Нажмите /start и создайте сделку заново."
    )


async def send_admin_help(message: Message) -> None:
    await message.answer(
        "<b>Админ-панель</b>\n\n"
        "/addbalance &lt;telegram_id&gt; &lt;amount&gt; [Stars|TON|USDT|RUB]\n"
        "/balance &lt;telegram_id&gt;\n"
        "/setpaylink &lt;url&gt;",
        parse_mode=ParseMode.HTML,
    )


async def add_balance_from_parts(message: Message, parts: list[str]) -> None:
    if len(parts) < 2:
        await message.answer("Формат: /addbalance <telegram_id> <amount> [Stars|TON|USDT|RUB]")
        return
    try:
        user_id = int(parts[0])
        value = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("ID и сумма должны быть числами.")
        return
    currency = parts[2] if len(parts) > 2 else "Stars"
    if currency not in CURRENCIES:
        await message.answer("Валюта должна быть одной из: Stars, TON, USDT, RUB")
        return
    add_balance(user_id, value, currency)
    admin = message.from_user
    logger.info(
        "balance_added admin_id=%s admin_username=%s target_user_id=%s amount=%s currency=%s",
        admin.id if admin else None,
        admin.username if admin else None,
        user_id,
        value,
        currency,
    )
    await message.answer(
        f"✅ Баланс пользователя <code>{user_id}</code> пополнен на {money(value)} {currency}.",
        parse_mode=ParseMode.HTML,
    )


async def show_balance_from_parts(message: Message, parts: list[str]) -> None:
    if len(parts) != 1:
        await message.answer("Формат: /balance <telegram_id>")
        return
    try:
        user_id = int(parts[0])
    except ValueError:
        await message.answer("Формат: /balance <telegram_id>")
        return
    await message.answer(f"Балансы <code>{user_id}</code>:\n{balance_lines(user_id)}", parse_mode=ParseMode.HTML)


async def set_paylink_from_parts(message: Message, parts: list[str]) -> None:
    url = " ".join(parts).strip()
    if not url.startswith(("https://", "http://")):
        await message.answer("Формат: /setpaylink https://...")
        return
    set_setting("payment_url", url)
    await message.answer(f"✅ Ссылка оплаты обновлена:\n<code>{url}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await send_admin_help(message)


@router.message(Command("addbalance"))
async def admin_add_balance(message: Message, command: CommandObject) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await add_balance_from_parts(message, (command.args or "").split())


@router.message(Command("balance"))
async def admin_balance(message: Message, command: CommandObject) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await show_balance_from_parts(message, (command.args or "").split())


@router.message(Command("setpaylink"))
async def admin_set_paylink(message: Message, command: CommandObject) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await set_paylink_from_parts(message, (command.args or "").split())


async def main() -> None:
    global BOT_USERNAME

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Create .env from .env.example.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
