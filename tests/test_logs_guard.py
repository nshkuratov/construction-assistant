"""Страж набора тестов: ни один тест не должен трогать настоящий ``logs/``."""

from tests.conftest import snapshot_logs


def test_snapshot_detects_changed_new_and_removed_files(tmp_path):
    (tmp_path / "a.jsonl").write_text("1\n")
    before = snapshot_logs(tmp_path)
    (tmp_path / "a.jsonl").write_text("1\n2\n")
    (tmp_path / "b.jsonl").write_text("x\n")
    assert snapshot_logs(tmp_path) != before
    assert set(snapshot_logs(tmp_path)) == {"a.jsonl", "b.jsonl"}


def test_snapshot_of_a_missing_dir_is_empty(tmp_path):
    assert snapshot_logs(tmp_path / "nope") == {}


def test_commit_service_default_audit_goes_to_tmp(tmp_path):
    from src.interfaces.fakes import FakeCrmSink, FakeSheetGateway
    from src.services.commit import CommitService

    service = CommitService(FakeSheetGateway([["x"]], ["T"]), FakeCrmSink(), None)
    assert str(tmp_path) in service._audit_path
