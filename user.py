import logging
import random
import re
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import rating
from config import ADMIN_IDS
from db import (CHANNEL_SLOTS, S, Reward, User, Withdrawal, add_ledger, credit_referral,
                get_decimal, get_int, get_setting, get_state, set_state)
from ui import (METHOD_LABELS, esc, fmt_money, fmt_rub, fmt_usd, kb, net_text, q2,
                ref_status, show)

ur = Router()

SBP_BANKS = [("sber", "Сбербанк"), ("tbank", "Т-Банк"), ("vtb", "ВТБ"), ("alfa", "Альфа-Банк"),
             ("gazprom", "Газпромбанк"), ("raif", "Райффайзен"), ("ozon", "Озон Банк"),
             ("other", "Другой (ввести вручную)")]
OPEN_WHEN_CLOSED = {"home", "bal", "refs", "wd"}


# ================================================================ вспомогательное
async def render_text(s, key: str) -> str:
    """Текст из настроек с подстановками {min_rating}, {reward}, {rate}."""
    text = await get_setting(s, "text:" + key)
    rate = await get_decimal(s, "usd_rub")
    return (text.replace("{min_rating}", str(await get_int(s, "min_rating")))
                .replace("{reward}", fmt_money(await get_decimal(s, "reward_usd"), rate))
                .replace("{rate}", fmt_rub(rate)))


async def is_member(bot: Bot, chat, uid: int) -> bool:
    try:
        member = await bot.get_chat_member(chat, uid)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logging.warning("get_chat_member failed for %s: %s", chat, e)
        return False


async def notify_admins(bot: Bot, text: str, markup=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=markup)
        except Exception:
            pass


def parse_amount(text: str):
    try:
        d = Decimal(text.strip().replace(",", ".").replace("$", ""))
    except InvalidOperation:
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


# ================================================================ маршрутизация
async def route(target, bot: Bot, tg_user):
    """Определяет, какой экран показать пользователю дальше."""
    async with S() as s:
        u = await s.get(User, tg_user.id)
    if u is None:
        return
    if u.blocked:
        reason = f"\nПричина: {esc(u.ban_reason)}" if u.ban_reason else ""
        return await show(target, "⛔ Доступ заблокирован." + reason)
    if not u.captcha_ok:
        return await send_captcha(target)
    if u.rating is None:
        return await run_check(target, bot, tg_user)
    if u.passed and not u.subscribed:
        return await show_channels(target, u)
    if not u.rules_ok:
        if u.passed:
            return await show_rules(target, 1)
        return await show_fail(target, u)
    return await show_menu(target)


@ur.message(CommandStart())
async def cmd_start(m: Message, bot: Bot):
    parts = (m.text or "").split()
    ref = None
    if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
        ref = int(parts[1][4:])
    async with S() as s:
        u = await s.get(User, m.from_user.id)
        if u is None:
            if (await get_setting(s, "bot_status")) == "closed":
                await m.answer("🛑 Бот временно приостановил работу. Зайдите позже.")
                return
            referrer = None
            if ref and ref != m.from_user.id and await s.get(User, ref):
                referrer = ref
            s.add(User(id=m.from_user.id, username=m.from_user.username,
                       first_name=m.from_user.first_name, referrer_id=referrer))
        else:
            u.username = m.from_user.username
            u.first_name = m.from_user.first_name
        await s.commit()
    await route(m, bot, m.from_user)


@ur.callback_query(F.data == "route")
async def cb_route(c: CallbackQuery, bot: Bot):
    await route(c, bot, c.from_user)


# ================================================================ капча
async def send_captcha(target):
    word, ok, other = random.choice([
        ("солнце", "☀️", ["🌙", "🍎"]),
        ("яблоко", "🍎", ["🐟", "🚗"]),
        ("рыба", "🐟", ["🌳", "⭐"]),
    ])
    options = [ok, *other]
    random.shuffle(options)
    await show(target, f"🧩 Выберите эмодзи для слова <b>{word}</b>",
               kb([(x, f"cap:{ok}:{x}") for x in options]))


