"""Поддержка (тикеты) и рассылка."""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from config import ADMIN_IDS
from db import (S, Reward, Ticket, TicketMsg, User, add_ledger, get_decimal, get_state,
                set_state)
from ui import esc, fmt_money, kb, show
from user import access, notify_admins

SUPER = set(ADMIN_IDS)

# ================================================================ пользовательская часть
sr = Router()


@sr.callback_query(F.data == "sup")
async def cb_sup(c: CallbackQuery):
    if not await access(c):
        return
    async with S() as s:
        await set_state(s, c.from_user.id, "sup_write", "")
        t = await s.scalar(select(Ticket).where(Ticket.user_id == c.from_user.id, Ticket.status == "open"))
        await s.commit()
    note = f"У вас открыт тикет #{t.id}, сообщение будет добавлено в него.\n\n" if t else ""
    await show(c, note + "✍️ Напишите сообщение для поддержки одним сообщением.",
               kb([("⬅️ Назад", "home")]))


async def _sup_state(m: Message) -> bool:
    if not m.text or m.text.startswith("/"):
        return False
    async with S() as s:
        action, _ = await get_state(s, m.from_user.id)
    return action == "sup_write"


@sr.message(F.text, _sup_state)
async def on_sup_text(m: Message, bot: Bot):
    uid = m.from_user.id
    async with S() as s:
        u = await s.get(User, uid)
        if u is None or u.blocked or not u.rules_ok:
            return
        t = await s.scalar(select(Ticket).where(Ticket.user_id == uid, Ticket.status == "open"))
        new = t is None
        if new:
            t = Ticket(user_id=uid)
            s.add(t)
            await s.flush()
        s.add(TicketMsg(ticket_id=t.id, sender_id=uid, is_admin=False, text=m.text[:3000]))
        await set_state(s, uid, "", "")
        await s.commit()
    await m.answer(f"✅ {'Тикет #%d создан' % t.id if new else 'Сообщение добавлено в тикет #%d' % t.id}. "
                   "Ответ придёт сюда.", reply_markup=kb([("⬅️ В меню", "home")]))
    who = f"@{esc(m.from_user.username)}" if m.from_user.username else f"ID {uid}"
    await notify_admins(bot, f"🆘 {'Новый тикет' if new else 'Сообщение в тикете'} #{t.id} от {who}:\n\n{esc(m.text[:500])}",
                        kb([("📄 Открыть", f"a:tk:{t.id}")]))


# ================================================================ админская часть
ar2 = Router()
ar2.message.filter(F.from_user.id.in_(SUPER))
ar2.callback_query.filter(F.from_user.id.in_(SUPER))


@ar2.callback_query(F.data == "a:tickets")
async def cb_tickets(c: CallbackQuery):
    async with S() as s:
        rows = (await s.scalars(select(Ticket).where(Ticket.status == "open")
                                .order_by(Ticket.created_at.desc()).limit(20))).all()
    if not rows:
        await show(c, "🆘 Открытых тикетов нет.", kb([("🔙 Админ-панель", "a:home")]))
        return
    btns = [[(f"Тикет #{t.id} · {t.user_id}", f"a:tk:{t.id}")] for t in rows]
    await show(c, f"🆘 <b>Открытые тикеты</b> ({len(rows)})", kb(*btns, [("🔙 Админ-панель", "a:home")]))


