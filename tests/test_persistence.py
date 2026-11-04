import logging

from src.utils.persistence import safe_load_json


def test_missing_file_returns_the_default_without_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert safe_load_json(str(tmp_path / "session.json"), default={"x": 1}) == {"x": 1}
    assert not caplog.records


def test_corrupt_file_still_warns(tmp_path, caplog):
    path = tmp_path / "session.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert safe_load_json(str(path), default=None) is None
    assert any("Failed to load" in r.message for r in caplog.records)


def test_valid_file_is_loaded(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"a": 2}', encoding="utf-8")
    assert safe_load_json(str(path)) == {"a": 2}
