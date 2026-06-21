import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.access import access
from app.bot.handlers import admin, common, menu, user
from app.bot.middlewares import AuthMiddleware
from app.config import settings
from app.db.base import Base, engine
from app.services.monitor import monitoring_job, report_job
from app.wb.client import wb_client

log = logging.getLogger(__name__)


async def on_startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await access.load()
    log.info("Старт: владелец %s, разрешённых пользователей %d",
             settings.owner_id, len(access.allowed))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await on_startup()

    bot = Bot(settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AuthMiddleware())
    dp.callback_query.outer_middleware(AuthMiddleware())
    dp.include_router(menu.router)
    dp.include_router(admin.router)
    dp.include_router(user.router)
    dp.include_router(common.router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        monitoring_job,
        "interval",
        minutes=settings.monitor_interval_minutes,
        args=[bot],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(report_job, "cron", minute=0, args=[bot], max_instances=1)
    scheduler.start()

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await wb_client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
