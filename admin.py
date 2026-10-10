import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

import rating
import crypto
from config import ADMIN_IDS
from db import (CHANNEL_SLOTS, PUBLIC_SLOT, S, Reward, Ticket, User, Withdrawal, add_ledger,
                del_setting, get_decimal, get_int, get_setting, get_state, set_setting,
                set_state)
from ui import METHOD_LABELS, esc, fmt_money, fmt_rub, fmt_usd, kb, net_text, q2, ref_status, show

ar = Router()
SUPER = set(ADMIN_IDS)
ar.message.filter(F.from_user.id.in_(SUPER))
ar.callback_query.filter(F.from_user.id.in_(SUPER))

PAGE = 8
SETTINGS_META = {
    "min_rating": ("⭐ Мин. рейтинг", "int"),
    "reward_usd": ("💵 Награда, $", "pos"),
    "usd_rub": ("💱 Курс $→₽", "pos"),
    "min_withdraw_usd": ("💸 Мин. вывод, $", "pos"),
    "card_fee_rub": ("🏦 Комиссия карты, ₽", "nonneg"),
    "clawback_days": ("📅 Дней проверки отписки", "pos"),
}
TEXT_KEYS = {
    "rules": "📜 Правила (шаг 1)",
    "rules_warn": "⚠️ Правила (предупреждение)",
    "menu": "🏠 Главное меню",
    "info": "ℹ️ Информация",
    "faq": "❓ FAQ",
    "criteria": "📋 Критерии рейтинга",
}
FILTERS = {
    "all": ("Все", []),
    "ok": ("✅ Прошли", [User.passed == True]),               # noqa: E712
    "low": ("❌ Не прошли", [User.rating.is_not(None), User.passed == False]),  # noqa: E712
    "ban": ("⛔ Баны", [User.blocked == True]),               # noqa: E712
}


def parse_dec(text: str, signed: bool = False):
    try:
        d = Decimal(text.strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    if not signed and d < 0:
        return None
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def uname(u: User) -> str:
    return f"@{esc(u.username)}" if u.username else f"ID {u.id}"


# ================================================================ дашборд
async def cnt(s, *conds) -> int:
    return await s.scalar(select(func.count()).select_from(User).where(*conds)) or 0


async def dashboard(target):
    async with S() as s:
        total = await cnt(s)
        passed = await cnt(s, User.passed == True)  # noqa: E712
        failed = await cnt(s, User.rating.is_not(None), User.passed == False)  # noqa: E712
        banned = await cnt(s, User.blocked == True)  # noqa: E712
        pend_n = await s.scalar(select(func.count()).select_from(Withdrawal)
                                .where(Withdrawal.status == "pending")) or 0
        pend_sum = await s.scalar(select(func.coalesce(func.sum(Withdrawal.amount), 0))
                                  .where(Withdrawal.status == "pending")) or 0
        balances = await s.scalar(select(func.coalesce(func.sum(User.balance), 0))) or 0
        rewards_n = await s.scalar(select(func.count()).select_from(Reward).where(Reward.status == "credited")) or 0
        tick_n = await s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "open")) or 0
        rate = await get_decimal(s, "usd_rub")
        min_r = await get_int(s, "min_rating")
        bot_open = (await get_setting(s, "bot_status")) != "closed"
        pay_open = (await get_setting(s, "payout_status")) != "closed"
    text = (
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        f"Бот: {'🟢 открыт' if bot_open else '🛑 закрыт'} · Выплаты: {'🟢 открыты' if pay_open else '🛑 закрыты'}\n"
        f"Порог рейтинга: <b>{min_r}+</b> · Курс: 1$ = {fmt_rub(rate)}\n\n"
        f"👤 Пользователей: <b>{total}</b>\n"
        f"   ✅ прошли: {passed} · ❌ не прошли: {failed} · ⛔ в бане: {banned}\n"
        f"🎁 Засчитано рефералов: <b>{rewards_n}</b>\n"
        f"💰 На балансах: <b>{fmt_money(balances, rate)}</b>\n"
        f"💸 Заявок на выплату: <b>{pend_n}</b> на {fmt_money(pend_sum, rate)}"
    )
    badge = f" ({pend_n})" if pend_n else ""
    markup = kb(
        [("👥 Пользователи", "a:users:all:0"), (f"💸 Выплаты{badge}", "a:pay")],
        [("📺 Каналы", "a:channels"), ("⚙️ Настройки", "a:settings")],
        [(f"🆘 Тикеты{' (%d)' % tick_n if tick_n else ''}", "a:tickets"), ("📢 Рассылка", "a:bc")],
        [("📝 Тексты", "a:texts"), ("🔄 Обновить", "a:home")],
    )
    await show(target, text, markup)


