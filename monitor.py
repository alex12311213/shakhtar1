"""Одноразовый запуск для GitHub Actions: python monitor.py watch | report"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from bot import fetch_matches, format_match  # берём парсер из вашего bot.py

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
STATE = Path("state.json")


async def send(text: str):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        )
        r.raise_for_status()


async def watch():
    matches = await fetch_matches()
    old = json.loads(STATE.read_text("utf-8")) if STATE.exists() else None
    if old is not None:  # при первом запуске только запоминаем состояние
        for m in matches:
            prev = old.get(m["title"])
            if prev is None:
                await send(" Новое событие на сайте:\n\n" + format_match(m))
            elif m["available"] and not prev:
                await send(" ПОЯВИЛИСЬ БИЛЕТЫ!\n\n" + format_match(m))
            elif prev and not m["available"]:
                await send(" Билеты закончились:\n\n" + format_match(m))
    STATE.write_text(
        json.dumps({m["title"]: m["available"] for m in matches}, ensure_ascii=False, indent=1),
        "utf-8",
    )


async def report():
    avail = [m for m in await fetch_matches() if m["available"]]
    body = "\n\n".join(format_match(m) for m in avail) or "Сейчас нет событий с билетами в продаже."
    await send(" Актуальные билеты:\n\n" + body)


if __name__ == "__main__":
    asyncio.run(report() if sys.argv[1:] == ["report"] else watch())
