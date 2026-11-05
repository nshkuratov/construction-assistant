"""ЕДИНСТВЕННЫЙ, кто пишет в Google Sheets и Bitrix24.

Порядок: сначала Sheets (источник истины), затем Bitrix24, всё под одной блокировкой вкладки.
Итоговые значения ячеек считаются один раз и сохраняются в записи с упреждающей фиксацией до первой
записи, поэтому сбой или повторное подтверждение никогда не добавят объём дважды. Запись лежит файлом
в каталоге чата, потому что ``ChatSession`` отклоняет неизвестные ключи.
"""

import asyncio
from datetime import date
from pathlib import Path

from src.interfaces.crm import CrmSink
from src.interfaces.errors import CrmError, SheetsError
from src.interfaces.sheets import SheetGateway
from src.interfaces.sites import SiteDirectory
from src.models import CatalogSnapshot, CellWrite, CommitResult, CrmEntry, Draft, Site
from src.services.locks import Locks
from src.session.store import SessionStore
from src.utils.persistence import append_jsonl, atomic_write_json, safe_load_json
from src.utils.timeutil import utcnow_iso

RECORD_NAME = "commit_record.json"
_EPS = 1e-9


class CommitRefused(Exception):
    """Фиксацию нельзя начать: нет такого черновика, версия устарела, есть неопределённая строка или объект исчез."""


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < _EPS


def position_key(number: int | None, row: int, name: str) -> str:
    return f"{number}|{name}" if number is not None else f"row{row}|{name}"


def _entry_key(item: dict) -> tuple:
    e = item["entry"]
    return (e["crm_value_id"], e["day"], e["position_key"])


