"""Реестры блокировок для бота: одна блокировка на чат, одна на вкладку таблицы.

Сервис фиксации берёт ОДНУ блокировку вкладки за фиксацию, поэтому риска порядка блокировок нет.
Ключ блокировки — название вкладки, потому что вкладка — это единица конкуренции за запись в таблице.
"""

import asyncio
from collections.abc import Hashable


class LockRegistry:
    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}

    def get(self, key: Hashable) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock


class Locks:
    def __init__(self) -> None:
        self._chats = LockRegistry()
        self._tabs = LockRegistry()

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        return self._chats.get(chat_id)

    def tab_lock(self, tab: str) -> asyncio.Lock:
        return self._tabs.get(tab)