@ur.callback_query(F.data.startswith("cap:"))
async def cb_captcha(c: CallbackQuery, bot: Bot):
    _, ok, x = c.data.split(":")
    if x != ok:
        await c.answer("Неверно.", show_alert=True)
        return
    async with S() as s:
        u = await s.get(User, c.from_user.id)
        if u is None:
            await c.answer("Отправьте /start", show_alert=True)
            return
        u.captcha_ok = True
        await s.commit()
    await route(c, bot, c.from_user)


# ================================================================ проверка рейтинга
async def run_check(target, bot: Bot, tg_user):
    bio = None
    try:
        bio = (await bot.get_chat(tg_user.id)).bio or ""
    except Exception as e:
        logging.info("bio unavailable for %s: %s", tg_user.id, e)
    photos = None
    try:
        photos = (await bot.get_user_profile_photos(tg_user.id, limit=1)).total_count
    except Exception as e:
        logging.info("photos unavailable for %s: %s", tg_user.id, e)

    score, notes = rating.score_profile(
        user_id=tg_user.id, first_name=tg_user.first_name, last_name=tg_user.last_name,
        username=tg_user.username, is_premium=bool(getattr(tg_user, "is_premium", False)),
        bio=bio, photo_count=photos)

    async with S() as s:
        min_r = await get_int(s, "min_rating")
        u = await s.get(User, tg_user.id)
        u.rating = score
        u.rating_notes = "\n".join(notes)
        u.passed = score >= min_r
        u.checked_at = datetime.now(timezone.utc)
        await s.commit()
    await route(target, bot, tg_user)


async def show_fail(target, u: User):
    async with S() as s:
        min_r = await get_int(s, "min_rating")
    lines = [
        "😔 <b>Проверка не пройдена</b>",
        "",
        f"Ваш рейтинг: <b>{u.rating}/100</b> (нужно {min_r}+).",
    ]
    if u.referrer_id:
        lines += ["", "Тому, кто вас пригласил, награда <b>не будет начислена</b> — вы не проходите проверку."]
    if u.rating_notes:
        lines += ["", "<b>Что снизило рейтинг:</b>"] + [f"• {esc(n)}" for n in u.rating_notes.split("\n")]
    lines += ["", f"Вы можете перейти к боту и сами приглашать людей с рейтингом <b>{min_r}+</b>."]
    await show(target, "\n".join(lines),
               kb([("🔄 Проверить заново", "rating_retry")],
                  [("📋 Критерии оценки", "criteria:route")],
                  [("🤖 Перейти к боту", "gobot")]))


@ur.callback_query(F.data == "rating_retry")
async def cb_rating_retry(c: CallbackQuery, bot: Bot):
    """Повторно оценивает профиль пользователя, ранее не прошедшего рейтинг."""
    async with S() as s:
        u = await s.get(User, c.from_user.id)
        if u is None or u.blocked or not u.captcha_ok:
            await c.answer("Сначала отправьте /start.", show_alert=True)
            return
        if u.passed:
            await c.answer("Вы уже прошли проверку.", show_alert=True)
            await route(c, bot, c.from_user)
            return
    await run_check(c, bot, c.from_user)


async def show_channels(target, u: User):
    rows = []
    async with S() as s:
        for n in CHANNEL_SLOTS:
            url = await get_setting(s, f"ch{n}_url")
            if url:
                title = await get_setting(s, f"ch{n}_title")
                rows.append([(f"📢 {title}", url)])
    rows.append([("✅ Проверить подписку", "check")])
    await show(target,
               f"✅ <b>Проверка пройдена!</b> Ваш рейтинг: <b>{u.rating}/100</b>\n\n"
               "Подпишитесь на все каналы ниже, затем нажмите «✅ Проверить подписку».",
               kb(*rows))


