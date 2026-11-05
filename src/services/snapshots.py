"""Снимки каталога для каждого чата; перечитываются только когда устарели."""

import asyncio
from datetime import UTC, datetime, timedelta

from src.config import Config
from src.interfaces.sheets import SheetGateway
from src.interfaces.sites import SiteDirectory
from src.models import CatalogSnapshot
from src.session.store import SessionStore
from src.utils.timeutil import now_utc


class SnapshotService:
    def __init__(
        self,
        gateway: SheetGateway,
        directory: SiteDirectory,
        data_dir: str | None = None,
        max_age_s: float | None = None,
    ):
        self._gateway = gateway
        self._directory = directory
        self._data_dir = data_dir
        self._max_age = timedelta(seconds=Config.SNAPSHOT_MAX_AGE_S if max_age_s is None else max_age_s)

    async def refresh(self, chat_id: int, site_ids: list[str], force: bool = False) -> dict[str, CatalogSnapshot]:
        """Обновляет снимки для ``site_ids`` ОДНИМ чтением через шлюз. Неизвестные id объектов пропускаются.

        Возвращает снимок каждого известного объекта (свежий или только что прочитанный).
        """
        store = SessionStore(chat_id, self._data_dir)
        listing = await asyncio.to_thread(self._directory.list_sites)
        by_id = {s.site_id: s for s in listing.sites}
        now = now_utc()

        result: dict[str, CatalogSnapshot] = {}
        stale = []
        for site_id in dict.fromkeys(site_ids):
            site = by_id.get(site_id)
            if site is None:
                continue
            snap = store.read_snapshot(site_id)
            if snap is not None and not force and now - _aware(snap.taken_at) < self._max_age:
                result[site_id] = snap
            else:
                stale.append(site)
        if stale:
            loaded = await asyncio.to_thread(self._gateway.load_snapshots, [s.sheet_tab for s in stale])
            for site in stale:
                snap = loaded[site.sheet_tab].model_copy(update={"site_id": site.site_id})
                store.write_snapshot(snap)
                result[site.site_id] = snap
        return result


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