@ar.message(Command("admin"))
async def cmd_admin(m: Message):
    async with S() as s:
        await set_state(s, m.from_user.id, "", "")
        await s.commit()
    await dashboard(m)


@ar.callback_query(F.data.in_({"a:home", "a:cancel"}))
async def cb_home(c: CallbackQuery):
    async with S() as s:
        await set_state(s, c.from_user.id, "", "")
        await s.commit()
    await dashboard(c)


# ================================================================ пользователи
@ar.callback_query(F.data.startswith("a:users:"))
async def cb_users(c: CallbackQuery):
    _, _, flt, off = c.data.split(":")
    off = int(off)
    label, conds = FILTERS.get(flt, FILTERS["all"])
    async with S() as s:
        total = await cnt(s, *conds)
        rows = (await s.scalars(select(User).where(*conds).order_by(User.created_at.desc())
                                .offset(off).limit(PAGE))).all()
    filter_row = [((f"• {v[0]}" if k == flt else v[0]), f"a:users:{k}:0") for k, v in FILTERS.items()]
    user_rows = []
    for u in rows:
        flag = "⛔" if u.blocked else ("✅" if u.passed else ("❌" if u.rating is not None else "⏳"))
        score = u.rating if u.rating is not None else "—"
        user_rows.append([(f"{flag} {('@' + u.username) if u.username else u.id} · {score} · {fmt_usd(u.balance)}",
                           f"a:view:{u.id}:{flt}:{off}")])
    nav = []
    if off > 0:
        nav.append(("⬅️", f"a:users:{flt}:{max(0, off - PAGE)}"))
    if off + PAGE < total:
        nav.append(("➡️", f"a:users:{flt}:{off + PAGE}"))
    markup = kb(filter_row[:2], filter_row[2:], *user_rows, *([nav] if nav else []),
                [("🔎 Найти по ID / @username", "a:find")], [("🔙 Админ-панель", "a:home")])
    shown = f"{off + 1}-{off + len(rows)}" if rows else "0"
    await show(c, f"👥 <b>Пользователи</b> — {label}\nВсего: {total}, показаны {shown}\n\n"
                  "⏳ — не завершил проверку", markup)


@ar.callback_query(F.data == "a:find")
async def cb_find(c: CallbackQuery):
    async with S() as s:
        await set_state(s, c.from_user.id, "adm:find", "")
        await s.commit()
    await show(c, "🔎 Отправьте Telegram ID или @username пользователя.",
               kb([("⬅️ Отмена", "a:cancel")]))


