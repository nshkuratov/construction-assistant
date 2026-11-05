from datetime import timedelta

from src.services.sites import LiveSiteDirectory, site_id_for
from src.services.snapshots import SnapshotService
from src.session.store import SessionStore
from src.utils.timeutil import now_utc


def _svc(sheet_gateway, crm_sink, tmp_path, max_age_s=60):
    directory = LiveSiteDirectory(sheet_gateway, crm_sink, max_age_s=0)
    return SnapshotService(sheet_gateway, directory, data_dir=str(tmp_path), max_age_s=max_age_s)


def _loads(gw):
    return [args for name, args in gw.calls if name == "load_snapshots"]


async def test_refresh_reads_once_for_all_stale_sites_and_writes_files(sheet_gateway, crm_sink, tmp_path):
    svc = _svc(sheet_gateway, crm_sink, tmp_path)
    ids = [site_id_for("Садик"), site_id_for("Школа")]
    result = await svc.refresh(7, ids)
    assert set(result) == set(ids)
    assert _loads(sheet_gateway) == [(("Садик", "Школа"),)]
    store = SessionStore(7, str(tmp_path))
    snap = store.read_snapshot(ids[0])
    assert snap is not None and snap.site_id == ids[0]
    assert snap.positions


async def test_fresh_snapshots_are_skipped_and_force_reads_again(sheet_gateway, crm_sink, tmp_path):
    svc = _svc(sheet_gateway, crm_sink, tmp_path)
    sid = site_id_for("Садик")
    await svc.refresh(7, [sid])
    await svc.refresh(7, [sid])
    assert len(_loads(sheet_gateway)) == 1
    await svc.refresh(7, [sid], force=True)
    assert len(_loads(sheet_gateway)) == 2


async def test_stale_snapshot_is_read_again_and_only_the_stale_site(sheet_gateway, crm_sink, tmp_path):
    svc = _svc(sheet_gateway, crm_sink, tmp_path)
    a, b = site_id_for("Садик"), site_id_for("Школа")
    await svc.refresh(7, [a, b])
    store = SessionStore(7, str(tmp_path))
    old = store.read_snapshot(a).model_copy(update={"taken_at": now_utc() - timedelta(seconds=300)})
    store.write_snapshot(old)
    sheet_gateway.calls.clear()
    await svc.refresh(7, [a, b])
    assert _loads(sheet_gateway) == [(("Садик",),)]


async def test_unknown_site_is_skipped_and_empty_list_reads_nothing(sheet_gateway, crm_sink, tmp_path):
    svc = _svc(sheet_gateway, crm_sink, tmp_path)
    assert await svc.refresh(7, ["deadbeef"]) == {}
    assert await svc.refresh(7, []) == {}
    assert _loads(sheet_gateway) == []
