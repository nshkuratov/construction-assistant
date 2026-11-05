import json
import threading

import pytest

from src.config import Config
from src.models import CatalogSnapshot, Clarification
from src.session.store import DRAFT_ID_LEN, SessionStore, new_draft_id


@pytest.fixture
def store(tmp_path):
    return SessionStore(42, data_dir=str(tmp_path), history_turns=3)


def test_layout_is_per_chat(store, tmp_path):
    store.save_session(store.load_session())
    assert store.session_path == tmp_path / "chats" / "42" / "session.json"
    assert store.session_path.exists()


def test_missing_session_starts_empty(store):
    s = store.load_session()
    assert (s.chat_id, s.draft, s.history) == (42, None, [])


def test_history_is_trimmed_on_every_write(store):
    for i in range(6):
        store.update_session(lambda s, i=i: s.history.append({"n": i}))
    assert [h["n"] for h in store.load_session().history] == [3, 4, 5]


def test_default_history_comes_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "HISTORY_TURNS", 1)
    s = SessionStore(1, data_dir=str(tmp_path))
    s.update_session(lambda x: x.history.extend([{"a": 1}, {"a": 2}]))
    assert s.load_session().history == [{"a": 2}]


def test_write_is_atomic_and_leaves_no_temp_files(store):
    store.update_session(lambda s: setattr(s, "site_id", "abc"))
    assert [p.name for p in store.dir.iterdir() if p.suffix == ".tmp"] == []
    assert json.loads(store.session_path.read_text(encoding="utf-8"))["site_id"] == "abc"


def test_failed_mutation_keeps_the_old_file(store):
    store.update_session(lambda s: setattr(s, "site_id", "keep"))

    def boom(s):
        s.site_id = "lost"
        raise RuntimeError("fail mid-update")

    with pytest.raises(RuntimeError):
        store.update_session(boom)
    assert store.load_session().site_id == "keep"


def test_corrupt_session_file_falls_back_to_empty(store):
    store.dir.mkdir(parents=True)
    store.session_path.write_text("{not json", encoding="utf-8")
    assert store.load_session().draft is None


def test_begin_turn_gives_a_new_id_and_clears_the_outcome(store):
    first = store.begin_turn()
    second = store.begin_turn()
    assert first != second and store.load_session().turn_id == second
    assert store.load_session().turn_outcome is None


def test_concurrent_updates_do_not_lose_writes(store):
    def bump():
        for _ in range(10):
            store.update_session(lambda s: s.history.append({"x": 1}))

    store.history_turns = 1000
    threads = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store.load_session().history) == 40


def test_clarification_survives_a_round_trip(store):
    store.update_session(lambda s: setattr(s, "clarification", Clarification(question="Какой?")))
    assert store.load_session().clarification.question == "Какой?"


def test_snapshot_round_trip_and_safe_ids(store, sample_grid):
    from src.domain.sheet_layout import parse_grid

    snap = parse_grid(sample_grid, site_id="abc12345")
    store.write_snapshot(snap)
    assert store.read_snapshot("abc12345") == CatalogSnapshot.model_validate(snap.model_dump(mode="json"))
    assert store.read_snapshot("nope") is None
    with pytest.raises(ValueError):
        store.snapshot_path("../escape")


def test_from_chat_dir_opens_the_exact_directory(store):
    store.update_session(lambda s: setattr(s, "site_id", "z"))
    again = SessionStore.from_chat_dir(str(store.dir))
    assert again.load_session().site_id == "z" and again.chat_id == 42


def test_draft_ids_are_short_and_distinct():
    ids = {new_draft_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) <= DRAFT_ID_LEN for i in ids)