async def user_card(uid: int, back: str = "a:users:all:0"):
    async with S() as s:
        u = await s.get(User, uid)
        if u is None:
            return None, None
        ref_label = "—"
        if u.referrer_id:
            ref = await s.get(User, u.referrer_id)
            ref_label = uname(ref) if ref else f"ID {u.referrer_id}"
        refs_n = await s.scalar(select(func.count()).select_from(User).where(User.referrer_id == uid)) or 0
        credited = await s.scalar(select(func.count()).select_from(Reward).where(Reward.referrer_id == uid, Reward.status == "credited")) or 0
        rate = await get_decimal(s, "usd_rub")
        min_r = await get_int(s, "min_rating")
    if u.blocked:
        status = "⛔ Заблокирован" + (f"\nПричина: {esc(u.ban_reason)}" if u.ban_reason else "")
    elif u.rating is None:
        status = "⏳ Не завершил проверку"
    elif not u.passed:
        status = "❌ Не прошёл проверку"
    elif not u.subscribed:
        status = "🟡 Прошёл рейтинг, не подписался"
    else:
        status = "✅ Активен"
    notes = ""
    if u.rating_notes:
        notes = "\n" + "\n".join(f"   • {esc(n)}" for n in u.rating_notes.split("\n"))
    rating_line = f"{u.rating}/100 (порог {min_r})" if u.rating is not None else "—"
    text = (f"👤 <b>{u.id}</b> {('@' + esc(u.username)) if u.username else ''}\n\n"
            f"Статус: {status}\n"
            f"Рейтинг: {rating_line}{notes}\n"
            f"Регистрация: {u.created_at.strftime('%d.%m.%Y %H:%M')}\n"
            f"Баланс: <b>{fmt_money(u.balance, rate)}</b>\n"
            f"Пригласил: {ref_label}\n"
            f"Рефералов: {refs_n} (засчитано: {credited})")
    ban_btn = ("✅ Разбанить", f"a:unban:{uid}") if u.blocked else ("⛔ Забанить", f"a:ban:{uid}")
    markup = kb([("💰 Баланс", f"a:bal:{uid}"), ("👥 Рефералы", f"a:refs:{uid}")],
                [("✉️ Написать", f"a:msg:{uid}"), ban_btn],
                [("🔙 К списку", back)])
    return text, markup


@ar.callback_query(F.data.startswith("a:view:"))
async def cb_view(c: CallbackQuery):
    parts = c.data.split(":")
    uid = int(parts[2])
    back = f"a:users:{parts[3]}:{parts[4]}" if len(parts) >= 5 else "a:users:all:0"
    text, markup = await user_card(uid, back)
    if not text:
        await c.answer("Пользователь не найден.", show_alert=True)
        return
    await show(c, text, markup)


