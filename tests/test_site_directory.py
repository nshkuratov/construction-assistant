import pytest

from src.interfaces.errors import SheetsError
from src.interfaces.fakes import FakeCrmSink, FakeSheetGateway
from src.models import CrmSite
from src.services.sites import LiveSiteDirectory, normalise_name, site_id_for


def _dir(grid, tabs, crm_names, **kw):
    gw = FakeSheetGateway(grid, tabs)
    crm = FakeCrmSink([CrmSite(value_id=str(i + 1), name=n) for i, n in enumerate(crm_names)])
    return LiveSiteDirectory(gw, crm, **kw), gw, crm


def test_intersection_matches_names_by_case_spaces_and_yo(sample_grid):
    d, _, _ = _dir(sample_grid, ["Объект Ёлка", "Школа"], ["объект  елка", "ШКОЛА"], max_age_s=60)
    listing = d.list_sites()
    assert [s.sheet_tab for s in listing.sites] == ["Объект Ёлка", "Школа"]
    assert [s.crm_value_id for s in listing.sites] == ["1", "2"]
    assert listing.mismatches == []
    assert listing.sites[0].site_id == site_id_for("объект елка")
    assert len(listing.sites[0].site_id) == 8


def test_site_only_in_google_or_only_in_crm_is_a_mismatch(sample_grid):
    d, _, _ = _dir(sample_grid, ["Садик", "Склад"], ["Садик", "Мост"], max_age_s=60)
    listing = d.list_sites()
    assert [s.name for s in listing.sites] == ["Садик"]
    assert len(listing.mismatches) == 2
    assert any("Склад" in m for m in listing.mismatches)
    assert any("Мост" in m for m in listing.mismatches)


def test_duplicate_names_on_one_side_are_reported_not_merged(sample_grid):
    d, _, _ = _dir(sample_grid, ["Садик", "садик"], ["Садик"], max_age_s=60)
    listing = d.list_sites()
    assert len(listing.sites) == 1
    assert any("повтор" in m for m in listing.mismatches)


def test_empty_sides_give_an_empty_listing(sample_grid):
    d, _, _ = _dir(sample_grid, ["Садик"], [], max_age_s=60)
    listing = d.list_sites()
    assert listing.sites == []
    assert len(listing.mismatches) == 1


def test_cache_holds_until_max_age_then_reads_again(sample_grid):
    now = [100.0]
    d, gw, _ = _dir(sample_grid, ["Садик"], ["Садик"], max_age_s=60, clock=lambda: now[0])
    d.list_sites()
    now[0] += 59
    d.list_sites()
    assert [c for c, _ in gw.calls].count("list_site_tabs") == 1
    now[0] += 2
    d.list_sites()
    assert [c for c, _ in gw.calls].count("list_site_tabs") == 2


def test_a_failed_read_is_not_cached(sample_grid):
    d, gw, _ = _dir(sample_grid, ["Садик"], ["Садик"], max_age_s=60)
    gw.fail("list_site_tabs")
    with pytest.raises(SheetsError):
        d.list_sites()
    assert len(d.list_sites().sites) == 1


def test_normalise_name():
    assert normalise_name(" Ёж  Лес ") == "ежлес"
