"""Кэш разрешённых пользователей (whitelist в памяти, синхронен с БД)."""

from app.db import repo
from app.db.base import Session


class Access:
    def __init__(self) -> None:
        self.allowed: set[int] = set()

    async def load(self) -> None:
        async with Session() as s:
            self.allowed = set(await repo.list_user_ids(s))

    def is_allowed(self, uid: int | None) -> bool:
        return uid is not None and uid in self.allowed

    def add(self, uid: int) -> None:
        self.allowed.add(uid)

    def remove(self, uid: int) -> None:
        self.allowed.discard(uid)


access = Access()
