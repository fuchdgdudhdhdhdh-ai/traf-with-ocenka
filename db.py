from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, DateTime, Integer, Numeric, String, Text,
                        select)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import DATABASE_URL


def _normalize(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


_URL = _normalize(DATABASE_URL)
if not _URL.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
    raise RuntimeError("DATABASE_URL must be a PostgreSQL URL.")

engine = create_async_engine(_URL, pool_pre_ping=True)
S = async_sessionmaker(engine, expire_on_commit=False)


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    captcha_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_notes: Mapped[str] = mapped_column(Text, default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)       # рейтинг >= порога
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False)   # подписка на публичный канал
    rules_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reward(Base):
    __tablename__ = "rewards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger)
    referral_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default="credited")   # credited / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    kind: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))        # списано, $
    method: Mapped[str] = mapped_column(String(20))                # card / sbp / crypto
    destination: Mapped[str] = mapped_column(Text)
    net: Mapped[Decimal] = mapped_column(Numeric(12, 2))           # к получению
    currency: Mapped[str] = mapped_column(String(8))               # RUB / USDT
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 2))          # курс на момент заявки
    status: Mapped[str] = mapped_column(String(20), default="pending")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TicketMsg(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(Integer)
    sender_id: Mapped[int] = mapped_column(BigInteger)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class State(Base):
    __tablename__ = "states"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    action: Mapped[str] = mapped_column(String(50), default="")
    data: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- настройки
CHANNEL_SLOTS = [1, 2, 3, 4, 5]
PUBLIC_SLOT = 3   # третий канал — публичный, только он проверяется на подписку

DEFAULTS = {
    "min_rating": "80",
    "reward_usd": "1.4",
    "usd_rub": "80",
    "min_withdraw_usd": "1.25",
    "card_fee_rub": "45",
    "bot_status": "open",
    "payout_status": "open",
    "clawback": "closed",        # списание награды при отписке реферала (open = включено)
    "clawback_days": "7",
    "ch3_id": "",
    "text:menu": "🏠 <b>Главное меню</b>",
    "text:info": "Информация пока не добавлена.",
    "text:faq": "FAQ пока не заполнен.",
    "text:criteria": "",   # пусто = генерируется автоматически из rating.py
    "text:rules": (
        "📜 <b>Правила работы</b>\n\n"
        "1️⃣ Приглашайте только реальных людей с рейтингом профиля <b>{min_rating}+</b>.\n"
        "2️⃣ Запрещено накручивать ботов, фейки и мультиаккаунты.\n"
        "3️⃣ Награда <b>{reward}</b> начисляется за каждого приглашённого, который прошёл проверку рейтинга и подписался на каналы.\n"
        "4️⃣ Запрещено вводить приглашаемых в заблуждение и спамить.\n"
        "5️⃣ При подозрении на нарушение администрация вправе отказать в выплате."
    ),
    "text:rules_warn": (
        "⚠️ <b>ВНИМАНИЕ! Прочитайте правила ещё раз — очень внимательно.</b>\n\n"
        "За любое нарушение правил аккаунт будет заблокирован (<b>БАН</b>).\n\n"
        "Нажимая кнопку ниже, вы подтверждаете, что прочитали и поняли правила."
    ),
}
for _n in CHANNEL_SLOTS:
    DEFAULTS[f"ch{_n}_title"] = f"Канал {_n}"
    DEFAULTS[f"ch{_n}_url"] = ""


async def get_setting(s, key: str) -> str:
    x = await s.get(Setting, key)
    return x.value if x is not None else DEFAULTS.get(key, "")


async def set_setting(s, key: str, value: str):
    x = await s.get(Setting, key)
    if x is not None:
        x.value = value
    else:
        s.add(Setting(key=key, value=value))


async def del_setting(s, key: str):
    x = await s.get(Setting, key)
    if x is not None:
        await s.delete(x)


async def get_decimal(s, key: str) -> Decimal:
    return Decimal(await get_setting(s, key) or "0")


async def get_int(s, key: str) -> int:
    return int(await get_setting(s, key) or "0")


# ---------------------------------------------------------------- состояния
async def set_state(s, uid: int, action: str = "", data: str = ""):
    x = await s.get(State, uid)
    if x is None:
        x = State(user_id=uid)
        s.add(x)
    x.action = action
    x.data = data


async def get_state(s, uid: int) -> tuple[str, str]:
    x = await s.get(State, uid)
    return (x.action or "", x.data or "") if x else ("", "")


def add_ledger(s, uid: int, amount, kind: str, description: str):
    s.add(Ledger(user_id=uid, amount=amount, kind=kind, description=description))


# ---------------------------------------------------------------- бизнес-логика
async def credit_referral(s, u: User):
    """Мгновенно начисляет награду пригласившему (без холда).

    Возвращает (referrer_id, amount) или None, если начислять нечего.
    """
    if not u.referrer_id:
        return None
    ref = await s.get(User, u.referrer_id)
    if ref is None or ref.blocked:
        return None
    if await s.scalar(select(Reward).where(Reward.referral_id == u.id)):
        return None
    amount = await get_decimal(s, "reward_usd")
    s.add(Reward(referrer_id=ref.id, referral_id=u.id, amount=amount))
    ref.balance = (ref.balance or Decimal("0")) + amount
    add_ledger(s, ref.id, amount, "referral", f"Реферал {u.id}")
    return ref.id, amount


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await conn.exec_driver_sql(
                "ALTER TABLE rewards ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'credited'")