@ar2.callback_query(F.data.startswith("a:tk:"))
async def cb_ticket(c: CallbackQuery):
    tid = int(c.data.split(":")[2])
    async with S() as s:
        t = await s.get(Ticket, tid)
        msgs = (await s.scalars(select(TicketMsg).where(TicketMsg.ticket_id == tid)
                                .order_by(TicketMsg.id.desc()).limit(10))).all() if t else []
    if not t:
        await c.answer("Тикет не найден.", show_alert=True)
        return
    body = "\n\n".join(f"<b>{'Админ' if x.is_admin else 'Пользователь'}:</b> {esc(x.text)}"
                       for x in reversed(msgs)) or "Сообщений нет."
    rows = []
    if t.status == "open":
        rows.append([("💬 Ответить", f"a:tkr:{tid}"), ("🔒 Закрыть", f"a:tkc:{tid}")])
    rows += [[("👤 Пользователь", f"a:view:{t.user_id}")], [("🔙 Тикеты", "a:tickets")]]
    await show(c, f"🆘 <b>Тикет #{tid}</b> ({t.status})\nПользователь: <code>{t.user_id}</code>\n\n{body}", kb(*rows))


@ar2.callback_query(F.data.startswith("a:tkr:"))
async def cb_ticket_reply(c: CallbackQuery):
    tid = c.data.split(":")[2]
    async with S() as s:
        await set_state(s, c.from_user.id, "adm:tkreply", tid)
        await s.commit()
    await show(c, f"Напишите ответ по тикету #{tid}.", kb([("⬅️ Отмена", "a:cancel")]))


@ar2.callback_query(F.data.startswith("a:tkc:"))
async def cb_ticket_close(c: CallbackQuery, bot: Bot):
    tid = int(c.data.split(":")[2])
    async with S() as s:
        t = await s.get(Ticket, tid)
        if t:
            t.status = "closed"
            await s.commit()
    if t:
        try:
            await bot.send_message(t.user_id, f"🔒 Тикет #{tid} закрыт.")
        except Exception:
            pass
    await show(c, f"🔒 Тикет #{tid} закрыт.", kb([("🔙 Тикеты", "a:tickets")]))


async def _adm_text_state(m: Message) -> bool:
    if not m.text or m.text.startswith("/"):
        return False
    async with S() as s:
        action, _ = await get_state(s, m.from_user.id)
    return action == "adm:tkreply"


@ar2.message(F.text, _adm_text_state)
async def on_ticket_reply(m: Message, bot: Bot):
    async with S() as s:
        _, data = await get_state(s, m.from_user.id)
        tid = int(data)
        t = await s.get(Ticket, tid)
        if not t:
            await set_state(s, m.from_user.id, "", "")
            await s.commit()
            await m.answer("Тикет не найден.")
            return
        s.add(TicketMsg(ticket_id=tid, sender_id=m.from_user.id, is_admin=True, text=m.text[:3000]))
        await set_state(s, m.from_user.id, "", "")
        await s.commit()
    try:
        await bot.send_message(t.user_id, f"🛠 <b>Ответ по тикету #{tid}:</b>\n{esc(m.text)}",
                               reply_markup=kb([("💬 Ответить", "sup")]))
        await m.answer("✅ Ответ отправлен.", reply_markup=kb([("🔙 К тикету", f"a:tk:{tid}")]))
    except Exception:
        await m.answer("⚠️ Ответ сохранён, но пользователю не доставлен (он мог заблокировать бота).",
                       reply_markup=kb([("🔙 К тикету", f"a:tk:{tid}")]))


# ---------------------------------------------------------------- рассылка
@ar2.callback_query(F.data == "a:bc")
async def cb_bc(c: CallbackQuery):
    async with S() as s:
        await set_state(s, c.from_user.id, "adm:broadcast", "")
        await s.commit()
    await show(c, "📢 <b>Рассылка</b>\n\nОтправьте одним сообщением то, что нужно разослать "
                  "(текст, фото, видео, документ). Перед отправкой я попрошу подтверждение.",
               kb([("⬅️ Отмена", "a:cancel")]))


async def _bc_state(m: Message) -> bool:
    if m.text and m.text.startswith("/"):
        return False
    async with S() as s:
        action, _ = await get_state(s, m.from_user.id)
    return action == "adm:broadcast"


