import json
import os
import subprocess
import sys

import pytest

from src.config import Config
from src.mcp.cli import dispatch


def call(tool, **args):
    return dispatch(tool, args)


def line(position_id, value=10, **kw):
    return {"position_id": position_id, "mode": "add", "value": value, "source_text": "x", **kw}


def test_get_session_context(chat):
    env = call("get_session")
    assert env["site_id"] == "sad" and env["site_name"] == "Садик" and env["today"] == "2026-09-02"
    assert env["sections"] == ["ОВ 3", "ЭМ3"] and env["draft"] is None


def test_get_session_without_site_still_answers(chat):
    chat.update_session(lambda s: setattr(s, "site_id", None))
    env = call("get_session")
    assert env["ok"] and env["site_name"] is None and env["sections"] == []


def test_select_site_by_name_number_and_typo(chat):
    assert call("select_site", site="Школа")["site"]["site_id"] == "shk"
    assert chat.load_session().site_id == "shk"
    assert call("select_site", site="1")["site"]["site_id"] == "sad"
    assert call("select_site", site="школу")["site"]["site_id"] == "shk"


def test_select_site_unknown_and_ambiguous(chat):
    assert call("select_site", site="Больница")["error"]["type"] == "not_found"
    (chat.dir / "sites.json").write_text(
        '[{"site_id":"sad","name":"Школа 1","sheet_tab":"a","crm_value_id":"1"},'
        '{"site_id":"shk","name":"Школа 2","sheet_tab":"b","crm_value_id":"2"}]',
        encoding="utf-8",
    )
    env = call("select_site", site="Школа")
    assert env["selected"] is False and len(env["candidates"]) == 2
    assert chat.load_session().site_id == "sad"


def test_propose_draft_stores_validated_lines(chat):
    env = call("propose_draft", lines=[line(17, 15), line(55, 20, unit="м")], day="2026-09-01")
    d = env["draft"]
    assert d["day"] == "2026-09-01" and d["day_source"] == "message" and d["version"] == 1
    assert [ln["position_id"] for ln in d["lines"]] == [17, 55]
    assert chat.load_session().draft.draft_id == d["draft_id"]


def test_default_day_source(chat):
    d = call("propose_draft", lines=[line(17)])["draft"]
    assert d["day"] == "2026-09-02" and d["day_source"] == "default"


def test_unit_mismatch_is_rejected(chat):
    env = call("propose_draft", lines=[line(17, 5, unit="кг")])
    assert env["ok"] is False and env["error"]["type"] == "unit_mismatch"
    assert chat.load_session().draft is None


def test_over_estimate_warning_in_add_mode(chat):
    env = call("propose_draft", lines=[line(17, 130)])  # сделано 140 из 265, осталось 125
    assert len(env["warnings"]) == 1 and "осталось 125" in env["warnings"][0]
    assert call("propose_draft", lines=[line(17, 125)])["warnings"] == []


def test_over_estimate_warning_in_set_mode_ignores_the_day_value(chat):
    # за день 2026-08-31 записано 53 из 140: установка 190 даёт 87 + 190 > 265, установка 178 даёт ровно 265
    over = {"position_id": 17, "mode": "set", "value": 179}
    ok = {"position_id": 17, "mode": "set", "value": 178}
    assert len(call("propose_draft", lines=[over], day="2026-08-31")["warnings"]) == 1
    assert call("propose_draft", lines=[ok], day="2026-08-31")["warnings"] == []


def test_clear_has_no_warning_and_no_value(chat):
    env = call("propose_draft", lines=[{"position_id": 17, "mode": "clear"}], day="2026-08-31")
    assert env["warnings"] == [] and env["draft"]["lines"][0]["value"] is None
    assert (
        call("propose_draft", lines=[{"position_id": 17, "mode": "clear", "value": 5}])["error"]["type"] == "validation"
    )


@pytest.mark.parametrize(
    "bad",
    [line(9999), line(17, 0), line(17, -3), {"position_id": 17, "mode": "add"}, {"mode": "add", "value": 5}],
)
def test_bad_lines_are_rejected(chat, bad):
    assert call("propose_draft", lines=[bad])["ok"] is False


def test_duplicate_position_and_empty_lines_are_rejected(chat):
    assert call("propose_draft", lines=[line(17), line(17)])["error"]["type"] == "validation"
    assert call("propose_draft", lines=[])["error"]["type"] == "validation"


