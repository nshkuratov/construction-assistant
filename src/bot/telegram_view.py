"""Рендер в Telegram: ``CardView`` → ``InlineKeyboardMarkup``, отправка и правка единственного сообщения карточки."""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from src.bot.card import Button, CardView

logger = logging.getLogger(__name__)

NOT_MODIFIED = "message is not modified"
_GONE_MARKERS = ("message to edit not found", "message can't be edited", "message_id_invalid")


def to_markup(buttons: list[list[Button]]) -> InlineKeyboardMarkup | None:
    rows = [
        [
            InlineKeyboardButton(b.label, url=b.url) if b.url else InlineKeyboardButton(b.label, callback_data=b.data)
            for b in row
        ]
        for row in buttons
        if row
    ]
    return InlineKeyboardMarkup(rows) if rows else None


class CardMessenger:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send_new(self, chat_id: int, view: CardView, old_message_id: int | None = None) -> int:
        """Отправляет новое сообщение карточки; клавиатура старой карточки сначала убирается."""
        if old_message_id is not None:
            await self.strip_keyboard(chat_id, old_message_id)
        msg = await self._bot.send_message(
            chat_id, view.text, parse_mode=view.parse_mode, reply_markup=to_markup(view.buttons)
        )
        return msg.message_id

    async def edit(self, chat_id: int, message_id: int | None, view: CardView) -> int:
        """Правит карточку на месте. Исчезнувшее сообщение заменяется новым. Возвращает id сообщения."""
        if message_id is None:
            return await self.send_new(chat_id, view)
        try:
            await self._bot.edit_message_text(
                view.text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=view.parse_mode,
                reply_markup=to_markup(view.buttons),
            )
        except BadRequest as exc:
            text = str(exc).lower()
            if NOT_MODIFIED in text:
                return message_id
            if any(marker in text for marker in _GONE_MARKERS):
                return await self.send_new(chat_id, view)
            raise
        return message_id

    async def strip_keyboard(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except BadRequest as exc:
            logger.info("old card keyboard not removed: %s", exc)

    async def plain(self, chat_id: int, text: str, buttons: list[list[Button]] | None = None) -> int:
        msg = await self._bot.send_message(chat_id, text, reply_markup=to_markup(buttons or []))
        return msg.message_id
