import asyncio
import json
import re
import time
from datetime import date
from pathlib import Path

import pytest

from src.interfaces.fakes import FakeSheetGateway
from src.models import CellWrite, ChatSession, Draft, DraftLine
from src.services.commit import CommitRefused, CommitService
from src.services.sites import LiveSiteDirectory, site_id_for
from src.session.store import SessionStore

DAY = date(2026, 9, 10)  # существующая колонка даты
TAB = "Садик"
SITE = site_id_for(TAB)


def _service(gw, crm, tmp_path):
    directory = LiveSiteDirectory(gw, crm, max_age_s=0)
    return CommitService(gw, crm, directory, data_dir=str(tmp_path), audit_path=str(tmp_path / "commits.jsonl"))


def _line(i, pid, mode, value=None):
    return DraftLine(line_id=i, position_id=pid, mode=mode, value=value)


def _put_draft(tmp_path, chat_id, lines, day=DAY, draft_id="d1", version=1, tab_site=SITE):
    store = SessionStore(chat_id, str(tmp_path))
    draft = Draft(draft_id=draft_id, version=version, site_id=tab_site, day=day, lines=lines)
    previous = store.load_session()
    store.save_session(ChatSession(chat_id=chat_id, site_id=tab_site, draft=draft, last_commit=previous.last_commit))
    return store


def _value(gw, tab, pid, day=DAY):
    snap = gw.load_snapshots([tab])[tab]
    return next(p for p in snap.positions if p.id == pid).values.get(day.isoformat())


def _write(gw, pid, value, tab=TAB, day=DAY):
    name = next(p.name for p in gw.load_snapshots([tab])[tab].positions if p.id == pid)
    gw.write_cells(tab, day, [CellWrite(position_id=pid, expected_name=name, value=value)])


def _crm_values(crm):
    return {k[2]: e.value for k, e in crm.entries.items()}


def _calls(gw, name):
    return [c for c, _ in gw.calls if c == name]


async def test_add_set_clear_in_one_draft_reach_both_sinks(sheet_gateway, crm_sink, tmp_path):
    _write(sheet_gateway, 5, 1.0)
    _write(sheet_gateway, 7, 4.0)
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0), _line(2, 6, "set", 3.0), _line(3, 7, "clear")])
    first = await svc.commit(1, "d1", 1)
    assert first.sheets_ok and first.crm_ok and first.crm_pending == []
    assert _value(sheet_gateway, TAB, 5) == 3.0
    assert _value(sheet_gateway, TAB, 6) == 3.0
    assert _value(sheet_gateway, TAB, 7) is None
    values = _crm_values(crm_sink)
    assert sorted(values.values()) == [3.0, 3.0]
    assert any(c == "delete_entries" for c, _ in crm_sink.calls)
    session = SessionStore(1, str(tmp_path)).load_session()
    assert session.draft.status == "committed" and session.last_commit == first
    assert not (SessionStore(1, str(tmp_path)).dir / "commit_record.json").exists()


async def test_clear_deletes_an_existing_crm_item(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 7, "set", 4.0)])
    await svc.commit(1, "d1", 1)
    assert len(crm_sink.entries) == 1
    _put_draft(tmp_path, 1, [_line(1, 7, "clear")], draft_id="d2")
    await svc.commit(1, "d2", 1)
    assert crm_sink.entries == {}
    assert _value(sheet_gateway, TAB, 7) is None


async def test_two_lines_on_one_position_are_folded_once(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0), _line(2, 5, "add", 3.0)])
    await svc.commit(1, "d1", 1)
    assert _value(sheet_gateway, TAB, 5) == 5.0


async def test_new_date_between_two_existing_dates(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    before = date(2026, 8, 21)
    _write(sheet_gateway, 5, 7.0, day=before)
    day = date(2026, 8, 22)
    assert sheet_gateway.column_of(TAB, day) is None
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)], day=day)
    result = await svc.commit(1, "d1", 1)
    assert result.sheets_ok and result.crm_ok
    assert sheet_gateway.column_of(TAB, day) is not None
    assert _value(sheet_gateway, TAB, 5, day) == 2.0
    assert _value(sheet_gateway, TAB, 5, before) == 7.0
    assert list(crm_sink.entries)[0][1] == day


@pytest.mark.parametrize("method", ["write_cells", "ensure_date_column"])
async def test_sheets_failure_leaves_crm_untouched_and_draft_pending(sheet_gateway, crm_sink, tmp_path, method):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    store = _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    sheet_gateway.fail(method)
    result = await svc.commit(1, "d1", 1)
    assert not result.sheets_ok and not result.crm_ok and result.errors
    assert crm_sink.entries == {} and not [c for c, _ in crm_sink.calls if c != "list_sites"]
    assert store.load_session().draft.status == "pending"
    again = await svc.commit(1, "d1", 1)
    assert again.sheets_ok and again.crm_ok
    assert _value(sheet_gateway, TAB, 5) == 2.0


