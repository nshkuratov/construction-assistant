"""Шов справочника объектов: объекты, общие для Google Таблиц и Bitrix24."""

from typing import Protocol

from src.models import SiteListing


class SiteDirectory(Protocol):
    def list_sites(self) -> SiteListing: ...