@ur.callback_query(F.data == "check")
async def cb_check(c: CallbackQuery, bot: Bot):
    async with S() as s:
        u = await s.get(User, c.from_user.id)
        ch_id = await get_setting(s, "ch3_id")
    if u is None or u.blocked or not u.captcha_ok or not u.passed:
        await c.answer("Отправьте /start", show_alert=True)
        return
    # Проверяется только публичный канал (№3). Частные каналы не проверяются.
    if ch_id and not await is_member(bot, ch_id, c.from_user.id):
        await c.answer("Вы ещё не подписались на все каналы.", show_alert=True)
        return
    credited = None
    try:
        async with S() as s:
            u = await s.get(User, c.from_user.id)
            first = not u.subscribed
            u.subscribed = True
            if first:
                credited = await credit_referral(s, u)
            await s.commit()
    except IntegrityError:
        credited = None   # двойное нажатие — награда уже начислена
    if credited:
        ref_id, amount = credited
        async with S() as s:
            rate = await get_decimal(s, "usd_rub")
        try:
            await bot.send_message(
                ref_id, f"🎉 Ваш реферал прошёл проверку! Начислено: <b>+{fmt_money(amount, rate)}</b>")
        except Exception:
            pass
    await route(c, bot, c.from_user)


# ================================================================ правила
async def show_rules(target, step: int):
    async with S() as s:
        text = await render_text(s, "rules" if step == 1 else "rules_warn")
    if step == 1:
        markup = kb([("➡️ Далее", "rules_next")])
    else:
        markup = kb([("✅ Прочитал(а) и согласен(на)", "rules_ok")])
    await show(target, text, markup)


async def can_enter(c: CallbackQuery) -> User | None:
    """Пользователь прошёл проверку (или не прошёл, но имеет право зайти в бота)."""
    async with S() as s:
        u = await s.get(User, c.from_user.id)
    if u is None or u.blocked or not u.captcha_ok or u.rating is None:
        return None
    if u.passed and not u.subscribed:
        return None
    return u


@ur.callback_query(F.data == "gobot")
async def cb_gobot(c: CallbackQuery, bot: Bot):
    if not await can_enter(c):
        return await route(c, bot, c.from_user)
    await show_rules(c, 1)


@ur.callback_query(F.data == "rules_next")
async def cb_rules_next(c: CallbackQuery, bot: Bot):
    if not await can_enter(c):
        return await route(c, bot, c.from_user)
    await c.answer("⚠️ Внимательно прочитайте ещё раз! За нарушение правил — БАН.", show_alert=True)
    await show_rules(c, 2)


@ur.callback_query(F.data == "rules_ok")
async def cb_rules_ok(c: CallbackQuery, bot: Bot):
    if not await can_enter(c):
        return await route(c, bot, c.from_user)
    async with S() as s:
        u = await s.get(User, c.from_user.id)
        u.rules_ok = True
        await s.commit()
    await show_menu(c)


@ur.callback_query(F.data == "rules_view")
async def cb_rules_view(c: CallbackQuery):
    if not await access(c):
        return
    async with S() as s:
        text = await render_text(s, "rules")
    await show(c, text, kb([("⬅️ Назад", "home")]))


# ================================================================ доступ и меню
async def access(c: CallbackQuery) -> bool:
    async with S() as s:
        u = await s.get(User, c.from_user.id)
        closed = (await get_setting(s, "bot_status")) == "closed"
    if u is None or u.blocked or not u.rules_ok:
        await c.answer("Сначала пройдите проверку — отправьте /start", show_alert=True)
        return False
    data = c.data or ""
    if closed and data not in OPEN_WHEN_CLOSED and not data.startswith(("wd:", "wdbank:")):
        await c.answer("🛑 Бот временно приостановлен. Доступны: Баланс, Мой трафик, Выплаты.",
                       show_alert=True)
        return False
    return True


