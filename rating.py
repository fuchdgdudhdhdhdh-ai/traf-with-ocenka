"""Оценка профиля Telegram (рейтинг 0-100).

Логика перенесена из бота-проверяльщика (traff_ocenka), но работает на данных,
которые доступны обычному боту через Bot API: имя, юзернейм, Premium, описание
профиля и количество аватарок. Данные «был в сети» и «дата смены аватарки»
через Bot API недоступны, поэтому эти два критерия не используются.
"""
import re
import unicodedata
from datetime import datetime, timezone

# Примерная привязка ID аккаунта -> дата регистрации (приблизительно)
ID_DATES = [
    (0, "2013-08"), (100_000_000, "2015-06"), (300_000_000, "2017-01"),
    (600_000_000, "2018-07"), (1_000_000_000, "2019-12"), (1_500_000_000, "2020-09"),
    (2_000_000_000, "2021-04"), (5_000_000_000, "2022-06"), (6_000_000_000, "2023-01"),
    (7_000_000_000, "2024-02"), (7_700_000_000, "2024-12"), (8_300_000_000, "2025-06"),
]
LEET_RE = re.compile(r"[a-z]+[0-9@$]+[a-z]+|[a-z]*[430@$][a-z]+[1370][a-z]*", re.I)

# Штрафы (баллы, которые снимаются)
P = {
    "age_lt1": 15,
    "age_lt2": 8,
    "no_photo": 20,
    "one_photo": 5,
    "digits": 8,
    "random_name": 10,
    "weird_symbols": 6,
    "leet": 8,
    "no_bio": 7,
    "bio_link": 10,
}
PREMIUM_BONUS = 10


def estimate_account_age_years(user_id: int) -> float:
    date_str = ID_DATES[0][1]
    for threshold, d in ID_DATES:
        if user_id >= threshold:
            date_str = d
    created = datetime.strptime(date_str, "%Y-%m").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).days / 365


def looks_random(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    scripts = {(unicodedata.name(c, "X").split() or ["X"])[0] for c in letters}
    if len(scripts) > 1:
        return True
    vowels = sum(c.lower() in "aeiouyаеёиоуыэюя" for c in letters)
    return vowels / len(letters) < 0.2


def has_weird_symbols(text: str) -> bool:
    return any(
        unicodedata.category(c).startswith(("S", "C", "M")) and not c.isspace()
        for c in text
    )


def score_profile(*, user_id, first_name, last_name, username, is_premium,
                  bio, photo_count):
    """Возвращает (рейтинг 0..100, список причин).

    bio / photo_count = None означает «данные получить не удалось» —
    соответствующие критерии пропускаются, чтобы не штрафовать зря.
    """
    score, notes = 100, []

    def penalty(points, text):
        nonlocal score
        score -= points
        notes.append(f"−{points} {text}")

    name = f"{first_name or ''} {last_name or ''}".strip()
    username = username or ""

    age = estimate_account_age_years(user_id)
    if age < 1:
        penalty(P["age_lt1"], "аккаунт младше года")
    elif age < 2:
        penalty(P["age_lt2"], "аккаунту меньше 2 лет")

    if photo_count is not None:
        if photo_count == 0:
            penalty(P["no_photo"], "нет аватарки")
        elif photo_count == 1:
            penalty(P["one_photo"], "всего одна аватарка")

    if re.search(r"\d", name) or re.search(r"\d", username):
        penalty(P["digits"], "цифры в имени или юзернейме")
    if looks_random(name):
        penalty(P["random_name"], "имя выглядит сгенерированным")
    if has_weird_symbols(name):
        penalty(P["weird_symbols"], "странные символы/эмодзи в имени")
    if LEET_RE.search(username):
        penalty(P["leet"], "«leet» в юзернейме")

    if bio is not None:
        bio = bio.strip()
        if not bio:
            penalty(P["no_bio"], "пустое описание профиля")
        elif re.search(r"(https?://|t\.me/|@\w{4,})", bio, re.I):
            penalty(P["bio_link"], "ссылка или упоминание в описании")

    if is_premium:
        score += PREMIUM_BONUS
        notes.append(f"+{PREMIUM_BONUS} Telegram Premium")

    return max(0, min(100, score)), notes


def criteria_text(min_rating: int) -> str:
    return "\n".join([
        "📋 <b>Критерии оценки рейтинга</b>",
        "",
        f"Каждый профиль стартует со 100 баллов. Чтобы пройти проверку, нужно набрать <b>{min_rating} и выше</b>.",
        "",
        "<b>Что снижает рейтинг:</b>",
        f"• Аккаунт младше 1 года — −{P['age_lt1']}",
        f"• Аккаунту от 1 до 2 лет — −{P['age_lt2']}",
        f"• Нет аватарки — −{P['no_photo']}",
        f"• Всего одна аватарка — −{P['one_photo']}",
        f"• Цифры в имени или юзернейме — −{P['digits']}",
        f"• Имя выглядит случайным/сгенерированным — −{P['random_name']}",
        f"• Странные символы или эмодзи в имени — −{P['weird_symbols']}",
        f"• «Leet»-написание в юзернейме (например, a1ex4nder) — −{P['leet']}",
        f"• Пустое описание профиля — −{P['no_bio']}",
        f"• Ссылка или @упоминание в описании — −{P['bio_link']}",
        "",
        "<b>Что повышает:</b>",
        f"• Telegram Premium — +{PREMIUM_BONUS}",
        "",
        "Максимум — 100 баллов.",
        "",
        "💡 Живой профиль — нормальное имя, аватарка (лучше несколько) и заполненное описание без ссылок — набирает 80+ без проблем.",
    ])
