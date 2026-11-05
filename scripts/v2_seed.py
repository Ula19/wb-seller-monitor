"""Живая проверка v2 без меню бота: добавить владельцу магазины и (по желанию) войти в аккаунт WB.

Запуск в контейнере v2 (первая загрузка магазинов пройдёт молча в ближайшем цикле мониторинга):
    docker compose exec bot python -m scripts.v2_seed 250169125:b2b 250000173 768659
    docker compose exec -it bot python -m scripts.v2_seed --login 9161234567   # бизнес-аккаунт, код вводишь сюда

Магазин: ID[:b2b|retail] (по умолчанию розница). Повторный запуск не дублирует.
Сессию из старой установки НЕ копировать: обе освежали бы её, refresh-токен WB ротируется → одну разлогинит.
"""
import argparse
import asyncio
import re

from app.config import settings
from app.db import repo
from app.db.base import Session, engine
from app.services import keeper
from app.wb import web_session
from app.wb.client import wb_client


def parse_shop(arg: str) -> tuple[int, bool]:
    m = re.fullmatch(r"(\d+)(?::(b2b|retail))?", arg.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"не похоже на ID[:b2b|retail]: {arg}")
    return int(m[1]), m[2] == "b2b"


async def add_shops(shops: list[tuple[int, bool]]) -> None:
    uid = settings.owner_id
    async with Session() as s:
        await repo.ensure_owner(s, uid)
        await s.commit()
    for sid, b2b in shops:
        info = await wb_client.fetch_supplier_info(sid) or {}
        name = info.get("trademark") or info.get("supplierName")
        async with Session() as s:
            added = await repo.add_source(s, uid, sid, name=name, brand=info.get("trademark"), b2b=b2b)
            await s.commit()
        print(f"{'+ добавлен' if added else '= уже есть'}: {sid} «{name or '?'}» ({'бизнес' if b2b else 'розница'})")


async def login(phone: str) -> None:
    async def get_code():
        return (await asyncio.to_thread(input, "Код из пуша/SMS WB: ")).strip()

    print("Открываю WB в браузере и ввожу номер… (до 1–2 мин)")
    state = await web_session.login(phone, get_code)
    if not state:
        print("❌ вход не удался (неверный код или антибот)")
        return
    acc = await keeper.save_login(settings.owner_id, phone, "business", state)
    print(f"✅ аккаунт {acc.label} сохранён (бизнес), Bearer в сессии: {'да' if keeper.has_bearer(state) else 'НЕТ'}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shops", nargs="*", type=parse_shop, help="ID[:b2b|retail]")
    ap.add_argument("--login", metavar="ТЕЛЕФОН", help="10 цифр без +7: войти в бизнес-аккаунт владельца")
    args = ap.parse_args()
    try:
        if args.login:
            phone = re.sub(r"\D", "", args.login)[-10:]
            if len(phone) != 10:
                ap.error("нужно 10 цифр номера без кода страны")
            await login(phone)
        if args.shops:
            await add_shops(args.shops)
    finally:
        await wb_client.close()
        await engine.dispose()


if __name__ == "__main__":
    assert parse_shop("250169125:b2b") == (250169125, True) and parse_shop("768659") == (768659, False)
    asyncio.run(main())
