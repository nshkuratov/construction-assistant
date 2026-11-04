"""Шов движка: на входе одно текстовое сообщение прораба, на выходе один структурированный результат."""

from typing import Protocol

from src.models import EngineOutcome


class Engine(Protocol):
    async def handle(self, chat_id: int, text: str) -> EngineOutcome: ...
