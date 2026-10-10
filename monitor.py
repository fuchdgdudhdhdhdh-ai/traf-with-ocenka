"""Фоновая проверка: если реферал отписался от публичного канала, награда списывается.

Работает только при включённой настройке «Списание при отписке» (по умолчанию выключено)
и только для наград за последние N дней (настройка «Дней проверки»).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select

from db import S, Reward, User, add_ledger, get_decimal, get_int, get_setting
from ui import fmt_money

INTERVAL = 600  # секунд между циклами


async def _left(bot: Bot, chat, uid: int):
    """True — точно вышел, False — состоит, None — не удалось проверить (ничего не делаем)."""
    try:
        member = await bot.get_chat_member(chat, uid)
    except Exception as e:
        logging.info("clawback check skipped for %s: %s", uid, e)
        return None
    return member.status in ("left", "kicked")


async def check_once(bot: Bot):
    async with S() as s:
        if (await get_setting(s, "clawback")) != "open":
            return
        chat = await get_setting(s, "ch3_id")
        days = await get_int(s, "clawback_days")
        if not chat:
            return
        since = datetime.now(timezone.utc) - timedelta(days=days)
        ids = (await s.scalars(select(Reward.id).where(Reward.status == "credited",
                                                       Reward.created_at >= since))).all()
    for rid in ids:
        async with S() as s:
            rw = await s.get(Reward, rid)
            if rw is None or rw.status != "credited":
                continue
            if await _left(bot, chat, rw.referral_id) is not True:
                await asyncio.sleep(0.05)
                continue
            owner = await s.get(User, rw.referrer_id)
            rw.status = "cancelled"
            if owner:
                owner.balance -= rw.amount
                add_ledger(s, owner.id, -rw.amount, "referral_unsubscribed",
                           f"Реферал {rw.referral_id} отписался")
            rate = await get_decimal(s, "usd_rub")
            await s.commit()
        if owner:
            try:
                await bot.send_message(
                    owner.id, f"⚠️ Ваш реферал отписался от канала. Награда списана: "
                              f"<b>−{fmt_money(rw.amount, rate)}</b>")
            except Exception:
                pass
        await asyncio.sleep(0.05)


async def monitor(bot: Bot):
    while True:
        try:
            await check_once(bot)
        except Exception:
            logging.exception("monitor")
        await asyncio.sleep(INTERVAL)