class CommitService:
    def __init__(
        self,
        gateway: SheetGateway,
        crm: CrmSink,
        directory: SiteDirectory,
        locks: Locks | None = None,
        data_dir: str | None = None,
        audit_path: str = "logs/commits.jsonl",
    ):
        self._gateway = gateway
        self._crm = crm
        self._directory = directory
        self._locks = locks or Locks()
        self._data_dir = data_dir
        self._audit_path = audit_path

    async def commit(self, chat_id: int, draft_id: str, version: int) -> CommitResult:
        store = SessionStore(chat_id, self._data_dir)
        draft = self._pending_or_done(store, draft_id, version)
        if isinstance(draft, CommitResult):
            return draft
        site = await self._site(draft.site_id)
        async with self._locks.tab_lock(site.sheet_tab):
            draft = self._pending_or_done(store, draft_id, version)
            if isinstance(draft, CommitResult):
                return draft
            self._check_lines(draft)
            return await self._commit_locked(store, draft, site)

    def _pending_or_done(self, store: SessionStore, draft_id: str, version: int) -> Draft | CommitResult:
        """Ожидающий черновик или первый результат, если именно этот (черновик, версия) уже зафиксирован."""
        session = store.load_session()
        draft = session.draft
        if draft is None or draft.draft_id != draft_id:
            raise CommitRefused("no such draft")
        if draft.version != version:
            raise CommitRefused("stale version")
        if draft.status == "committed":
            if session.last_commit is None or session.last_commit.draft_id != draft_id:
                raise CommitRefused("committed draft has no result")
            return session.last_commit
        return draft

    @staticmethod
    def _check_lines(draft: Draft) -> None:
        if not draft.lines:
            raise CommitRefused("empty draft")
        if any(ln.position_id is None or ln.candidates for ln in draft.lines):
            raise CommitRefused("unresolved line")

    async def _site(self, site_id: str) -> Site:
        listing = await asyncio.to_thread(self._directory.list_sites)
        for site in listing.sites:
            if site.site_id == site_id:
                return site
        raise CommitRefused("site is gone")

    async def _commit_locked(self, store: SessionStore, draft: Draft, site: Site) -> CommitResult:
        tab = site.sheet_tab
        try:
            snap = (await asyncio.to_thread(self._gateway.load_snapshots, [tab]))[tab]
            record = self._usable_record(store, draft, snap)
            if record is None:
                record = self._build_record(draft, site, snap)
                atomic_write_json(str(self._record_path(store)), record)
            cells = record["cells"]
            writes = [CellWrite(position_id=c["position_id"], expected_name=c["name"], value=c["final"]) for c in cells]
            await asyncio.to_thread(self._gateway.ensure_date_column, tab, draft.day)
            await asyncio.to_thread(self._gateway.write_cells, tab, draft.day, writes)
        except SheetsError as exc:
            result = CommitResult(draft_id=draft.draft_id, sheets_ok=False, crm_ok=False, errors=[str(exc)])
            self._audit(store, site, draft, [], result)
            return result

        failed, errors = await self._send_crm(record["entries"])
        previous = store.load_session().last_commit
        sent = {_entry_key(i) for i in record["entries"]}
        carried = [i for i in (previous.crm_pending if previous else []) if _entry_key(i) not in sent]
        pending = carried + failed
        result = CommitResult(
            draft_id=draft.draft_id,
            sheets_ok=True,
            crm_ok=not pending,
            written=[{"position_id": c["position_id"], "name": c["name"], "value": c["final"]} for c in cells],
            errors=errors,
            crm_pending=pending,
        )
        self._mark_committed(store, draft, result)
        self._record_path(store).unlink(missing_ok=True)
        self._audit(store, site, draft, record["cells"], result)
        return result

    def _mark_committed(self, store: SessionStore, draft: Draft, result: CommitResult) -> None:
        def _mark(s) -> None:
            if s.draft is not None and s.draft.draft_id == draft.draft_id and s.draft.version == draft.version:
                s.draft.status = "committed"
            s.last_commit = result

        store.update_session(_mark)

    @staticmethod
    def _record_path(store: SessionStore) -> Path:
        return store.dir / RECORD_NAME

    def _usable_record(self, store: SessionStore, draft: Draft, snap: CatalogSnapshot) -> dict | None:
        """Сохранённая запись, если она ещё действительна: тот же черновик и версия, каждая ячейка всё ещё
        хранит значение «до» (ещё не записана) или итоговое значение (уже записана)."""
        record = safe_load_json(str(self._record_path(store)), default=None)
        if not record or record.get("draft_id") != draft.draft_id or record.get("version") != draft.version:
            return None
        iso = draft.day.isoformat()
        by_id = {p.id: p for p in snap.positions}
        for c in record["cells"]:
            pos = by_id.get(c["position_id"])
            if pos is None or pos.name != c["name"]:
                return None
            cur = pos.values.get(iso)
            if not (_same(cur, c["before"]) or _same(cur, c["final"])):
                return None
        return record

    @staticmethod
    def _build_record(draft: Draft, site: Site, snap: CatalogSnapshot) -> dict:
        iso = draft.day.isoformat()
        by_id = {p.id: p for p in snap.positions}
        before: dict[int, float | None] = {}
        final: dict[int, float | None] = {}
        for line in draft.lines:
            pid = line.position_id
            pos = by_id.get(pid)
            if pos is None:
                raise CommitRefused("position is not in the sheet")
            if pid not in final:
                before[pid] = final[pid] = pos.values.get(iso)
            if line.mode == "add":
                final[pid] = round((final[pid] or 0.0) + line.value, 6)
            elif line.mode == "set":
                final[pid] = line.value
            else:
                final[pid] = None
        cells, entries = [], []
        for pid, value in final.items():
            pos = by_id[pid]
            cells.append({"position_id": pid, "name": pos.name, "before": before[pid], "final": value})
            entry = CrmEntry(
                site_name=site.name,
                crm_value_id=site.crm_value_id,
                day=draft.day,
                position_key=position_key(pos.number, pos.id, pos.name),
                position_name=pos.name,
                section=pos.section,
                unit=pos.unit,
                value=0.0 if value is None else value,
                unit_price=pos.unit_price,
            )
            entries.append(
                {
                    "op": "delete" if value is None else "upsert",
                    "tab": site.sheet_tab,
                    "position_id": pid,
                    "entry": entry.model_dump(mode="json"),
                }
            )
        return {
            "draft_id": draft.draft_id,
            "version": draft.version,
            "tab": site.sheet_tab,
            "day": iso,
            "cells": cells,
            "entries": entries,
        }

    async def _send_crm(self, items: list[dict]) -> tuple[list[dict], list[str]]:
        """Отправляет upsert, затем удаления. Возвращает записи, которые не прошли, и ошибки."""
        upserts = [i for i in items if i["op"] == "upsert"]
        deletes = [i for i in items if i["op"] == "delete"]
        try:
            if upserts:
                await asyncio.to_thread(self._crm.upsert_entries, [CrmEntry(**i["entry"]) for i in upserts])
        except CrmError as exc:
            return list(items), [str(exc)]
        try:
            if deletes:
                await asyncio.to_thread(self._crm.delete_entries, [CrmEntry(**i["entry"]) for i in deletes])
        except CrmError as exc:
            return deletes, [str(exc)]
        return [], []

    async def retry_crm(self, chat_id: int) -> CommitResult:
        """Повторно отправляет ожидающие записи Bitrix24. Запись, изменённая более новой фиксацией, отбрасывается."""
        store = SessionStore(chat_id, self._data_dir)
        last = store.load_session().last_commit
        if last is None:
            raise CommitRefused("nothing to retry")
        if not last.crm_pending:
            return last
        by_tab: dict[str, list[dict]] = {}
        for item in last.crm_pending:
            by_tab.setdefault(item["tab"], []).append(item)

        remaining: list[dict] = []
        errors: list[str] = []
        for tab, items in by_tab.items():
            async with self._locks.tab_lock(tab):
                try:
                    snap = (await asyncio.to_thread(self._gateway.load_snapshots, [tab]))[tab]
                except SheetsError as exc:
                    remaining += items
                    errors.append(str(exc))
                    continue
                live = [i for i in items if not _superseded(i, snap)]
                failed, errs = await self._send_crm(live)
                remaining += failed
                errors += errs

        result = last.model_copy(update={"crm_ok": not remaining, "crm_pending": remaining, "errors": errors})

        def _save(s) -> None:
            if s.last_commit is not None and s.last_commit.draft_id == last.draft_id:
                s.last_commit = result

        store.update_session(_save)
        return result

    def _audit(self, store: SessionStore, site: Site, draft: Draft, cells: list[dict], result: CommitResult) -> None:
        append_jsonl(
            self._audit_path,
            {
                "ts": utcnow_iso(),
                "chat_id": store.chat_id,
                "site": site.name,
                "day": draft.day.isoformat(),
                "draft_id": draft.draft_id,
                "version": draft.version,
                "values": [{"position_id": c["position_id"], "name": c["name"], "value": c["final"]} for c in cells],
                "sheets_ok": result.sheets_ok,
                "crm_ok": result.crm_ok,
                "errors": result.errors,
            },
        )


def _superseded(item: dict, snap: CatalogSnapshot) -> bool:
    """True, если таблица больше не содержит значение, которое отправила бы эта ожидающая запись."""
    entry = item["entry"]
    pos = next((p for p in snap.positions if p.id == item["position_id"]), None)
    if pos is None or pos.name != entry["position_name"]:
        return True
    cur = pos.values.get(date.fromisoformat(entry["day"]).isoformat())
    if item["op"] == "delete":
        return cur is not None
    return not _same(cur, entry["value"])