async def show_menu(target):
    async with S() as s:
        title = await render_text(s, "menu")
        min_r = await get_int(s, "min_rating")
        closed = (await get_setting(s, "bot_status")) == "closed"
    if closed:
        title = "🛑 <b>Бот временно приостановлен</b>\n\n" + title
        markup = kb([("💰 Баланс", "bal"), ("📊 Мой трафик", "refs")], [("💸 Выплаты", "wd")])
    else:
        title += f"\n\n🔗 Приглашайте только людей с рейтингом <b>{min_r}+</b>."
        markup = kb(
            [("💰 Баланс", "bal"), ("📊 Мой трафик", "refs")],
            [("🔗 Моя ссылка", "link"), ("💸 Выплаты", "wd")],
            [("📋 Критерии рейтинга", "criteria:home"), ("📜 Правила", "rules_view")],
            [("ℹ️ Информация", "info"), ("❓ FAQ", "faq")],
            [("🆘 Поддержка", "sup")],
        )
    await show(target, title, markup)


@ur.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    if await access(c):
        await show_menu(c)


@ur.callback_query(F.data.startswith("criteria:"))
async def cb_criteria(c: CallbackQuery):
    back = "home" if c.data.endswith("home") else "route"
    async with S() as s:
        custom = await get_setting(s, "text:criteria")
        min_r = await get_int(s, "min_rating")
    await show(c, custom or rating.criteria_text(min_r), kb([("⬅️ Назад", back)]))


@ur.callback_query(F.data.in_({"info", "faq"}))
async def cb_content(c: CallbackQuery):
    if not await access(c):
        return
    async with S() as s:
        text = await render_text(s, c.data)
    await show(c, text, kb([("⬅️ Назад", "home")]))


# ================================================================ баланс / трафик / ссылка
@ur.callback_query(F.data == "bal")
async def cb_balance(c: CallbackQuery):
    if not await access(c):
        return
    async with S() as s:
        u = await s.get(User, c.from_user.id)
        rate = await get_decimal(s, "usd_rub")
        reward = await get_decimal(s, "reward_usd")
        credited = await s.scalar(
            select(func.count()).select_from(Reward)
            .where(Reward.referrer_id == u.id, Reward.status == "credited")) or 0
    await show(c,
               f"💰 Баланс: <b>{fmt_money(u.balance, rate)}</b>\n\n"
               f"Курс: 1$ = {fmt_rub(rate)}\n"
               f"За реферала: {fmt_money(reward, rate)}\n"
               f"Засчитано рефералов: {credited}",
               kb([("💸 Вывести", "wd")], [("⬅️ Назад", "home")]))


@ur.callback_query(F.data == "refs")
async def cb_refs(c: CallbackQuery):
    if not await access(c):
        return
    async with S() as s:
        uid = c.from_user.id
        rows = (await s.scalars(select(User).where(User.referrer_id == uid)
                                .order_by(User.created_at.desc()).limit(30))).all()
        total = await s.scalar(select(func.count()).select_from(User)
                               .where(User.referrer_id == uid)) or 0
        credited_ids = set((await s.scalars(
            select(Reward.referral_id).where(Reward.referrer_id == uid, Reward.status == "credited"))).all())
        earned = await s.scalar(select(func.coalesce(func.sum(Reward.amount), 0))
                                .where(Reward.referrer_id == uid, Reward.status == "credited")) or 0
        rate = await get_decimal(s, "usd_rub")
    lines = []
    for x in rows:
        name = f"@{esc(x.username)}" if x.username else f"ID {x.id}"
        lines.append(f"• {name} — {ref_status(x, x.id in credited_ids)}")
    if total > len(rows):
        lines.append(f"…и ещё {total - len(rows)}")
    body = "\n".join(lines) or "Пока нет рефералов."
    await show(c,
               f"📊 <b>Мой трафик</b>\nПриглашено: {total}, засчитано: {len(credited_ids)}\n"
               f"Заработано: {fmt_money(earned, rate)}\n\n{body}",
               kb([("⬅️ Назад", "home")]))


