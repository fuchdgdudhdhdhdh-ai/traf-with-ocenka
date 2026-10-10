"""Автоматическая выдача чеков через Crypto Pay API (@CryptoBot).

Включается, если задана переменная окружения CRYPTO_PAY_TOKEN
(токен из @CryptoBot → Crypto Pay → Create App).
Для тестовой сети добавьте CRYPTO_PAY_TESTNET=1.
"""
import logging
import os

import aiohttp

TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "")
BASE = ("https://testnet-pay.crypt.bot/api/" if os.environ.get("CRYPTO_PAY_TESTNET")
        else "https://pay.crypt.bot/api/")


async def create_check(amount, user_id: int):
    """Возвращает (ссылка на чек, None) или (None, текст ошибки).

    Чек привязывается к Telegram ID получателя — активировать его сможет только он.
    """
    payload = {"asset": "USDT", "amount": str(amount), "pin_to_user_id": int(user_id)}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.post(BASE + "createCheck", json=payload,
                                 headers={"Crypto-Pay-API-Token": TOKEN}) as resp:
                data = await resp.json()
    except Exception as e:
        logging.warning("createCheck failed: %s", e)
        return None, "нет связи с Crypto Pay"
    if data.get("ok") and data.get("result", {}).get("bot_check_url"):
        return data["result"]["bot_check_url"], None
    err = data.get("error")
    err = err.get("name") if isinstance(err, dict) else str(err)
    logging.warning("createCheck error: %s", err)
    return None, err or "неизвестная ошибка"
