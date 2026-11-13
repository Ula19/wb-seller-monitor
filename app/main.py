import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.access import access
from app.bot.handlers import admin, common, menu, user
from app.bot.middlewares import AuthMiddleware
from app.config import settings
from app.db import repo
from app.db.base import Session
from app.services import backup, keeper
from app.services.monitor import monitoring_job, report_job
from app.wb.client import wb_client

log = logging.getLogger(__name__)


async def on_startup() -> None:
    # схему БД готовит Alembic (`alembic upgrade head` в CMD Dockerfile) до старта бота
    # строка владельца в users: на неё ссылаются его источники и аккаунты
    async with Session() as s:
        await repo.ensure_owner(s, settings.owner_id)
        await s.commit()
        saved_guardium = await repo.get_setting(s, "wb_guardium")
    await access.load()
    if saved_guardium:  # актуальный guardium живёт в БД (кнопка «Токены»), .env — стартовый seed
        wb_client.set_guardium(saved_guardium)
    n_px = len(wb_client._proxies)
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
    try:  # имя бота нужно виджету входа на сайте (api в Telegram не ходит — берёт из БД)
        me = await bot.get_me()
        async with Session() as s:
            await repo.set_setting(s, repo.BOT_USERNAME_KEY, me.username)
            await s.commit()
    except Exception as e:
        log.warning("имя бота для сайта не сохранено: %s", e)
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
    scheduler.add_job(backup.backup_job, "cron", hour=settings.backup_hour, minute=30,
                      args=[bot], max_instances=1)
    scheduler.add_job(  # освежаем веб-сессию (куки бизнес-цен) + визит: код получения (меняется в 00:00) и баланс
        keeper.refresh_job, "cron", hour=f"*/{settings.web_refresh_hours}", minute=5,  # 12 → 00:05 и 12:05 МСК
        args=[bot], max_instances=1,
    )
    scheduler.add_job(  # история заказов (полученные) — раз в сутки; новая покупка — по событию из опроса аккаунта
        keeper.purchases_job, "cron", hour=4, minute=30, args=[bot], max_instances=1,
    )
    scheduler.add_job(  # карта хостов картинок товаров для сайта
        keeper.img_hosts_job, "interval", days=1, max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    scheduler.add_job(  # тик опроса аккаунтов: каждому — типы, которым пора (Настройки); 401 → рефреш → «войдите заново»
        keeper.state_job, "interval", minutes=1, args=[bot], max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    scheduler.start()
    login_task = asyncio.create_task(keeper.login_requests_loop(bot))  # вход в WB по заявкам сайта
    proxy_task = asyncio.create_task(keeper.proxy_checks_loop())  # «Проверить» прокси с сайта
    poll_task = asyncio.create_task(keeper.account_polls_loop(bot))  # ↻ «Обновить» аккаунт с сайта
    sync_task = asyncio.create_task(keeper.browser_syncs_loop(bot))  # визит в браузере по кнопке с сайта
    cert_task = asyncio.create_task(keeper.certificates_loop())  # активация сертификатов с сайта
    bell_task = asyncio.create_task(keeper.notices_check_loop())  # «Проверить сейчас» уведомления с сайта

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        login_task.cancel()
        proxy_task.cancel()
        poll_task.cancel()
        sync_task.cancel()
        cert_task.cancel()
        bell_task.cancel()
        await wb_client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
