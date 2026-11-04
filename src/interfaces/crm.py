"""Шов приёмника CRM (элементы смарт-процесса Bitrix24). Синхронный. Пишет только сервис фиксации."""

from typing import Protocol

from src.models import CrmEntry, CrmSite


class CrmSink(Protocol):
    def list_sites(self) -> list[CrmSite]: ...

    def upsert_entries(self, entries: list[CrmEntry]) -> None:
        """Создаёт или обновляет элементы по ключу (объект, день, ``position_key``). Идемпотентно."""
        ...

    def delete_entries(self, entries: list[CrmEntry]) -> None:
        """Удаляет элементы по тому же ключу. Отсутствующий элемент не ошибка."""
        ...