@ur.callback_query(F.data == "link")
async def cb_link(c: CallbackQuery, bot: Bot):
    if not await access(c):
        return
    me = await bot.get_me()
    async with S() as s:
        min_r = await get_int(s, "min_rating")
        reward = await get_decimal(s, "reward_usd")
        rate = await get_decimal(s, "usd_rub")
    await show(c,
               "🔗 <b>Ваша ссылка для приглашения:</b>\n"
               f"https://t.me/{me.username}?start=ref_{c.from_user.id}\n\n"
               f"⚠️ Приглашайте людей с рейтингом профиля <b>{min_r}+</b>. "
               f"Награда {fmt_money(reward, rate)} начисляется только за тех, "
               "кто прошёл проверку и подписался на каналы.",
               kb([("📋 Критерии рейтинга", "criteria:home")], [("⬅️ Назад", "home")]))


# ================================================================ вывод средств
def bank_kb():
    return kb(*[[(name, f"wdbank:{code}")] for code, name in SBP_BANKS])


@ur.callback_query(F.data == "wd")
async def cb_wd(c: CallbackQuery):
    if not await access(c):
        return
    async with S() as s:
        if (await get_setting(s, "payout_status")) == "closed":
            await c.answer("💸 Выплаты временно приостановлены. Попробуйте позже.", show_alert=True)
            return
        u = await s.get(User, c.from_user.id)
        rate = await get_decimal(s, "usd_rub")
        min_w = await get_decimal(s, "min_withdraw_usd")
        fee = await get_decimal(s, "card_fee_rub")
    await show(c,
               "💸 <b>Вывод средств</b>\n\n"
               f"Баланс: <b>{fmt_money(u.balance, rate)}</b>\n"
               f"Минимум: {fmt_money(min_w, rate)}\n\n"
               f"💳 Карта — комиссия {fmt_rub(fee)}\n"
               "📱 СБП — без комиссии\n"
               "🤖 Чек CryptoBot — без комиссии, в USDT (1$ = 1 USDT)\n\n"
               "Выберите способ:",
               kb([("💳 Карта", "wd:card"), ("📱 СБП", "wd:sbp")],
                  [("🤖 Чек CryptoBot", "wd:crypto")],
                  [("⬅️ Назад", "home")]))


@ur.callback_query(F.data.startswith("wd:"))
async def cb_wd_method(c: CallbackQuery):
    if not await access(c):
        return
    method = c.data.split(":")[1]
    if method not in METHOD_LABELS:
        return
    async with S() as s:
        await set_state(s, c.from_user.id, "wd_amount", method)
        u = await s.get(User, c.from_user.id)
        rate = await get_decimal(s, "usd_rub")
        min_w = await get_decimal(s, "min_withdraw_usd")
        fee = await get_decimal(s, "card_fee_rub")
        await s.commit()
    hint = {
        "card": f"Комиссия {fmt_rub(fee)} будет вычтена из суммы к получению.",
        "sbp": "Без комиссии — вся сумма поступит на счёт.",
        "crypto": "Без комиссии. Чек придёт сообщением от бота.",
    }[method]
    await show(c,
               f"Введите сумму вывода в <b>$</b> (минимум {fmt_usd(min_w)}, доступно {fmt_usd(u.balance)}).\n{hint}",
               kb([("⬅️ Отмена", "home")]))


async def finalize_withdrawal(bot: Bot, uid: int, amount: Decimal, method: str, dest: str):
    """Создаёт заявку и списывает баланс. Возвращает текст ответа пользователю."""
    async with S() as s:
        u = await s.get(User, uid)
        if u.balance < amount:
            await set_state(s, uid, "", "")
            await s.commit()
            return "Недостаточно средств."
        rate = await get_decimal(s, "usd_rub")
        fee = await get_decimal(s, "card_fee_rub")
        if method == "card":
            net, cur = q2(amount * rate - fee), "RUB"
        elif method == "sbp":
            net, cur = q2(amount * rate), "RUB"
        else:
            net, cur = q2(amount), "USDT"
        if net <= 0:
            return "Сумма слишком мала для этого способа."
        u.balance -= amount
        w = Withdrawal(user_id=uid, amount=amount, method=method, destination=dest,
                       net=net, currency=cur, rate=rate)
        s.add(w)
        await s.flush()
        add_ledger(s, uid, -amount, "withdraw_hold", f"Заявка #{w.id}")
        await set_state(s, uid, "", "")
        await s.commit()
    await notify_admins(
        bot,
        f"💸 <b>Новая заявка #{w.id}</b>\n{fmt_usd(amount)} → {net_text(w)} · {METHOD_LABELS[method]}",
        kb([("📄 Открыть", f"a:payview:{w.id}")]))
    return (f"✅ Заявка #{w.id} создана.\nСписано: {fmt_usd(amount)}\n"
            f"К получению: <b>{net_text(w)}</b>\nЗаявки обрабатываются администратором вручную.")


