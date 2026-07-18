import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from app.bot.access import access
from app.bot.handlers import admin, common, menu, user
from app.bot.middlewares import AuthMiddleware
from app.config import settings
from app.db import repo
from app.db.base import Base, Session, engine
from app.services.monitor import monitoring_job, report_job
from app.wb.client import wb_client

log = logging.getLogger(__name__)


async def on_startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all не добавляет колонки в существующую таблицу — добавляем вручную
        await conn.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS delivery_hours INTEGER"
        ))
        await conn.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS from_seller BOOLEAN"
        ))
        await conn.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS subject_id INTEGER"
        ))
        await conn.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS shelf_price INTEGER"
        ))
        # is_fast упразднён: все магазины мониторятся ежеминутно единым джобом
        await conn.execute(text(
            "ALTER TABLE sellers DROP COLUMN IF EXISTS is_fast"
        ))
        await conn.execute(text(
            "ALTER TABLE sellers ADD COLUMN IF NOT EXISTS b2b BOOLEAN DEFAULT TRUE"
        ))
    await access.load()
    # актуальная кука живёт в БД (обновляется из Telegram); .env — лишь стартовый seed
    async with Session() as s:
        saved = await repo.get_setting(s, "wb_cookie")
    if saved:
        await wb_client.set_cookie(saved)
    n_px = len(wb_client._proxies) + len(wb_client._dc_proxies)
    proxy_state = f"вкл ({n_px} шт.)" if n_px else "выкл (напрямую)"
    log.info("Старт: владелец %s, разрешённых пользователей %d, прокси WB: %s",
             settings.owner_id, len(access.allowed), proxy_state)


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
    scheduler.add_job(  # единый джоб: ВСЕ магазины каждые monitor_interval_seconds
        monitoring_job,
        "interval",
        seconds=settings.monitor_interval_seconds,
        args=[bot],
        max_instances=1,  # проход дольше интервала → следующий запуск коалесится
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
