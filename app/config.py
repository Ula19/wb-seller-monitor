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
    # минимальное изменение цены (%) для уведомления
    price_change_threshold_pct: float = 5.0
    # наша цена = цена ВБ минус этот процент
    our_discount_pct: float = 15.0

    # троттлинг (защита от бана по IP). WB банит за ТЕМП, а не за факт парсинга:
    # безопасно ≤~100 запросов/час, паузы 3-7с. Не снижай без нужды — словишь 403.
    request_min_delay: float = 3.0
    request_jitter: float = 4.0
    max_pages: int = 100

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