def test_unresolved_line_keeps_candidates(chat):
    env = call("propose_draft", lines=[{"mode": "add", "value": 8, "source_text": "сверление", "candidates": [48, 49]}])
    assert env["draft"]["lines"][0]["candidates"] == [48, 49]
    assert (
        call("propose_draft", lines=[{"mode": "add", "value": 8, "candidates": [48, 7777]}])["error"]["type"]
        == "not_found"
    )


def test_new_version_keeps_id_and_replace_false_merges(chat):
    first = call("propose_draft", lines=[line(17), line(55)], day="2026-09-01")["draft"]
    merged = call("propose_draft", lines=[line(17, 99)], replace=False)["draft"]
    assert merged["draft_id"] == first["draft_id"] and merged["version"] == 2 and merged["day"] == "2026-09-01"
    assert {ln["position_id"]: ln["value"] for ln in merged["lines"]} == {55: 10.0, 17: 99.0}
    replaced = call("propose_draft", lines=[line(18)])["draft"]
    assert [ln["position_id"] for ln in replaced["lines"]] == [18] and replaced["version"] == 3


def test_propose_draft_resets_ui_and_clears_clarification(chat):
    call("ask_clarification", question="Какой?", options=[])
    chat.update_session(lambda s: setattr(s.ui, "view", "line_edit"))
    call("propose_draft", lines=[line(17)])
    s = chat.load_session()
    assert s.clarification is None and s.ui.view == "card"


def test_ask_clarification_refuses_position_only_options(chat):
    res = call(
        "ask_clarification",
        question="Какое сверление?",
        source_text="сверление Д80 — 3 шт",
        options=[{"label": "Д80 №44", "position_id": 48}, {"label": "Д80 №45", "position_id": 49}],
    )
    assert res["error"]["type"] == "validation" and "propose_draft" in res["error"]["message"]
    assert chat.load_session().clarification is None


def test_ask_clarification_validates_ids(chat):
    ok = call(
        "ask_clarification",
        question="Какое сверление?",
        options=[
            {"label": "Д80 №44", "position_id": 48},
            {"label": "Нет в смете"},
            {"label": "вчера", "day": "2026-09-01"},
        ],
    )
    assert ok["ok"] and chat.load_session().clarification.options[0].position_id == 48
    assert (
        call("ask_clarification", question="q", options=[{"label": "a", "position_id": 1}])["error"]["type"]
        == "not_found"
    )
    assert (
        call("ask_clarification", question="q", options=[{"label": "a", "site_id": "zzz"}])["error"]["type"]
        == "not_found"
    )
    assert (
        call("ask_clarification", question="q", options=[{"label": "a", "day": "вчера"}])["error"]["type"]
        == "validation"
    )


def test_send_report_form_is_built_from_the_catalog(chat):
    text = call("send_report_form")["form"]
    assert "Садик" in text and "ОВ 3" in text and "ЭМ3" in text
    assert text.count("•") == 4 and "{" not in text


def test_report_friction_appends_jsonl(chat):
    call("report_friction", summary="не понял «шпилька»", details="d")
    call("report_friction", summary="second")
    rows = [json.loads(x) for x in open(Config.FRICTION_LOG, encoding="utf-8")]
    assert [r["summary"] for r in rows] == ["не понял «шпилька»", "second"] and rows[0]["chat_id"] == 42


def test_finish_turn_draft_needs_a_draft_from_this_turn(chat):
    chat.begin_turn()
    assert call("finish_turn", outcome="draft", message="ok")["error"]["type"] == "no_draft"
    call("propose_draft", lines=[line(17)])
    assert call("finish_turn", outcome="draft", message="ok")["ok"]
    assert chat.load_session().turn_outcome.outcome == "draft"


def test_old_draft_does_not_satisfy_a_new_turn(chat):
    chat.begin_turn()
    call("propose_draft", lines=[line(17)])
    chat.begin_turn()
    assert call("finish_turn", outcome="draft")["error"]["type"] == "no_draft"


def test_second_finish_turn_in_one_turn_fails(chat):
    chat.begin_turn()
    assert call("finish_turn", outcome="answer", message="осталось 70 м")["ok"]
    assert call("finish_turn", outcome="answer", message="again")["error"]["type"] == "already_finished"
    chat.begin_turn()
    assert call("finish_turn", outcome="answer", message="new turn")["ok"]


