import html
from decimal import ROUND_HALF_UP, Decimal

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

CENT = Decimal("0.01")
METHOD_LABELS = {"card": "💳 Карта", "sbp": "📱 СБП", "crypto": "🤖 Чек CryptoBot"}


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def kb(*rows):
    """rows: списки кортежей (текст, callback_data | url)."""
    keyboard = []
    for row in rows:
        buttons = []
        for text, data in row:
            if data.startswith(("http://", "https://", "tg://")):
                buttons.append(InlineKeyboardButton(text=text, url=data))
            else:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        keyboard.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show(target, text, markup=None):
    """Показывает экран: для callback редактирует сообщение, для message отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except Exception:
            pass
        msg = target.message
        if msg is None:
            return await target.bot.send_message(target.from_user.id, text, reply_markup=markup)
        try:
            return await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return msg
        except Exception:
            pass
        try:
            await msg.delete()
        except Exception:
            pass
        return await target.bot.send_message(msg.chat.id, text, reply_markup=markup)
    return await target.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- деньги
def q2(d) -> Decimal:
    return Decimal(d).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_usd(d) -> str:
    return f"{q2(d)}$"


def fmt_rub(x) -> str:
    s = f"{q2(x):.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return f"{s} ₽"


def fmt_money(d, rate) -> str:
    """2.80$ (224 ₽)"""
    return f"{fmt_usd(d)} ({fmt_rub(Decimal(d) * Decimal(rate))})"


def net_text(w) -> str:
    return fmt_rub(w.net) if w.currency == "RUB" else f"{q2(w.net)} USDT"


def ref_status(u, credited: bool) -> str:
    if credited:
        return "✅ засчитан"
    if u.rating is None:
        return "⏳ проверка не завершена"
    if not u.passed:
        return f"❌ рейтинг {u.rating}"
    if not u.subscribed:
        return "⏳ не подписался"
    return "—"
