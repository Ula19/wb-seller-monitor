"""Мелкие помощники, общие для хендлеров, и отправка сообщений без потерь."""

import asyncio
import logging
import re

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

log = logging.getLogger(__name__)


async def send_safe(bot, uid: int, text: str, **kw) -> bool | None:
    """send_message, который не теряет сообщение на Flood control. → True — ушло; False — чата нет (бот заблокирован,
    «chat not found»): этому uid сейчас больше не слать; None — другая ошибка (в лог).
    RetryAfter — ждём сколько сказал Telegram (+1 с) и повторяем ОДИН раз."""
    for attempt in (1, 2):
        try:
            await bot.send_message(uid, text, **kw)
            return True
        except TelegramRetryAfter as e:
            if attempt == 2:
                log.warning("сообщение не доставлено %s: Flood control и после паузы: %s", uid, e)
                return None
            log.warning("Telegram: Flood control, жду %s с и повторяю (%s)", e.retry_after, uid)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError as e:
            log.warning("сообщение не доставлено %s: бот заблокирован: %s", uid, e)
            return False
        except TelegramBadRequest as e:
            log.warning("сообщение не доставлено %s: %s", uid, e)
            return False if "chat not found" in str(e).lower() else None
        except Exception as e:
            log.warning("сообщение не доставлено %s: %s", uid, e)
            return None
    return None


def parse_supplier_id(text: str | None) -> int | None:
    """Извлекает supplier_id из текста: число или ссылка wildberries.ru/seller/<ID>."""
    if not text:
        return None
    m = re.search(r"seller/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,})", text)
    return int(m.group(1)) if m else None


def parse_seller_slug(text: str | None) -> str | None:
    """Slug продавца из ссылки .../seller/<slug> или одиночного слова (не число)."""
    if not text:
        return None
    m = re.search(r"seller/([A-Za-z0-9][A-Za-z0-9_-]*)", text)
    tok = (m.group(1) if m else text).strip()
    if tok and not tok.isdigit() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]+", tok):
        return tok.lower()
    return None


if __name__ == "__main__":  # self-check парсеров и send_safe
    assert parse_supplier_id("https://www.wildberries.ru/seller/250110041") == 250110041
    assert parse_seller_slug("https://www.wildberries.ru/seller/moderndevice") == "moderndevice"
    assert parse_seller_slug("https://www.wildberries.ru/seller/250110041") is None  # число → не slug
    assert parse_seller_slug("ModernDevice") == "moderndevice"
    assert parse_seller_slug("добавь магазин сюда") is None

    from unittest.mock import patch
    from aiogram.methods import SendMessage

    M = SendMessage(chat_id=1, text="x")
    flood = lambda: TelegramRetryAfter(M, "Flood control exceeded on method 'SendMessage'. Retry in 10 seconds", 10)

    class Bot:
        def __init__(self, *script):
            self.script, self.sent = list(script), []

        async def send_message(self, uid, text, **kw):
            self.sent.append((uid, text, kw))
            x = self.script.pop(0) if self.script else None
            if x:
                raise x

    async def main():
        naps = []

        async def nap(sec):
            naps.append(sec)
        with patch("asyncio.sleep", nap):
            b = Bot(flood())
            assert await send_safe(b, 7, "привет", parse_mode="HTML") is True  # пауза и повтор — ушло
            assert naps == [11] and b.sent == [(7, "привет", {"parse_mode": "HTML"})] * 2, (naps, b.sent)
            b = Bot(flood(), flood())
            assert await send_safe(b, 7, "x") is None and len(b.sent) == 2  # повтор только один
            for e, want in ((TelegramBadRequest(M, "Bad Request: chat not found"), False),
                            (TelegramForbiddenError(M, "Forbidden: bot was blocked by the user"), False),
                            (TelegramBadRequest(M, "Bad Request: message is too long"), None), (RuntimeError("сеть"), None)):
                b = Bot(e)
                assert await send_safe(b, 7, "x") is want and len(b.sent) == 1, (e, want)
            assert naps == [11, 11]  # на прочих ошибках не ждём
    asyncio.run(main())
    print("ok")