async def test_crm_failure_keeps_the_sheet_and_retry_completes_it(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0), _line(2, 6, "set", 3.0)])
    crm_sink.fail("upsert_entries")
    result = await svc.commit(1, "d1", 1)
    assert result.sheets_ok and not result.crm_ok
    assert len(result.crm_pending) == 2 and crm_sink.entries == {}
    assert _value(sheet_gateway, TAB, 5) == 2.0
    retried = await svc.retry_crm(1)
    assert retried.crm_ok and retried.crm_pending == []
    assert sorted(_crm_values(crm_sink).values()) == [2.0, 3.0]
    assert _value(sheet_gateway, TAB, 5) == 2.0  # повтор не трогает таблицу
    assert SessionStore(1, str(tmp_path)).load_session().last_commit.crm_ok


async def test_retry_crm_keeps_failing_entries_pending(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    crm_sink.fail("upsert_entries", times=2)
    await svc.commit(1, "d1", 1)
    still = await svc.retry_crm(1)
    assert not still.crm_ok and len(still.crm_pending) == 1 and still.errors
    done = await svc.retry_crm(1)
    assert done.crm_ok


async def test_retry_without_a_commit_is_refused(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    with pytest.raises(CommitRefused):
        await svc.retry_crm(1)


async def test_retry_drops_an_entry_that_another_chat_superseded(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 5.0)])
    crm_sink.fail("upsert_entries")
    await svc.commit(1, "d1", 1)  # чат 1: в таблице 5, CRM ещё не записана
    _put_draft(tmp_path, 2, [_line(1, 5, "set", 9.0)])
    await svc.commit(2, "d1", 1)  # чат 2 перезаписал ту же ячейку
    assert list(_crm_values(crm_sink).values()) == [9.0]
    upserts_before = len([c for c, _ in crm_sink.calls if c == "upsert_entries"])
    retried = await svc.retry_crm(1)
    assert retried.crm_ok and retried.crm_pending == []
    assert len([c for c, _ in crm_sink.calls if c == "upsert_entries"]) == upserts_before
    assert list(_crm_values(crm_sink).values()) == [9.0]


async def test_retry_drops_a_pending_delete_when_the_cell_was_filled_again(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 7, "set", 4.0)])
    await svc.commit(1, "d1", 1)
    _put_draft(tmp_path, 1, [_line(1, 7, "clear")], draft_id="d2")
    crm_sink.fail("delete_entries")
    cleared = await svc.commit(1, "d2", 1)
    assert not cleared.crm_ok and len(cleared.crm_pending) == 1
    _put_draft(tmp_path, 2, [_line(1, 7, "set", 6.0)])
    await svc.commit(2, "d1", 1)
    retried = await svc.retry_crm(1)
    assert retried.crm_ok
    assert list(_crm_values(crm_sink).values()) == [6.0]  # более новый элемент не удалён


async def test_pending_entries_of_an_earlier_commit_carry_over(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    crm_sink.fail("upsert_entries")
    await svc.commit(1, "d1", 1)
    _put_draft(tmp_path, 1, [_line(1, 6, "add", 1.0)], draft_id="d2")
    second = await svc.commit(1, "d2", 1)
    assert second.sheets_ok and not second.crm_ok and len(second.crm_pending) == 1
    done = await svc.retry_crm(1)
    assert done.crm_ok and sorted(_crm_values(crm_sink).values()) == [1.0, 2.0]


async def test_double_commit_writes_once(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    a, b = await asyncio.gather(svc.commit(1, "d1", 1), svc.commit(1, "d1", 1))
    c = await svc.commit(1, "d1", 1)
    assert a == b == c
    assert _value(sheet_gateway, TAB, 5) == 2.0
    assert len(_calls(sheet_gateway, "write_cells")) == 1
    assert len([x for x, _ in crm_sink.calls if x == "upsert_entries"]) == 1


class SlowGateway(FakeSheetGateway):
    """Настоящее чтение занимает время; без блокировки вкладки оба коммита прочитают старое значение."""

    def load_snapshots(self, tabs):
        snaps = super().load_snapshots(tabs)
        time.sleep(0.05)
        return snaps


async def test_concurrent_commits_on_one_tab_lose_no_update(sample_grid, crm_sink, tmp_path):
    gw = SlowGateway(sample_grid, [TAB, "Школа"])
    svc = _service(gw, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 5.0)])
    _put_draft(tmp_path, 2, [_line(1, 5, "add", 5.0)])
    await asyncio.gather(svc.commit(1, "d1", 1), svc.commit(2, "d1", 1))
    assert _value(gw, TAB, 5) == 10.0
    assert list(_crm_values(crm_sink).values()) == [10.0]