def test_finish_turn_question_needs_something_to_ask(chat):
    chat.begin_turn()
    assert call("finish_turn", outcome="question", message="?")["error"]["type"] == "no_question"
    call("ask_clarification", question="Какой объект?", options=[])
    assert call("finish_turn", outcome="question", message="Какой объект?")["ok"]


def test_finish_turn_question_accepts_an_unresolved_line(chat):
    chat.begin_turn()
    call("propose_draft", lines=[{"mode": "add", "value": 8, "candidates": [48, 49]}])
    assert call("finish_turn", outcome="question", message="Какое?")["ok"]


def test_finish_turn_form_and_message_rules(chat):
    chat.begin_turn()
    assert call("finish_turn", outcome="form")["error"]["type"] == "no_form"
    assert call("finish_turn", outcome="answer", message="  ")["error"]["type"] == "validation"
    form = call("send_report_form")["form"]
    assert call("finish_turn", outcome="form")["ok"]
    assert chat.load_session().turn_outcome.message == form


def test_real_cli_run_reads_back_the_session(chat):
    """Настоящий путь: ``python -m src.mcp.cli`` в новом процессе, затем чтение session.json."""
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    env.update({
        "CA_CHAT_DIR": Config.CA_CHAT_DIR,
        "CA_TZ": "Europe/Moscow",
        "CA_SKIP_DOTENV": "1",
        "PYTHONUTF8": "1",
    })
    out = subprocess.run(
        [sys.executable, "-m", "src.mcp.cli", "propose_draft", "--json", json.dumps({"lines": [line(17, 15)]})],
        capture_output=True, text=True, encoding="utf-8", env={**env, "RUN_TELEMETRY_LOG": os.devnull}, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout)["ok"] is True, out.stdout + out.stderr
    stored = json.loads((chat.dir / "session.json").read_text(encoding="utf-8"))
    assert stored["draft"]["lines"][0]["position_id"] == 17


def test_ask_clarification_refuses_an_invented_option(chat):
    res = call(
        "ask_clarification",
        question="Какой бетон?",
        options=[{"label": "Укладка бетона"}, {"label": "Бетонирование конструкции"}],
    )
    assert res["error"]["type"] == "validation" and "Нет в смете" in res["error"]["message"]
    assert chat.load_session().clarification is None


def test_ask_clarification_keeps_site_day_and_the_not_in_estimate_label(chat):
    res = call(
        "ask_clarification",
        question="Что делать?",
        options=[
            {"label": "вчера", "day": "2026-09-01"},
            {"label": "Школа", "site_id": "shk"},
            {"label": "Нет в смете"},
        ],
    )
    assert res["ok"]
    assert [o.label for o in chat.load_session().clarification.options] == ["вчера", "Школа", "Нет в смете"]


def test_ask_clarification_drops_the_free_label_that_the_card_adds_itself(chat):
    res = call(
        "ask_clarification",
        question="Какой день?",
        options=[{"label": "вчера", "day": "2026-09-01"}, {"label": "Другое"}],
    )
    assert res["ok"]
    assert [o.label for o in chat.load_session().clarification.options] == ["вчера"]


def test_propose_draft_stores_skipped_items(chat):
    skipped = [{"source_text": "бетон 5 кубов", "reason": "нет в смете"}]
    d = call("propose_draft", lines=[line(17)], skipped=skipped)["draft"]
    assert d["skipped"] == skipped
    assert chat.load_session().draft.skipped[0].source_text == "бетон 5 кубов"


def test_propose_draft_keeps_skipped_when_not_replacing_and_replaces_otherwise(chat):
    call("propose_draft", lines=[line(17)], skipped=[{"source_text": "бетон", "reason": "нет в смете"}])
    kept = call("propose_draft", lines=[line(55)], replace=False)["draft"]
    assert [x["source_text"] for x in kept["skipped"]] == ["бетон"]
    fresh = call("propose_draft", lines=[line(55)], replace=True)["draft"]
    assert fresh["skipped"] == []


def test_propose_draft_without_lines_is_refused_even_with_skipped(chat):
    env = call("propose_draft", lines=[], skipped=[{"source_text": "бетон", "reason": "нет в смете"}])
    assert env["error"]["type"] == "validation"