@ur.callback_query(F.data.startswith("wdbank:"))
async def cb_wd_bank(c: CallbackQuery, bot: Bot):
    if not await access(c):
        return
    code = c.data.split(":")[1]
    async with S() as s:
        action, data = await get_state(s, c.from_user.id)
        if action != "wd_bank":
            await c.answer("Сессия истекла, начните заново.", show_alert=True)
            return
        if code == "other":
            await set_state(s, c.from_user.id, "wd_bank_custom", data)
            await s.commit()
            await show(c, "Введите название банка вручную.")
            return
    amount, phone = data.split("|", 1)
    bank = dict(SBP_BANKS).get(code, code)
    text = await finalize_withdrawal(bot, c.from_user.id, Decimal(amount), "sbp", f"{phone} ({bank})")
    await show(c, text, kb([("⬅️ В меню", "home")]))


# ================================================================ текстовые сообщения (состояния)
@ur.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot):
    async with S() as s:
        action, data = await get_state(s, m.from_user.id)
        u = await s.get(User, m.from_user.id)
    if u is None:
        await m.answer("Отправьте /start, чтобы начать.")
        return

    if action == "wd_amount":
        method = data
        amount = parse_amount(m.text)
        if amount is None:
            await m.answer("Введите число, например 5 или 2.5")
            return
        async with S() as s:
            min_w = await get_decimal(s, "min_withdraw_usd")
            rate = await get_decimal(s, "usd_rub")
            fee = await get_decimal(s, "card_fee_rub")
        if amount < min_w:
            await m.answer(f"Минимальная сумма вывода — {fmt_usd(min_w)}.")
            return
        if amount > u.balance:
            await m.answer(f"Недостаточно средств. Доступно: {fmt_usd(u.balance)}.")
            return
        if method == "card" and amount * rate <= fee:
            await m.answer(f"Сумма должна быть больше комиссии ({fmt_rub(fee)}).")
            return
        if method == "crypto":
            await m.answer(await finalize_withdrawal(bot, u.id, amount, "crypto", "CryptoBot check"),
                           reply_markup=kb([("⬅️ В меню", "home")]))
            return
        async with S() as s:
            await set_state(s, u.id, "wd_dest", f"{method}|{amount}")
            await s.commit()
        await m.answer("Введите номер карты." if method == "card"
                       else "Введите номер телефона для СБП.")
        return

    if action == "wd_dest":
        method, amount = data.split("|")
        digits = re.sub(r"\D", "", m.text)
        if method == "card":
            if not 12 <= len(digits) <= 19:
                await m.answer("Введите номер карты цифрами.")
                return
            await m.answer(await finalize_withdrawal(bot, u.id, Decimal(amount), "card", digits),
                           reply_markup=kb([("⬅️ В меню", "home")]))
            return
        if not 10 <= len(digits) <= 15:
            await m.answer("Введите номер телефона, например +79001234567.")
            return
        async with S() as s:
            await set_state(s, u.id, "wd_bank", f"{amount}|+{digits}")
            await s.commit()
        await m.answer("Выберите банк получателя:", reply_markup=bank_kb())
        return

    if action == "wd_bank_custom":
        amount, phone = data.split("|", 1)
        bank = m.text.strip()[:60]
        await m.answer(await finalize_withdrawal(bot, u.id, Decimal(amount), "sbp", f"{phone} ({bank})"),
                       reply_markup=kb([("⬅️ В меню", "home")]))
        return

    await m.answer("Используйте кнопки меню. Если меню нет — отправьте /start.")
