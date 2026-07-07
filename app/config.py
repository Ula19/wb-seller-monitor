from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки берутся из переменных окружения / .env (см. .env.example)."""

    bot_token: str
    owner_id: int
    database_url: str = "postgresql+asyncpg://wb:wb@db:5432/wb"

    # параметры запросов к WB
    wb_dest: int = -1257786
    wb_spp: int = 30
    # кука авторизованного аккаунта WB (нужна для b2b-цен). Протухает за дни — обновлять.
    wb_cookie: str = ""
    # прокси к WB (WB режет не-РФ и дата-центровые IP). Один или список через запятую,
    # схемы socks5h:// или http://. На 403 бот переключается на следующий. Пусто = напрямую.
    # Нужен РОССИЙСКИЙ резидентный/мобильный прокси.
    wb_proxies: str = ""
    # заголовки для b2b-эндпоинта __internal (без них WBAAS даёт 403 даже с кукой).
    # ponytail: захвачены из браузера. Если b2b снова начнёт давать 403 — первым делом
    # обнови WB_SPA_VERSION (актуальную видно в заголовке x-spa-version в DevTools);
    # deviceid привязан к браузеру, где минтишь куку, обычно менять не нужно.
    wb_device_id: str = "site_a5d186ace2fe4386952d1652f6a28303"
    wb_spa_version: str = "14.16.0"
    # режим цены по умолчанию для магазинов, добавленных командой /addseller
    # (в UI режим выбирается кнопками). True — бизнес (нужна кука), False — розница.
    wb_b2b: bool = True
    # временный дебаг: логировать сырой JSON первого товара (смотрим все поля API)
    debug_raw: bool = False

    # расписание
    monitor_interval_minutes: int = 10
    # приоритетные магазины (is_fast) опрашиваются отдельным быстрым джобом
    fast_monitor_interval_minutes: int = 1
    big_shop_threshold: int = 100
    # минимальное СНИЖЕНИЕ цены (%) для уведомления. Рост цены не алертим вовсе.
    price_drop_threshold_pct: float = 1.0
    # наша цена = цена ВБ минус этот процент
    our_discount_pct: float = 15.0
    # наценка от личной business-цены (detail с кукой) до витринной «с WB Кошельком».
    # Розница = business_product + этот %. Аккаунт бизнесовый → detail даёт цену ниже
    # витрины; на сайте цена ~2.73% выше. Плоский (SPP аккаунта плоский). 0 = не применять.
    wb_retail_markup_pct: float = 2.73

    # троттлинг (защита от бана по IP). WB банит за ТЕМП, а не за факт парсинга:
    # безопасно ≤~100 запросов/час, паузы 3-7с. Не снижай без нужды — словишь 403.
    request_min_delay: float = 3.0
    request_jitter: float = 4.0
    max_pages: int = 100

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