@ar.callback_query(F.data.startswith("a:refs:"))
async def cb_refs(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    async with S() as s:
        owner = await s.get(User, uid)
        rows = (await s.scalars(select(User).where(User.referrer_id == uid)
                                .order_by(User.created_at.desc()).limit(25))).all()
        credited = set((await s.scalars(select(Reward.referral_id).where(Reward.referrer_id == uid, Reward.status == "credited"))).all())
    lines = [f"• {uname(x)} — {ref_status(x, x.id in credited)}" for x in rows] or ["Нет рефералов."]
    btns = []
    for x in rows[:10]:
        label = f"{('@' + x.username) if x.username else x.id}"
        btns.append([(label, f"a:view:{x.id}"), ("🗑", f"a:remref:{x.id}"), ("🚫", f"a:blockref:{x.id}")])
    legend = "\n\n🗑 — убрать реферала и списать награду · 🚫 — то же + бан реферала"
    await show(c, f"👥 Рефералы {uname(owner) if owner else uid}\n\n" + "\n".join(lines) + (legend if rows else ""),
               kb(*btns, [("🔙 К карточке", f"a:view:{uid}")]))


@ar.callback_query(F.data.startswith(("a:bal:", "a:ban:", "a:msg:")))
async def cb_user_action(c: CallbackQuery):
    _, kind, uid = c.data.split(":")
    prompts = {
        "bal": ("adm:bal", "Отправьте сумму в $ со знаком, например <code>+1.4</code> или <code>-2</code>."),
        "ban": ("adm:ban", "Отправьте причину блокировки — пользователь получит её сообщением."),
        "msg": ("adm:msg", "Отправьте текст сообщения для пользователя."),
    }
    action, prompt = prompts[kind]
    async with S() as s:
        await set_state(s, c.from_user.id, action, uid)
        await s.commit()
    await show(c, f"{prompt}\nПользователь: <code>{uid}</code>", kb([("⬅️ Отмена", "a:cancel")]))


@ar.callback_query(F.data.startswith("a:unban:"))
async def cb_unban(c: CallbackQuery, bot: Bot):
    uid = int(c.data.split(":")[2])
    async with S() as s:
        u = await s.get(User, uid)
        if not u:
            await c.answer("Не найден.", show_alert=True)
            return
        u.blocked = False
        u.ban_reason = None
        await s.commit()
    try:
        await bot.send_message(uid, "✅ Вы разблокированы и снова можете пользоваться ботом.")
    except Exception:
        pass
    text, markup = await user_card(uid)
    await show(c, "✅ Пользователь разблокирован.\n\n" + text, markup)


# ================================================================ выплаты
@ar.callback_query(F.data == "a:pay")
async def cb_pay(c: CallbackQuery):
    async with S() as s:
        rows = (await s.scalars(select(Withdrawal).where(Withdrawal.status == "pending")
                                .order_by(Withdrawal.created_at))).all()
    if not rows:
        await show(c, "💸 Заявок на выплату нет.", kb([("🔙 Админ-панель", "a:home")]))
        return
    btns = [[(f"#{w.id} · {fmt_usd(w.amount)} · {METHOD_LABELS.get(w.method, w.method)} · {w.user_id}",
              f"a:payview:{w.id}")] for w in rows[:20]]
    more = f"\n…и ещё {len(rows) - 20}" if len(rows) > 20 else ""
    await show(c, f"💸 <b>Заявки на выплату</b> ({len(rows)}){more}", kb(*btns, [("🔙 Админ-панель", "a:home")]))


@ar.callback_query(F.data.startswith("a:payview:"))
async def cb_payview(c: CallbackQuery):
    wid = int(c.data.split(":")[2])
    async with S() as s:
        w = await s.get(Withdrawal, wid)
        u = await s.get(User, w.user_id) if w else None
    if not w:
        await c.answer("Заявка не найдена.", show_alert=True)
        return
    who = f'<a href="tg://user?id={w.user_id}">{uname(u) if u else w.user_id}</a>'
    fee = fmt_rub(w.amount * w.rate - w.net) if w.currency == "RUB" else "—"
    text = (f"💸 <b>Заявка #{w.id}</b> — {w.status}\n\n"
            f"Пользователь: {who} (<code>{w.user_id}</code>)\n"
            f"Списано: <b>{fmt_usd(w.amount)}</b> (курс {fmt_rub(w.rate)})\n"
            f"Комиссия: {fee}\n"
            f"К выплате: <b>{net_text(w)}</b>\n"
            f"Способ: {METHOD_LABELS.get(w.method, w.method)}\n"
            f"Реквизиты: <code>{esc(w.destination)}</code>\n"
            f"Создана: {w.created_at.strftime('%d.%m.%Y %H:%M')}")
    if w.status != "pending":
        await show(c, text, kb([("🔙 Список", "a:pay")]))
        return
    paid_label = ("🤖 Выдать чек" if crypto.TOKEN else "🤖 Отправить чек") if w.method == "crypto" else "✅ Выплачено"
    await show(c, text, kb([(paid_label, f"a:paid:{w.id}"), ("❌ Отклонить", f"a:rej:{w.id}")],
                           [("👤 Пользователь", f"a:view:{w.user_id}")],
                           [("🔙 Список", "a:pay")]))


async def auto_crypto(c: CallbackQuery, bot: Bot, wid: int) -> bool:
    """Создаёт чек через Crypto Pay. True — заявка закрыта; False — делаем вручную."""
    async with S() as s:
        w = await s.get(Withdrawal, wid)
        if not w or w.status != "pending" or w.method != "crypto":
            return False
        w.status = "processing"   # защита от двойного нажатия
        await s.commit()
        amount, uid = w.net, w.user_id
    url, err = await crypto.create_check(amount, uid)
    async with S() as s:
        w = await s.get(Withdrawal, wid)
        if url is None:
            w.status = "pending"
            await s.commit()
            await c.answer(f"Авто-чек не создан ({err}). Отправьте чек вручную.", show_alert=True)
            return False
        w.status = "paid"
        w.note = url
        w.processed_at = datetime.now(timezone.utc)
        add_ledger(s, uid, 0, "withdraw_paid", f"Заявка #{wid}: чек CryptoBot (авто)")
        await s.commit()
    try:
        await bot.send_message(uid, f"✅ Заявка #{wid} выплачена: <b>{net_text(w)}</b>\n🤖 Ваш чек CryptoBot:\n{esc(url)}")
    except Exception:
        pass
    await show(c, f"✅ Заявка #{wid}: чек создан автоматически и отправлен пользователю.",
               kb([("🔙 Список", "a:pay")]))
    return True


@ar.callback_query(F.data.startswith("a:paid:"))
async def cb_paid(c: CallbackQuery, bot: Bot):
    wid = int(c.data.split(":")[2])
    if crypto.TOKEN and await auto_crypto(c, bot, wid):
        return
    async with S() as s:
        w = await s.get(Withdrawal, wid)
        if not w or w.status != "pending":
            await c.answer("Заявка уже обработана или не найдена.", show_alert=True)
            return
        if w.method == "crypto":
            await set_state(s, c.from_user.id, "adm:check", str(wid))
            await s.commit()
            await show(c, f"Заявка #{wid}: {net_text(w)}.\n\nСоздайте чек в @CryptoBot и отправьте сюда ссылку на него — "
                          "я перешлю её пользователю и закрою заявку.", kb([("⬅️ Отмена", "a:cancel")]))
            return
        w.status = "paid"
        w.processed_at = datetime.now(timezone.utc)
        add_ledger(s, w.user_id, 0, "withdraw_paid", f"Заявка #{wid} выплачена")
        await s.commit()
    try:
        await bot.send_message(w.user_id, f"✅ Заявка #{wid} выплачена: <b>{net_text(w)}</b>")
    except Exception:
        pass
    await show(c, f"✅ Заявка #{wid} отмечена как выплаченная.", kb([("🔙 Список", "a:pay")]))


@ar.callback_query(F.data.startswith("a:rej:"))
async def cb_reject(c: CallbackQuery, bot: Bot):
    wid = int(c.data.split(":")[2])
    async with S() as s:
        w = await s.get(Withdrawal, wid)
        if not w or w.status != "pending":
            await c.answer("Заявка уже обработана или не найдена.", show_alert=True)
            return
        w.status = "rejected"
        w.processed_at = datetime.now(timezone.utc)
        u = await s.get(User, w.user_id)
        u.balance += w.amount
        add_ledger(s, u.id, w.amount, "withdraw_refund", f"Заявка #{wid} отклонена")
        await s.commit()
    try:
        await bot.send_message(w.user_id, f"❌ Заявка #{wid} отклонена, {fmt_usd(w.amount)} возвращены на баланс.")
    except Exception:
        pass
    await show(c, f"❌ Заявка #{wid} отклонена, средства возвращены.", kb([("🔙 Список", "a:pay")]))


# ================================================================ настройки
async def settings_screen(target):
    async with S() as s:
        vals = {k: await get_setting(s, k) for k in SETTINGS_META}
        bot_open = (await get_setting(s, "bot_status")) != "closed"
        pay_open = (await get_setting(s, "payout_status")) != "closed"
        claw_on = (await get_setting(s, "clawback")) == "open"
    lines = []
    for key, (label, _) in SETTINGS_META.items():
        lines.append(f"{label}: <b>{vals[key]}</b>")
    keys = list(SETTINGS_META)
    rows = [[(SETTINGS_META[k][0], f"a:set:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([(f"{'🟢' if bot_open else '🛑'} Бот: {'открыт' if bot_open else 'закрыт'}", "a:toggle:bot_status")])
    rows.append([(f"{'🟢' if pay_open else '🛑'} Выплаты: {'открыты' if pay_open else 'закрыты'}", "a:toggle:payout_status")])
    rows.append([(f"{'🟢' if claw_on else '⚪'} Списание при отписке: {'вкл' if claw_on else 'выкл'}", "a:toggle:clawback")])
    rows.append([("🔙 Админ-панель", "a:home")])
    await show(target, "⚙️ <b>Настройки</b>\n\n" + "\n".join(lines) + "\n\nНажмите, чтобы изменить:", kb(*rows))


@ar.callback_query(F.data == "a:settings")
async def cb_settings(c: CallbackQuery):
    await settings_screen(c)


@ar.callback_query(F.data.startswith("a:set:"))
async def cb_set(c: CallbackQuery):
    key = c.data.split(":", 2)[2]
    if key not in SETTINGS_META:
        return
    async with S() as s:
        await set_state(s, c.from_user.id, "adm:set", key)
        cur = await get_setting(s, key)
        await s.commit()
    hint = "целое число 0–100" if SETTINGS_META[key][1] == "int" else "число, например 1.4"
    await show(c, f"{SETTINGS_META[key][0]}\nСейчас: <b>{cur}</b>\n\nОтправьте новое значение ({hint}).",
               kb([("⬅️ Отмена", "a:cancel")]))


@ar.callback_query(F.data.startswith("a:toggle:"))
async def cb_toggle(c: CallbackQuery):
    key = c.data.split(":")[2]
    if key not in ("bot_status", "payout_status", "clawback"):
        return
    async with S() as s:
        cur = await get_setting(s, key)
        await set_setting(s, key, "open" if cur == "closed" else "closed")
        await s.commit()
    await settings_screen(c)


# ================================================================ каналы
async def channels_screen(target):
    lines = []
    btns = []
    async with S() as s:
        for n in CHANNEL_SLOTS:
            title = await get_setting(s, f"ch{n}_title")
            url = await get_setting(s, f"ch{n}_url")
            kind = "📢 публичный, проверяется" if n == PUBLIC_SLOT else "🔒 частный, не проверяется"
            lines.append(f"<b>{n}. {esc(title)}</b> ({kind})\n   {esc(url) or '— ссылка не задана —'}")
            btns.append([(f"{n} · {title}", f"a:ch:{n}")])
        ch_id = await get_setting(s, "ch3_id")
    lines.append(f"\nID публичного канала: <code>{esc(ch_id) or '—'}</code>")
    await show(target, "📺 <b>Каналы</b> (пользователь получает все 5)\n\n" + "\n".join(lines),
               kb(*btns, [("🔙 Админ-панель", "a:home")]))


@ar.callback_query(F.data == "a:channels")
async def cb_channels(c: CallbackQuery):
    await channels_screen(c)


async def slot_screen(target, n: int):
    async with S() as s:
        title = await get_setting(s, f"ch{n}_title")
        url = await get_setting(s, f"ch{n}_url")
        ch_id = await get_setting(s, "ch3_id")
    if n == PUBLIC_SLOT:
        kind = ("📢 Публичный канал. Бот должен быть его администратором — только здесь проверяется подписка.\n"
                f"ID/username: <code>{esc(ch_id) or '—'}</code>")
    else:
        kind = "🔒 Частный канал. Подписка не проверяется — нужна только ссылка-приглашение."
    rows = [[("✏️ Название", f"a:setch:{n}:title"), ("🔗 Ссылка", f"a:setch:{n}:url")]]
    if n == PUBLIC_SLOT:
        rows.append([("🆔 ID канала", f"a:setch:{n}:id")])
    rows.append([("🗑 Очистить", f"a:chclear:{n}")])
    rows.append([("🔙 Каналы", "a:channels")])
    await show(target, f"<b>Канал {n}</b>\n{kind}\n\nНазвание: {esc(title)}\nСсылка: {esc(url) or '—'}", kb(*rows))


@ar.callback_query(F.data.startswith("a:ch:"))
async def cb_slot(c: CallbackQuery):
    await slot_screen(c, int(c.data.split(":")[2]))


@ar.callback_query(F.data.startswith("a:chclear:"))
async def cb_chclear(c: CallbackQuery):
    n = int(c.data.split(":")[2])
    async with S() as s:
        await del_setting(s, f"ch{n}_url")
        await del_setting(s, f"ch{n}_title")
        if n == PUBLIC_SLOT:
            await del_setting(s, "ch3_id")
        await s.commit()
    await slot_screen(c, n)


@ar.callback_query(F.data.startswith("a:setch:"))
async def cb_setch(c: CallbackQuery):
    _, _, n, field = c.data.split(":")
    prompts = {
        "title": "название канала (до 40 символов)",
        "url": "ссылку на канал (https://t.me/...)",
        "id": "ID канала (например -1001234567890) или @username",
    }
    async with S() as s:
        await set_state(s, c.from_user.id, "adm:setch", f"{n}:{field}")
        await s.commit()
    await show(c, f"Канал {n}: отправьте {prompts[field]}.", kb([("⬅️ Отмена", "a:cancel")]))


# ================================================================ тексты
@ar.callback_query(F.data == "a:texts")
async def cb_texts(c: CallbackQuery):
    rows = [[(label, f"a:text:{k}")] for k, label in TEXT_KEYS.items()]
    await show(c, "📝 <b>Тексты</b>\n\nВыберите, что отредактировать. Форматирование (жирный, курсив) сохраняется.",
               kb(*rows, [("🔙 Админ-панель", "a:home")]))


@ar.callback_query(F.data.startswith("a:text:"))
async def cb_text(c: CallbackQuery):
    key = c.data.split(":", 2)[2]
    if key not in TEXT_KEYS:
        return
    async with S() as s:
        await set_state(s, c.from_user.id, "adm:text", key)
        cur = await get_setting(s, "text:" + key)
        min_r = await get_int(s, "min_rating")
        await s.commit()
    if key == "criteria" and not cur:
        cur = rating.criteria_text(min_r)
        note = "(сейчас генерируется автоматически)"
    else:
        note = ""
    await show(c, f"📝 <b>{TEXT_KEYS[key]}</b> {note}\n\n{cur}\n\n———\n"
                  "Отправьте новый текст одним сообщением.\n"
                  "Подстановки: {min_rating}, {reward}, {rate}",
               kb([("♻️ Сбросить по умолчанию", f"a:textreset:{key}")], [("⬅️ Отмена", "a:cancel")]))


@ar.callback_query(F.data.startswith("a:textreset:"))
async def cb_textreset(c: CallbackQuery):
    key = c.data.split(":", 2)[2]
    async with S() as s:
        await del_setting(s, "text:" + key)
        await set_state(s, c.from_user.id, "", "")
        await s.commit()
    await c.answer("Сброшено на значение по умолчанию.", show_alert=True)
    await cb_texts(c)


# ================================================================ ввод текста админом (состояния adm:*)
async def admin_state(m: Message) -> bool:
    if not m.text or m.text.startswith("/"):
        return False
    async with S() as s:
        action, _ = await get_state(s, m.from_user.id)
    return action.startswith("adm:")


async def clear_state(aid: int):
    async with S() as s:
        await set_state(s, aid, "", "")
        await s.commit()


@ar.message(F.text, admin_state)
async def on_admin_text(m: Message, bot: Bot):
    aid = m.from_user.id
    async with S() as s:
        action, data = await get_state(s, aid)
    text = m.text.strip()

    if action == "adm:find":
        async with S() as s:
            if text.lstrip("@").isdigit():
                u = await s.get(User, int(text.lstrip("@")))
            else:
                u = await s.scalar(select(User).where(func.lower(User.username) == text.lstrip("@").lower()))
        if not u:
            await m.answer("Не найден. Попробуйте ещё раз или нажмите «Отмена».", reply_markup=kb([("⬅️ Отмена", "a:cancel")]))
            return
        await clear_state(aid)
        card, markup = await user_card(u.id)
        await m.answer(card, reply_markup=markup)

    elif action == "adm:bal":
        d = parse_dec(text, signed=True)
        if d is None or d == 0:
            await m.answer("Нужно число со знаком, например +1.4 или -2.")
            return
        uid = int(data)
        async with S() as s:
            u = await s.get(User, uid)
            if not u:
                await m.answer("Пользователь не найден.")
                return
            if u.balance + d < 0:
                await m.answer(f"Баланс не может стать отрицательным (сейчас {fmt_usd(u.balance)}).")
                return
            u.balance += d
            add_ledger(s, uid, d, "manual", "Изменение администратором")
            await set_state(s, aid, "", "")
            await s.commit()
        card, markup = await user_card(uid)
        await m.answer(f"✅ Баланс изменён на {d:+}$.\n\n" + card, reply_markup=markup)

    elif action == "adm:ban":
        uid = int(data)
        async with S() as s:
            u = await s.get(User, uid)
            if not u:
                await m.answer("Пользователь не найден.")
                await set_state(s, aid, "", "")
                await s.commit()
                return
            u.blocked = True
            u.ban_reason = text[:500]
            await set_state(s, aid, "", "")
            await s.commit()
        try:
            await bot.send_message(uid, f"⛔ Вы заблокированы администрацией.\nПричина: {esc(text)}")
        except Exception:
            pass
        card, markup = await user_card(uid)
        await m.answer("⛔ Пользователь заблокирован.\n\n" + card, reply_markup=markup)

    elif action == "adm:msg":
        uid = int(data)
        await clear_state(aid)
        try:
            await bot.send_message(uid, m.text)
            await m.answer(f"✅ Сообщение отправлено ({uid}).", reply_markup=kb([("🔙 К карточке", f"a:view:{uid}")]))
        except Exception:
            await m.answer(f"❌ Не удалось отправить ({uid}) — возможно, он заблокировал бота.")

    elif action == "adm:set":
        key = data
        kind = SETTINGS_META[key][1]
        d = parse_dec(text)
        if d is None or (kind == "pos" and d <= 0):
            await m.answer("Нужно положительное число, например 1.4.")
            return
        if kind == "int":
            if d != d.to_integral_value() or not 0 <= d <= 100:
                await m.answer("Нужно целое число от 0 до 100.")
                return
            value = str(int(d))
        else:
            value = str(d.normalize()) if d != d.to_integral_value() else str(int(d))
        async with S() as s:
            await set_setting(s, key, value)
            await set_state(s, aid, "", "")
            await s.commit()
        await settings_screen(m)

    elif action == "adm:setch":
        n, field = data.split(":")
        n = int(n)
        if field == "title":
            async with S() as s:
                await set_setting(s, f"ch{n}_title", text[:40])
                await set_state(s, aid, "", "")
                await s.commit()
        elif field == "url":
            if not text.startswith(("https://", "http://", "tg://")):
                await m.answer("Ссылка должна начинаться с https://")
                return
            async with S() as s:
                await set_setting(s, f"ch{n}_url", text)
                await set_state(s, aid, "", "")
                await s.commit()
        else:  # id публичного канала
            try:
                chat = await bot.get_chat(text)
                member = await bot.get_chat_member(chat.id, bot.id)
            except Exception as e:
                logging.info("channel check failed: %s", e)
                await m.answer("Не удалось найти канал или бот не добавлен в него. Добавьте бота админом и отправьте ID ещё раз.")
                return
            async with S() as s:
                await set_setting(s, "ch3_id", str(chat.id))
                await set_state(s, aid, "", "")
                await s.commit()
            is_admin = member.status in ("administrator", "creator")
            await m.answer(f"✅ Канал найден: <b>{esc(chat.title)}</b>\n"
                           f"Бот администратор: {'да' if is_admin else '<b>нет</b> — без этого подписка не проверится'}")
        await slot_screen(m, n)

    elif action == "adm:text":
        async with S() as s:
            await set_setting(s, "text:" + data, m.html_text)
            await set_state(s, aid, "", "")
            await s.commit()
        await m.answer(f"✅ Текст «{TEXT_KEYS.get(data, data)}» сохранён.", reply_markup=kb([("📝 К текстам", "a:texts")]))

    elif action == "adm:check":
        wid = int(data)
        if not text.startswith(("http://", "https://")):
            await m.answer("Отправьте ссылку на чек (начинается с https://).")
            return
        async with S() as s:
            w = await s.get(Withdrawal, wid)
            if not w or w.status != "pending":
                await m.answer("Заявка уже обработана.")
                await set_state(s, aid, "", "")
                await s.commit()
                return
            w.status = "paid"
            w.note = text
            w.processed_at = datetime.now(timezone.utc)
            add_ledger(s, w.user_id, 0, "withdraw_paid", f"Заявка #{wid}: чек CryptoBot")
            await set_state(s, aid, "", "")
            await s.commit()
        try:
            await bot.send_message(w.user_id, f"✅ Заявка #{wid} выплачена: <b>{net_text(w)}</b>\n🤖 Ваш чек CryptoBot:\n{esc(text)}")
        except Exception:
            await m.answer("⚠️ Заявка закрыта, но пользователю сообщение не доставлено — перешлите ему чек вручную.")
        await m.answer(f"✅ Заявка #{wid} закрыта, чек отправлен.", reply_markup=kb([("🔙 К выплатам", "a:pay")]))
