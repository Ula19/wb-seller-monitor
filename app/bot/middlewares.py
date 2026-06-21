"""Middleware доступа: отсекает чужих до любого хендлера (сообщения и кнопки)."""

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from app.bot.access import access
from app.config import settings


class AuthMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        uid = user.id if user else None
        if uid == settings.owner_id or access.is_allowed(uid):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ Нет доступа", show_alert=True)
        else:
            try:
                await event.answer(
                    "⛔ Нет доступа.\n"
                    f"Ваш ID: {uid}\n"
                    f"Передайте его администратору для команды /grant {uid}."
                )
            except Exception:
                pass
        return None
