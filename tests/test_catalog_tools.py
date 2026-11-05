import ast
from pathlib import Path

from src.config import Config
from src.mcp.cli import dispatch


def call(tool, **args):
    return dispatch(tool, args)


def test_no_chat_dir_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(Config, "CA_CHAT_DIR", "")
    env = call("list_sites")
    assert env["ok"] is False and env["error"]["type"] == "no_chat"


def test_list_sites_names_numbers_and_active(chat):
    env = call("list_sites")
    assert [(s["number"], s["name"], s["active"]) for s in env["sites"]] == [(1, "Садик", True), (2, "Школа", False)]


def test_find_positions_batches_queries(chat):
    env = call("find_positions", queries=["медь 6-ка", "кабель 3 на 2,5", "лопата"], limit=3)
    assert env["ok"]
    first, second, third = env["results"]
    assert first["candidates"][0]["position_id"] == 17 and not first["ambiguous"]
    assert second["candidates"][0]["position_id"] == 55
    assert third == {"query": "лопата", "candidates": [], "ambiguous": False, "found": False}


def test_find_positions_flags_identical_rows_as_ambiguous(chat):
    env = call("find_positions", queries=["сверление Д80"])
    result = env["results"][0]
    assert result["ambiguous"] and {c["position_id"] for c in result["candidates"][:2]} == {48, 49}


def test_find_positions_without_site_or_snapshot(chat):
    chat.update_session(lambda s: setattr(s, "site_id", None))
    assert call("find_positions", queries=["x"])["error"]["type"] == "no_site"
    chat.update_session(lambda s: setattr(s, "site_id", "ghost"))
    assert call("find_positions", queries=["x"])["error"]["type"] == "no_snapshot"


def test_get_position_has_computed_numbers(chat):
    p = call("get_position", position_id=17)["position"]
    assert (p["planned"], p["done_total"], p["left"], p["unit"]) == (265.0, 140.0, 125.0, "м")


def test_get_position_unknown_id(chat):
    assert call("get_position", position_id=9999)["error"]["type"] == "not_found"


def test_get_remaining_by_query_and_ids(chat):
    env = call("get_remaining", queries=["кабель 3х2,5"])
    assert env["results"][0]["position"]["left"] == 70.0  # 182 по плану - 2 x 56 сделано
    env = call("get_remaining", position_ids=[17, 18])
    assert [r["position"]["position_id"] for r in env["results"]] == [17, 18]


def test_get_remaining_ambiguous_query_returns_candidates(chat):
    r = call("get_remaining", queries=["сверление Д80"])["results"][0]
    assert r["ambiguous"] and "position" not in r and len(r["candidates"]) >= 2


def test_get_remaining_needs_exactly_one_selector(chat):
    assert call("get_remaining", queries=["x"], position_id=1)["error"]["type"] == "validation"
    assert call("get_remaining")["error"]["type"] == "validation"


def test_get_entries(chat):
    env = call("get_entries", day="2026-08-25")
    assert env["has_column"] and {e["position_id"] for e in env["entries"]} >= {17, 55}
    assert next(e for e in env["entries"] if e["position_id"] == 55)["value"] == 56.0


def test_get_entries_empty_day_and_bad_date(chat):
    env = call("get_entries", day="2026-10-10")
    assert env["entries"] == [] and env["has_column"] is False
    assert call("get_entries", day="10.10")["error"]["type"] == "validation"


def test_resolve_date_tool(chat):
    assert call("resolve_date", text="вчера")["day"] == "2026-09-01"
    future = call("resolve_date", text="05.09.2026")
    assert future["future"] and "warning" in future
    assert call("resolve_date", text="когда-нибудь")["error"]["type"] == "unparsed_date"


def test_wrappers_import_no_store_adapter():
    banned = ("gspread", "google", "bitrix", "sheets", "crm", "requests", "httpx")
    for path in Path("src/mcp/wrappers").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(b in name.lower().split(".") or b in name.lower() for b in banned), (path, name)