async def test_stale_version_and_unknown_draft_are_refused(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)], version=3)
    with pytest.raises(CommitRefused, match="stale"):
        await svc.commit(1, "d1", 2)
    with pytest.raises(CommitRefused, match="no such draft"):
        await svc.commit(1, "other", 3)
    with pytest.raises(CommitRefused, match="no such draft"):
        await svc.commit(99, "d1", 3)
    assert _calls(sheet_gateway, "write_cells") == []


async def test_unresolved_empty_and_gone_site_are_refused(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [DraftLine(line_id=1, position_id=None, mode="add", value=1.0, candidates=[5, 6])])
    with pytest.raises(CommitRefused, match="unresolved"):
        await svc.commit(1, "d1", 1)
    _put_draft(tmp_path, 1, [])
    with pytest.raises(CommitRefused, match="empty"):
        await svc.commit(1, "d1", 1)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 1.0)], tab_site="00000000")
    with pytest.raises(CommitRefused, match="site"):
        await svc.commit(1, "d1", 1)
    _put_draft(tmp_path, 1, [_line(1, 9999, "add", 1.0)])
    with pytest.raises(CommitRefused, match="position"):
        await svc.commit(1, "d1", 1)
    assert _calls(sheet_gateway, "write_cells") == []


async def test_error_applied_after_the_write_does_not_double_add(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    sheet_gateway.fail("write_cells", after_apply=True)
    failed = await svc.commit(1, "d1", 1)
    assert not failed.sheets_ok and _value(sheet_gateway, TAB, 5) == 2.0
    ok = await svc.commit(1, "d1", 1)
    assert ok.sheets_ok and _value(sheet_gateway, TAB, 5) == 2.0


async def test_crash_between_the_write_and_the_commit_mark_does_not_double_add(
    sheet_gateway, crm_sink, tmp_path, monkeypatch
):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    store = _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    real = CommitService._mark_committed
    calls = {"n": 0}

    def crash_once(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process killed")
        return real(self, *a, **kw)

    monkeypatch.setattr(CommitService, "_mark_committed", crash_once)
    with pytest.raises(RuntimeError):
        await svc.commit(1, "d1", 1)
    assert _value(sheet_gateway, TAB, 5) == 2.0
    assert store.load_session().draft.status == "pending"
    result = await svc.commit(1, "d1", 1)
    assert result.sheets_ok and result.crm_ok
    assert _value(sheet_gateway, TAB, 5) == 2.0
    assert list(_crm_values(crm_sink).values()) == [2.0]


async def test_a_stale_record_never_overwrites_another_chats_volume(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    sheet_gateway.fail("write_cells")  # чат 1 падает до применения; его запись остаётся
    await svc.commit(1, "d1", 1)
    _put_draft(tmp_path, 2, [_line(1, 5, "add", 5.0)])
    await svc.commit(2, "d1", 1)
    result = await svc.commit(1, "d1", 1)
    assert result.sheets_ok
    assert _value(sheet_gateway, TAB, 5) == 7.0  # 5 из чата 2 + 2 из чата 1, ничего не потеряно


async def test_every_commit_result_is_audited_failures_included(sheet_gateway, crm_sink, tmp_path):
    svc = _service(sheet_gateway, crm_sink, tmp_path)
    _put_draft(tmp_path, 1, [_line(1, 5, "add", 2.0)])
    sheet_gateway.fail("write_cells")
    await svc.commit(1, "d1", 1)
    await svc.commit(1, "d1", 1)
    lines = [json.loads(x) for x in (tmp_path / "commits.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["sheets_ok"] for x in lines] == [False, True]
    assert lines[1]["values"][0]["value"] == 2.0 and lines[1]["site"] == TAB and lines[1]["chat_id"] == 1


def test_only_the_commit_service_calls_the_write_methods():
    src = Path(__file__).resolve().parent.parent / "src"
    # место вызова или переданная ссылка (to_thread(x.write_cells, ...)); не `def` и не слово из докстринга
    ref = re.compile(r"\.(write_cells|upsert_entries|delete_entries)\b")
    skip = {src / "services" / "commit.py", src / "interfaces" / "fakes.py"}
    offenders = []
    for path in src.rglob("*.py"):
        if path in skip or "adapters" in path.parts:
            continue
        for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ref.search(text):
                offenders.append(f"{path.relative_to(src)}:{n}")
    assert offenders == []
