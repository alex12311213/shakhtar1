"""
Telegram-бот для отслеживания билетов на матчи ФК Шахтар (tickets.shakhtar.com)

Что умеет:
  /start  - главное меню с кнопками
  /check  - все события и статус продажи билетов
  /days   - сколько дней осталось до каждого матча
  /link   - ссылки на продажу билетов
  Авторассылка 2 раза в день (время меняется в SEND_TIMES)
  Мониторинг раз в минуту: сообщает, когда появились билеты / новое событие / билеты закончились
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ----------------------------- НАСТРОЙКИ -----------------------------
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))  # Europe/Kyiv, если нужен киевский
SITE_URL = "https://tickets.shakhtar.com"

SEND_TIMES = [(12, 0), (0, 0)]  # время автоотчётов (часы, минуты): 12:00 и 00:00
WATCH_INTERVAL = 60             # как часто проверять сайт, секунд

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shakhtar-bot")

dp = Dispatcher()
bot: Bot | None = None
last_state: dict[str, dict] = {}


# ----------------------------- ПАРСЕР САЙТА -----------------------------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def parse_matches(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Реальные статусы продажи берём из JSON-LD (schema.org) в начале страницы
    status: dict[str, bool] = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.get_text())
        except ValueError:
            continue
        items = data.get("mainEntity", {}).get("itemListElement", [])
        for el in items:
            ev = el.get("item", {})
            name = ev.get("name")
            if not name:
                continue
            sold_out = "SoldOut" in ev.get("eventStatus", "")
            in_stock = ev.get("offers", {}).get("availability", "").endswith("InStock")
            status[norm(name)] = in_stock and not sold_out

    # 2. Карточки событий из HTML
    result = []
    for item in soup.select("#tickets .match-item"):
        names = [n.get_text(strip=True) for n in item.select(".match-command-name")]
        if not names:
            continue
        title = " vs ".join(names)

        date = None
        d = item.select_one(".match-date")
        if d and d.get("data-utc-datetime"):
            raw = d["data-utc-datetime"][:19]
            date = (
                datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=timezone.utc)
                .astimezone(TZ)
            )

        link = item.select_one(".match-action a")
        url = urljoin(SITE_URL, link["href"]) if link and link.get("href") else SITE_URL
        stadium = item.select_one(".match-stadium-title")
        comp = item.select_one(".match-type-name")
        available = status.get(norm(title), False)

        result.append(
            {
                "title": title,
                "date": date,
                "competition": comp.get_text(strip=True) if comp else "",
                "stadium": stadium.get_text(strip=True) if stadium else "",
                "available": available,
                "url": url,
            }
        )
    return result


async def fetch_matches() -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "uk,ru;q=0.8,en;q=0.5",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        r = await client.get(SITE_URL)
        r.raise_for_status()
    return parse_matches(r.text)


# ----------------------------- ФОРМАТИРОВАНИЕ -----------------------------
def days_left(date: datetime) -> str:
    d = (date.date() - datetime.now(TZ).date()).days
    if d < 0:
        return "матч уже прошёл"
    if d == 0:
        return "матч сегодня!"
    return f"осталось дней: {d}"


def format_match(m: dict) -> str:
    lines = [f"⚽ {m['title']}"]
    if m["competition"]:
        lines.append(f"🏆 {m['competition']}")
    if m["date"]:
        lines.append(f"📅 {m['date']:%d.%m.%Y %H:%M} ({days_left(m['date'])})")
    if m["stadium"]:
        lines.append(f"🏟 {m['stadium']}")
    lines.append("🎟 🟢 Билеты в продаже" if m["available"] else "🎟 🔴 Нет в продаже / распродано")
    lines.append(f"🔗 {m['url']}")
    return "\n".join(lines)


async def build_report(only_available: bool = False) -> str:
    matches = await fetch_matches()
    if only_available:
        matches = [m for m in matches if m["available"]]
        if not matches:
            return "Сейчас нет событий с билетами в продаже."
    else:
        matches.sort(key=lambda m: not m["available"])  # сначала те, где есть билеты
        if not matches:
            return "События на сайте не найдены."
    return "\n\n".join(format_match(m) for m in matches)


# ----------------------------- КОМАНДЫ БОТА -----------------------------
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎟 Актуальные билеты", callback_data="tickets")],
            [InlineKeyboardButton(text="📅 Дни до матча", callback_data="days")],
            [InlineKeyboardButton(text="🛒 Ссылки на продажу", callback_data="links")],
        ]
    )


async def do_tickets(msg: Message):
    try:
        text = await build_report()
    except Exception as e:
        log.exception("Ошибка /check")
        text = f"⚠️ Не удалось получить данные с сайта: {e}"
    await msg.answer(text)


async def do_days(msg: Message):
    try:
        matches = [m for m in await fetch_matches() if m["date"]]
        text = "\n".join(f"⚽ {m['title']}: {days_left(m['date'])}" for m in matches)
        text = text or "Матчей с датой не найдено."
    except Exception as e:
        log.exception("Ошибка /days")
        text = f"⚠️ Не удалось получить данные с сайта: {e}"
    await msg.answer(text)


async def do_links(msg: Message):
    try:
        matches = await fetch_matches()
        text = "\n".join(f"🛒 {m['title']}\n{m['url']}" for m in matches)
        text = text or "Ссылок не найдено."
    except Exception as e:
        log.exception("Ошибка /link")
        text = f"⚠️ Не удалось получить данные с сайта: {e}"
    await msg.answer(text)


@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer("Привет! Выбери действие:", reply_markup=main_kb())


@dp.message(Command("check"))
async def cmd_check(m: Message):
    await do_tickets(m)


@dp.message(Command("days"))
async def cmd_days(m: Message):
    await do_days(m)


@dp.message(Command("link"))
async def cmd_link(m: Message):
    await do_links(m)


@dp.callback_query(F.data == "tickets")
async def cb_tickets(c: CallbackQuery):
    await c.answer()
    await do_tickets(c.message)


@dp.callback_query(F.data == "days")
async def cb_days(c: CallbackQuery):
    await c.answer()
    await do_days(c.message)


@dp.callback_query(F.data == "links")
async def cb_links(c: CallbackQuery):
    await c.answer()
    await do_links(c.message)


# ----------------------------- АВТОРАССЫЛКА И МОНИТОРИНГ -----------------------------
async def scheduled_report():
    try:
        text = "🕛 Актуальные билеты:\n\n" + await build_report(only_available=True)
    except Exception as e:
        log.exception("Ошибка авторассылки")
        text = f"⚠️ Не удалось получить данные с сайта: {e}"
    await bot.send_message(CHAT_ID, text)


async def watch():
    """Раз в минуту сравнивает состояние сайта с предыдущим и шлёт уведомления."""
    global last_state
    try:
        new = {m["title"]: m for m in await fetch_matches()}
    except Exception as e:
        log.warning("Проверка сайта не удалась: %s", e)
        return

    if last_state:  # при первом запуске только запоминаем состояние
        for title, m in new.items():
            old = last_state.get(title)
            if old is None:
                await bot.send_message(CHAT_ID, "🆕 Новое событие на сайте:\n\n" + format_match(m))
            elif m["available"] and not old["available"]:
                await bot.send_message(CHAT_ID, "🔔 ПОЯВИЛИСЬ БИЛЕТЫ!\n\n" + format_match(m))
            elif old["available"] and not m["available"]:
                await bot.send_message(CHAT_ID, "❌ Билеты закончились:\n\n" + format_match(m))
    last_state = new


# ----------------------------- ЗАПУСК -----------------------------
async def main():
    global bot, last_state

    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Создай файл .env (см. .env.example)")
    if not CHAT_ID:
        raise SystemExit("Не задан CHAT_ID. Добавь его в файл .env")

    bot = Bot(TOKEN, default=DefaultBotProperties(link_preview_is_disabled=True))

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="check", description="Актуальные билеты"),
            BotCommand(command="days", description="Дни до матча"),
            BotCommand(command="link", description="Ссылки на продажу"),
        ]
    )

    try:
        last_state = {m["title"]: m for m in await fetch_matches()}
        log.info("Начальное состояние загружено: %d событий", len(last_state))
    except Exception as e:
        log.warning("Не удалось загрузить начальное состояние: %s", e)

    scheduler = AsyncIOScheduler(timezone=TZ)
    for hour, minute in SEND_TIMES:
        scheduler.add_job(scheduled_report, "cron", hour=hour, minute=minute)
    scheduler.add_job(watch, "interval", seconds=WATCH_INTERVAL, max_instances=1)
    scheduler.start()

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile
