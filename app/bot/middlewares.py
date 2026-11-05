"""Middleware доступа: отсекает чужих до любого хендлера (сообщения и кнопки)."""

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from app.config import settings


def start_text(uid: int | None) -> str:
    """Что бот отвечает всем, кроме владельца: ID и (если пускаем) ссылку на сайт."""
    if uid in settings.superadmins:
        site = f"https://{settings.site_domain}" if settings.site_domain else "сайт ещё не настроен"
        return f"Ваш Telegram ID: {uid}\nУправление мониторингом — на сайте: {site}\nСюда будут приходить уведомления."
    return f"Ваш Telegram ID: {uid}\nДоступа нет — передайте этот ID владельцу."


class AuthMiddleware(BaseMiddleware):
    """Старое меню бота — только владельцу (первый ID); остальным — ID и ссылка на сайт."""
    # ponytail: меню бота удаляем целиком, когда сайт закроет отчёты, «Токены» и /backup

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        uid = user.id if user else None
        if uid == settings.owner_id:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Управление — на сайте", show_alert=True)
        else:
            try:
                await event.answer(start_text(uid))
            except Exception:
                pass
        return None