@ar2.message(_bc_state)
async def on_bc_message(m: Message):
    async with S() as s:
        n = await s.scalar(select(func.count()).select_from(User).where(User.blocked == False)) or 0  # noqa: E712
        await set_state(s, m.from_user.id, "adm:bcready", f"{m.chat.id}:{m.message_id}")
        await s.commit()
    await m.answer(f"Разослать это сообщение <b>{n}</b> пользователям?",
                   reply_markup=kb([("✅ Отправить", "a:bcgo")], [("⬅️ Отмена", "a:cancel")]))


async def _broadcast(bot: Bot, admin_id: int, from_chat: int, msg_id: int, ids):
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, from_chat, msg_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.04)
    try:
        await bot.send_message(admin_id, f"📢 Рассылка завершена.\nУспешно: {ok}\nОшибок: {fail}")
    except Exception:
        logging.exception("broadcast report")


@ar2.callback_query(F.data == "a:bcgo")
async def cb_bcgo(c: CallbackQuery, bot: Bot):
    async with S() as s:
        action, data = await get_state(s, c.from_user.id)
        if action != "adm:bcready":
            await c.answer("Сессия истекла.", show_alert=True)
            return
        ids = (await s.scalars(select(User.id).where(User.blocked == False))).all()  # noqa: E712
        await set_state(s, c.from_user.id, "", "")
        await s.commit()
    chat_id, msg_id = data.split(":")
    asyncio.create_task(_broadcast(bot, c.from_user.id, int(chat_id), int(msg_id), list(ids)))
    await show(c, f"📢 Рассылка запущена ({len(ids)} получателей). Отчёт придёт по завершении.",
               kb([("🔙 Админ-панель", "a:home")]))


# ---------------------------------------------------------------- удаление / бан реферала
async def _detach_referral(rid: int, block: bool):
    """Списывает награду у пригласившего. Возвращает (owner_id, amount) или None."""
    async with S() as s:
        ref_user = await s.get(User, rid)
        if ref_user is None:
            return "missing"
        owner_id = ref_user.referrer_id
        amount = None
        if owner_id:
            rw = await s.scalar(select(Reward).where(Reward.referral_id == rid))
            owner = await s.get(User, owner_id)
            if rw is not None and rw.status == "credited" and owner is not None:
                owner.balance -= rw.amount
                rw.status = "cancelled"
                add_ledger(s, owner.id, -rw.amount, "referral_removed",
                           f"Реферал {rid} {'заблокирован' if block else 'удалён'} администратором")
                amount = rw.amount
            ref_user.referrer_id = None
        if block:
            ref_user.blocked = True
            ref_user.ban_reason = "Нарушение правил"
        rate = await get_decimal(s, "usd_rub")
        await s.commit()
    return owner_id, amount, rate


@ar2.callback_query(F.data.startswith(("a:remref:", "a:blockref:")))
async def cb_remove_referral(c: CallbackQuery, bot: Bot):
    _, kind, rid = c.data.split(":")
    block = kind == "blockref"
    res = await _detach_referral(int(rid), block)
    if res == "missing":
        await c.answer("Пользователь не найден.", show_alert=True)
        return
    owner_id, amount, rate = res
    if owner_id and block:
        extra = f" С баланса списано {fmt_money(amount, rate)}." if amount else ""
        try:
            await bot.send_message(
                owner_id, "⚠️ <b>Предупреждение</b>\n\nОдин из ваших рефералов заблокирован администрацией "
                          f"за нарушение правил.{extra}\n\n⛔ При повторном нарушении ваш аккаунт будет заблокирован.")
        except Exception:
            pass
    note = f" Награда списана: {fmt_money(amount, rate)}." if amount else " Награды к списанию не было."
    await show(c, f"{'🚫 Реферал заблокирован.' if block else '🗑 Реферал убран.'}{note}",
               kb([("🔙 К пользователю", f"a:view:{owner_id}")] if owner_id else [("🔙 Админ-панель", "a:home")]))
