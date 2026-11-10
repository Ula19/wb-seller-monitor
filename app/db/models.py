"""Схема v2: товар глобален (по nm_id), пользователь следит за ним через источники.

Один скан WB обслуживает всех: items/price_history общие, sources — чьё и в каком
режиме цены, бизнес-цена — у каждого бизнес-аккаунта своя (account_prices).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """Пользователи с доступом. role: owner | user."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = _now()


class Seller(Base):
    """Продавец WB (данные с WB, общие для всех)."""

    __tablename__ = "sellers"

    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    added_at: Mapped[datetime] = _now()
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Item(Base):
    """Товар WB глобально + его текущий снимок (розница = витрина каталога).

    Не удаляем: пропал из выдачи — is_active=False. Поэтому «новинка» = nm_id,
    которого ещё нет в items.
    """

    __tablename__ = "items"

    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ponytail: без FK на sellers — поиск принесёт товары неотслеживаемых продавцов
    supplier_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pics: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # розница, ₽
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_seller: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # промахов подряд: нет в выдаче цикла. «Пропал» — только после repo.MISS_TO_GONE (один промах —
    # недолистанная страница WB, а не уход товара); появился — сброс в 0
    miss_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()


class PriceHistory(Base):
    """События товара для графиков и ленты: new — появился, change — цена/остаток,
    gone — пропал из выдачи (stock=0). Розничная цена."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.nm_id", ondelete="CASCADE"), index=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str] = mapped_column(String(8), default="change", server_default="change")
    at: Mapped[datetime] = _now()


class Source(Base):
    """Что отслеживает пользователь. Пока kind=seller (поиск и товар — позже).

    seeded_at — когда прошла первая загрузка: до неё новинки не шлём, иначе весь
    ассортимент магазина пришёл бы «новинками».
    """

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("user_id", "kind", "supplier_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="seller", server_default="seller")
    supplier_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sellers.supplier_id", ondelete="CASCADE"), nullable=True)
    b2b: Mapped[bool] = mapped_column(Boolean, default=False)  # режим цены: бизнес / розница
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()

    seller: Mapped[Seller | None] = relationship(lazy="joined")

    @property
    def name(self) -> str | None:
        return self.seller.name if self.seller else None


class Proxy(Base):
    """Прокси аккаунтов WB (вводятся на сайте). url с логином/паролем — только зашифрованным
    (app.crypto), server = host:port для показа. Проверку (внешний IP, доступность WB) делает
    worker: api ставит checking, worker пишет exit_ip/ok/error."""

    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    server: Mapped[str] = mapped_column(String(128))
    url_enc: Mapped[bytes] = mapped_column(LargeBinary)
    exit_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    checking: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()


class WbAccount(Base):
    """Аккаунт WB пользователя (1–10 на пользователя).

    state_enc — storageState браузера ЦЕЛИКОМ (куки + localStorage с Bearer
    wbx__tokenData), зашифрован app.crypto. status: active | relogin | off.
    """

    __tablename__ = "wb_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # своё имя с сайта
    phone_mask: Mapped[str] = mapped_column(String(32))  # ключ «тот же аккаунт» при повторном входе
    # профиль из WB (www/webapi/personalinfo): при входе и на рефреше сессии
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)  # полностью: сайт закрытый
    purchase_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)  # процент выкупа
    kind: Mapped[str] = mapped_column(String(16), default="business")  # business | retail
    state_enc: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # состояние (account_api, раз в 10 мин), рубли; у бизнес-аккаунта может быть NULL
    wallet_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)  # % Кошелька
    limit_left: Mapped[int | None] = mapped_column(Integer, nullable=True)  # остаток месячного лимита скидки
    monthly_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # месячный лимит, сброс 1-го числа
    max_order: Mapped[int | None] = mapped_column(Integer, nullable=True)  # разовый лимит заказа
    max_rid: Mapped[int | None] = mapped_column(Integer, nullable=True)  # лимит позиции
    funds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ₽ в Кошельке WB (available_funds_rub)
    vb_points: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ВБ-баллы (не рубли)
    # кошелёк заблокирован (full-balance: wallet.details.availability не "available"); None — WB поля не прислал
    wallet_blocked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    wallet_arrested: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ₽ под арестом
    state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # сбои прокси аккаунта подряд (опрос/рефреш); сброс — удачный опрос или новый вход
    proxy_fails: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    polling: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # ↻ с сайта: worker опросит
    # визит в браузере аккаунта (код получения + история баланса): плановый в 00:05/12:05 МСК или по кнопке с сайта
    browser_sync: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    pickup_code: Mapped[str | None] = mapped_column(String(16), nullable=True)  # код получения в ПВЗ
    pickup_code_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # день МСК, на который код (до 00:00)
    # история заказов загружена целиком (до последней страницы) — дальше раз в час только догрузка новых
    purchases_full_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    visit_error: Mapped[str | None] = mapped_column(String(128), nullable=True)  # почему визит в браузере не удался
    proxy_fail_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # свой выход в WB: вход, рефреш, состояние, personalinfo. Меняется только перелогином
    proxy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("proxies.id", ondelete="RESTRICT"), nullable=True, index=True)

    @property
    def title(self) -> str:
        """Как называть аккаунт: своё имя с сайта, иначе имя из WB, иначе маска номера."""
        return self.label or self.name or self.phone_mask


class AccountPurchase(Base):
    """История заказов аккаунта: полученные позиции (astro purchases/list, status 32). Копим, не удаляем."""

    __tablename__ = "account_purchases"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    rid: Mapped[str] = mapped_column(String(64), primary_key=True)
    nm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ₽ оплачено (со скидкой Кошелька)
    dt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)  # забрали
    office_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    office_address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    office_hours: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AccountVbOp(Base):
    """Операция ВБ-баллов (loyalty bonuses/history). operation: 1 начисление, 2 списание; amount — баллы."""

    __tablename__ = "account_vb_ops"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    tx_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AccountBalanceOp(Base):
    """Операция баланса Кошелька (mywallet в браузере; WB отдаёт 20 последних — копим у себя).
    op_type: 0 приход, 1 расход; amount — ₽ со знаком (moneyIncome + bonusIncome)."""

    __tablename__ = "account_balance_ops"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    tx_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    op_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    dt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BalanceNote(Base):
    """Своя заметка к операции баланса (с сайта). Живёт отдельно — операции перезаписываются из WB."""

    __tablename__ = "balance_notes"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    tx_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    text: Mapped[str] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccountOrder(Base):
    """Активная позиция заказа аккаунта (rid из wbxoofex). Ушла в архив — строку удаляем."""

    __tablename__ = "account_orders"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    rid: Mapped[str] = mapped_column(String(64), primary_key=True)
    nm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ₽
    dst_office_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # ПВЗ
    status_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1 оформлен … 3 в пути, 5 в ПВЗ
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # заберите до
    delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # ожидается до
    paid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None — WB не сказал
    office_address: Mapped[str | None] = mapped_column(String(256), nullable=True)  # из справочника ПВЗ (pickups)
    office_hours: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # скрыт на сайте


class AccountPrice(Base):
    """Бизнес-цена товара под конкретным аккаунтом."""

    __tablename__ = "account_prices"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    nm_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.nm_id", ondelete="CASCADE"), primary_key=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priced_at: Mapped[datetime] = _now()


class Target(Base):
    """Цель ₽ на товар: пуш, когда лучшая цена (витрина/бизнес/с Кошельком) опустится до неё.

    below — сейчас ниже цели (и в наличии): пуш только на пересечении вниз, без повторов;
    цена выше или товар кончился — сбрасывается, следующее пересечение снова даст пуш.
    """

    __tablename__ = "targets"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True)
    nm_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.nm_id", ondelete="CASCADE"), primary_key=True)
    price: Mapped[int] = mapped_column(Integer)
    below: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    below_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # последнее срабатывание
    created_at: Mapped[datetime] = _now()


class WbLoginRequest(Base):
    """Заявка сайта на вход в аккаунт WB. api пишет, worker выполняет (браузер есть только у него).

    status: new → running → wait_code → done | error | cancelled (отмена с сайта). Активная — одна на пользователя
    (частичный уникальный индекс). Номер и код стираются, когда заявка завершилась.
    """

    __tablename__ = "wb_login_requests"
    __table_args__ = (Index("uq_login_active", "user_id", unique=True,
                            postgresql_where=text("status IN ('new', 'running', 'wait_code')")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="SET NULL"), nullable=True)
    proxy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True)
    resend: Mapped[str | None] = mapped_column(String(8), nullable=True)  # просьба сайта: push | sms
    sent_via: Mapped[str | None] = mapped_column(String(8), nullable=True)  # куда WB отправил код: PUSH | SMS
    resend_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # раньше WB не даст
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Certificate(Base):
    """Подарочный сертификат WB к активации на аккаунт. api пишет pending, worker активирует.

    status: pending → sending (уходит в WB; ставится прямо перед POST) → ok | error | unknown (ответа нет — мог
    активироваться, проверьте баланс; не повторяем). code — 16 символов открытым текстом (решение владельца),
    уникален среди не-ошибочных: код, на который WB ответил ошибкой, можно ввести снова.
    """

    __tablename__ = "certificates"
    __table_args__ = (Index("uq_cert_code", "code", unique=True, postgresql_where=text("status <> 'error'")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    nominal_entered: Mapped[int] = mapped_column(Integer)  # ₽, что ввёл человек (до активации WB номинал не отдаёт)
    nominal: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ₽ из ответа WB
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    wb_cert_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)  # = txId в истории баланса
    error_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = _now()
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AppSetting(Base):
    """Key-value рантайм-настройки (guardium, часы отчётов, рабочее окно)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
