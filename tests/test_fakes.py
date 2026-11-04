from datetime import date

import pytest

from src.domain.sheet_layout import parse_grid
from src.interfaces.errors import CrmError, SheetsError
from src.models import CellWrite, CrmEntry, CrmSite


def _name(gw, tab, row):
    return next(p.name for p in gw.load_snapshots([tab])[tab].positions if p.id == row)


def test_round_trip_on_two_tabs(sheet_gateway):
    day = date(2026, 8, 27)  # новая дата между двумя существующими
    name = _name(sheet_gateway, "Садик", 17)
    sheet_gateway.write_cells("Садик", day, [CellWrite(position_id=17, expected_name=name, value=7)])
    snaps = sheet_gateway.load_snapshots(["Садик", "Школа"])
    assert snaps["Садик"].positions[[p.id for p in snaps["Садик"].positions].index(17)].values["2026-08-27"] == 7
    assert "2026-08-27" not in snaps["Школа"].date_columns  # другая вкладка не тронута
    cols = list(snaps["Садик"].date_columns)
    assert cols == sorted(cols)  # даты остаются по порядку
    assert snaps["Садик"].date_columns["2026-08-27"] == "P"


def test_none_empties_a_cell(sheet_gateway):
    name = _name(sheet_gateway, "Садик", 17)
    day = date(2026, 8, 21)  # существующая колонка, в строке 17 стоит 15
    sheet_gateway.write_cells("Садик", day, [CellWrite(position_id=17, expected_name=name, value=None)])
    snap = sheet_gateway.load_snapshots(["Садик"])["Садик"]
    p = next(p for p in snap.positions if p.id == 17)
    assert "2026-08-21" not in p.values and p.done_total == 125.0


def test_shifted_insert_keeps_old_values_and_extends_formulas(sheet_gateway):
    sheet_gateway.ensure_date_column("Садик", date(2026, 8, 1))
    snap = sheet_gateway.load_snapshots(["Садик"])["Садик"]
    assert snap.date_columns["2026-08-01"] == "J" and snap.date_columns["2026-08-19"] == "K"
    assert next(p for p in snap.positions if p.id == 17).done_total == 140.0
    assert sheet_gateway.cell("Садик", 5, "I") == "=SUM(J5:DA5)"


def test_wrong_expected_name_refuses_the_whole_write(sheet_gateway):
    good = _name(sheet_gateway, "Садик", 17)
    before = sheet_gateway.load_snapshots(["Садик"])["Садик"]
    with pytest.raises(SheetsError):
        sheet_gateway.write_cells(
            "Садик",
            date(2026, 10, 5),
            [
                CellWrite(position_id=17, expected_name=good, value=1),
                CellWrite(position_id=18, expected_name="другое", value=1),
            ],
        )
    after = sheet_gateway.load_snapshots(["Садик"])["Садик"]
    assert after.date_columns == before.date_columns  # новая колонка тоже не создана
    assert after.positions == before.positions


def test_unknown_tab_is_a_typed_error(sheet_gateway):
    with pytest.raises(SheetsError):
        sheet_gateway.load_snapshots(["нет такой"])


def test_fail_n_times_then_recover(sheet_gateway):
    sheet_gateway.fail("list_site_tabs", times=2)
    for _ in range(2):
        with pytest.raises(SheetsError):
            sheet_gateway.list_site_tabs()
    assert sheet_gateway.list_site_tabs() == ["Садик", "Школа"]


def test_failure_after_insert_does_not_duplicate_the_column_on_retry(sheet_gateway):
    day = date(2026, 8, 27)
    sheet_gateway.fail("ensure_date_column", times=1, after_apply=True)
    with pytest.raises(SheetsError):
        sheet_gateway.ensure_date_column("Садик", day)
    letter = sheet_gateway.ensure_date_column("Садик", day)  # повтор
    snap = parse_grid(sheet_gateway.grids["Садик"])
    assert letter == "P" and len(snap.date_columns) == 40
    assert list(snap.date_columns).count("2026-08-27") == 1


def test_call_log_records_calls(sheet_gateway):
    sheet_gateway.list_site_tabs()
    sheet_gateway.sheet_url("Садик")
    assert [c[0] for c in sheet_gateway.calls] == ["list_site_tabs"]
    assert sheet_gateway.sheet_url("Садик").endswith("Садик")


def _entry(key="1|x", value=2.0, site="1"):
    return CrmEntry(
        site_name="Садик", crm_value_id=site, day=date(2026, 11, 3), position_key=key, position_name="x", value=value
    )


def test_crm_upsert_is_idempotent_and_delete_removes(crm_sink):
    crm_sink.upsert_entries([_entry()])
    crm_sink.upsert_entries([_entry(value=5.0)])
    assert len(crm_sink.entries) == 1 and next(iter(crm_sink.entries.values())).value == 5.0
    crm_sink.delete_entries([_entry(), _entry(key="9|none")])  # отсутствующий элемент не считается ошибкой
    assert crm_sink.entries == {}


def test_crm_sites_are_configurable_and_keys_are_per_site():
    from src.interfaces.fakes import FakeCrmSink

    sink = FakeCrmSink([CrmSite(value_id="9", name="Гараж")])
    assert [s.name for s in sink.list_sites()] == ["Гараж"]
    sink.upsert_entries([_entry(site="1"), _entry(site="2")])
    assert len(sink.entries) == 2


def test_crm_failure_modes(crm_sink):
    crm_sink.fail("upsert_entries", times=1)
    with pytest.raises(CrmError):
        crm_sink.upsert_entries([_entry()])
    assert crm_sink.entries == {}  # сбой до применения ничего не меняет
    crm_sink.fail("upsert_entries", times=1, after_apply=True)
    with pytest.raises(CrmError):
        crm_sink.upsert_entries([_entry()])
    assert len(crm_sink.entries) == 1  # случай 5xx после применения
    assert [c[0] for c in crm_sink.calls] == ["upsert_entries", "upsert_entries"]
