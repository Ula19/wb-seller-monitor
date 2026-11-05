"""Ежедневный бэкап БД владельцу в Telegram.

Дамп шифруется age ОТКРЫТЫМ ключом (BACKUP_AGE_RECIPIENT): в БД живая сессия
аккаунта WB, а на сервере нет закрытого ключа, чтобы расшифровать. Нет ключа —
бэкап не делаем вовсе: открытый дамп в чат не уходит никогда.

Восстановление: расшифровать у себя (закрытый ключ из менеджера паролей)
    age -d -i key.txt wb-….dump.age > wb.dump
и залить pg_restore'ом ИЗ КОНТЕЙНЕРА БОТА: там клиент 17, старый 16 формат дампа 17 не читает
    docker compose exec -T bot pg_restore --clean --if-exists --no-owner -d postgresql://wb:wb@db:5432/wb < wb.dump
"""
import asyncio
import logging
from datetime import datetime

from aiogram.types import BufferedInputFile

from app.config import settings

log = logging.getLogger(__name__)

TG_LIMIT = 50 * 1024 * 1024  # бот не отправит документ больше 50 МБ


class BackupError(Exception):
    pass


async def _run(cmd: list[str], stdin: bytes | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(stdin), 300)
    if proc.returncode != 0:
        raise BackupError(f"{cmd[0]}: код {proc.returncode}: {err.decode(errors='replace')[:300]}")
    return out


async def make_backup() -> tuple[str, bytes]:
    """Зашифрованный дамп (имя файла, байты). -Fc уже сжат — gzip не нужен."""
    recipient = settings.backup_age_recipient.strip()
    if not recipient:
        raise BackupError("не задан BACKUP_AGE_RECIPIENT — без шифрования бэкап не отправляю")
    dsn = settings.database_url.replace("+asyncpg", "")  # pg_dump не знает драйвер SQLAlchemy
    dump = await _run(["pg_dump", "-Fc", "--no-owner", dsn])
    data = await _run(["age", "-r", recipient], stdin=dump)
    if len(data) > TG_LIMIT:
        raise BackupError(f"дамп {len(data) // 2**20} МБ больше лимита Telegram 50 МБ")
    return f"wb-{datetime.now():%Y%m%d-%H%M}.dump.age", data


async def send_backup(bot) -> str:
    """Делает бэкап и шлёт владельцу. Возвращает текст итога (для /backup)."""
    try:
        name, data = await make_backup()
    except Exception as e:
        log.error("бэкап: не удался: %s", e)
        text = f"⚠️ Бэкап БД не удался: {e}"
        await bot.send_message(settings.owner_id, text)
        return text
    await bot.send_document(
        settings.owner_id, BufferedInputFile(data, filename=name),
        caption=f"🗄 Бэкап БД, {len(data) // 1024} КБ, зашифрован age",
    )
    log.info("бэкап: отправлен %s, %d КБ", name, len(data) // 1024)
    return f"✅ Бэкап отправлен: {name}"


async def backup_job(bot) -> None:
    await send_backup(bot)


if __name__ == "__main__":  # self-check: без ключа не шифруем и не шлём, с ключом — пайплайн
    from unittest.mock import AsyncMock, patch

    async def demo():
        bot = AsyncMock()
        run = AsyncMock(side_effect=[b"DUMP", b"ENC"])
        with patch(f"{__name__}._run", run), patch.object(settings, "backup_age_recipient", ""):
            assert "BACKUP_AGE_RECIPIENT" in await send_backup(bot)
            assert run.await_count == 0 and bot.send_document.await_count == 0
        with patch(f"{__name__}._run", run), patch.object(settings, "backup_age_recipient", "age1x"):
            assert (await send_backup(bot)).startswith("✅")
            assert run.await_args_list[0].args[0][-1].startswith("postgresql://")
            assert run.await_args_list[1].args[0] == ["age", "-r", "age1x"]
            assert run.await_args_list[1].kwargs["stdin"] == b"DUMP"
            doc = bot.send_document.await_args.args[1]
            assert doc.data == b"ENC" and doc.filename.endswith(".dump.age")
        # настоящий сбой команды → понятная ошибка с кодом
        try:
            await _run(["sh", "-c", "echo boom >&2; exit 3"])
            raise AssertionError("ожидали BackupError")
        except BackupError as e:
            assert "код 3" in str(e) and "boom" in str(e)
        print("ok")

    asyncio.run(demo())
