from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки берутся из переменных окружения / .env (см. .env.example)."""

    bot_token: str
    owner_id: int
    database_url: str = "postgresql+asyncpg://wb:wb@db:5432/wb"

    # параметры запросов к WB
    wb_dest: int = -1257786
    wb_spp: int = 30
    # пул слотов: прямой IP + по слоту на каждый прокси отсюда (запятая-разделитель,
    # схемы socks5h:// или http://). Магазины делятся по слотам параллельно; слот
    # магазина обслуживает и каталог, и b2b-detail. Класс/гео IP мобильному API не
    # важны — бери любые дешёвые, важен только суммарный темп на IP.
    wb_proxies: str = ""
    # каталог и бизнес-цены ходят в мобильное API (api-ios) как iOS-приложение WB.
    # guardium (заголовок x-guardium-token) — антибот, живёт ~3 дня, IP не проверяет.
    # Bearer (заголовок authorization) — аккаунт для бизнес-цен, срока нет.
    # Актуальные обновляются кнопкой «Токены», здесь — стартовый seed.
    # deviceid и UA — с того телефона, где сняты токены.
    wb_guardium: str = ""
    wb_bearer: str = ""
    # udg из запросов приложения в бизнес-режиме (HAR 2026-10-28). ponytail: смысл
    # не известен — если у другого аккаунта бизнес-цены не придут, сверить с его HAR.
    wb_b2b_udg: int = 82
    wb_ios_device_id: str = "A412BA68B34F45B28D783F5AF464537E"
    wb_ios_ua: str = "Wildberries/7.7.9001 (RU.WILDBERRIES.MOBILEAPP; build:24083938; iOS 27.0.1) Alamofire/5.9.1"
    # режим цены по умолчанию для магазинов, добавленных командой /addseller
    # (в UI режим выбирается кнопками). True — бизнес (нужен Bearer), False — розница.
    wb_b2b: bool = True
    # временный дебаг: логировать сырой JSON первого товара (смотрим все поля API)
    debug_raw: bool = False

    # расписание: единый джоб, ВСЕ магазины каждые N секунд (b2b — с кукой каждый цикл)
    monitor_interval_seconds: int = 60
    big_shop_threshold: int = 100
    # минимальное СНИЖЕНИЕ цены (%) для уведомления. Рост цены не алертим вовсе.
    price_drop_threshold_pct: float = 1.0
    # наша цена = цена ВБ минус этот процент
    our_discount_pct: float = 15.0
    # скидка WB Кошелька: у розничных магазинов рядом с ценой показываем «с кошельком −N%».
    # Только отображение (в БД хранится цена без кошелька = каталожная витрина).
    # У WB ставка персональная/пер-товарная — это ориентир нашего уровня. 0 = не показывать.
    wb_wallet_discount_pct: float = 6.0

    # троттлинг (защита от бана по IP). WB банит за ТЕМП, а не за факт парсинга:
    # безопасно ≤~100 запросов/час, паузы 3-7с. Не снижай без нужды — словишь 403.
    request_min_delay: float = 3.0
    request_jitter: float = 4.0
    max_pages: int = 100

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
