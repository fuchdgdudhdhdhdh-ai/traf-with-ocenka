import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from admin import ar
from config import BOT_TOKEN, PORT
from db import engine, init_db
from monitor import monitor
from support import ar2, sr
from user import ur

logging.basicConfig(level=logging.INFO)


async def health(request):
    return web.Response(text="OK")


async def run():
    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(ar2)  # админские обработчики тикетов/рассылки
    dp.include_router(ar)   # админка раньше пользовательской части
    dp.include_router(sr)   # поддержка (состояния) раньше общего текстового обработчика
    dp.include_router(ur)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    monitor_task = asyncio.create_task(monitor(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Bot started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        monitor_task.cancel()
        await runner.cleanup()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
