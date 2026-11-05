"""Справочник объектов: объекты, которые есть И в Google Sheets (вкладки), И в Bitrix24."""

import hashlib
import re
import time
from collections.abc import Callable

from src.config import Config
from src.interfaces.crm import CrmSink
from src.interfaces.sheets import SheetGateway
from src.models import Site, SiteListing


def normalise_name(name: str) -> str:
    """Регистр, пробелы и ``ё`` не важны, когда два хранилища называют один объект."""
    return re.sub(r"\s+", "", name.casefold().replace("ё", "е"))


def site_id_for(name: str) -> str:
    """Стабильный id объекта. Переименование вкладки даёт новый объект (известное ограничение)."""
    return hashlib.sha1(normalise_name(name).encode("utf-8")).hexdigest()[:8]


class LiveSiteDirectory:
    def __init__(
        self,
        gateway: SheetGateway,
        crm: CrmSink,
        max_age_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._gateway = gateway
        self._crm = crm
        self._max_age_s = Config.SNAPSHOT_MAX_AGE_S if max_age_s is None else max_age_s
        self._clock = clock
        self._cached: SiteListing | None = None
        self._cached_at = 0.0

    def list_sites(self) -> SiteListing:
        if self._cached is not None and self._clock() - self._cached_at < self._max_age_s:
            return self._cached
        listing = self._build()
        self._cached, self._cached_at = listing, self._clock()
        return listing

    def _build(self) -> SiteListing:
        tabs = self._gateway.list_site_tabs()
        crm_sites = self._crm.list_sites()
        mismatches: list[str] = []

        crm_by_key = {}
        for cs in crm_sites:
            key = normalise_name(cs.name)
            if key in crm_by_key:
                mismatches.append(f"Битрикс24: повтор названия «{cs.name}»")
            else:
                crm_by_key[key] = cs

        sites: list[Site] = []
        seen: set[str] = set()
        for tab in tabs:
            key = normalise_name(tab)
            if key in seen:
                mismatches.append(f"Google Sheets: повтор названия «{tab}»")
                continue
            seen.add(key)
            cs = crm_by_key.get(key)
            if cs is None:
                mismatches.append(f"Только в Google Sheets: «{tab}»")
                continue
            sites.append(Site(site_id=site_id_for(tab), name=tab, sheet_tab=tab, crm_value_id=cs.value_id))
        mismatches.extend(f"Только в Битрикс24: «{cs.name}»" for key, cs in crm_by_key.items() if key not in seen)
        return SiteListing(sites=sites, mismatches=mismatches)
