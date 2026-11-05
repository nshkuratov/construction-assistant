import json
import pathlib
from datetime import date, datetime

import pytest

from src.bot.card import (
    Action,
    CancelIntent,
    CardEnv,
    CommitIntent,
    EngineIntent,
    RetryIntent,
    apply,
    apply_text,
    decode,
    encode_card,
    render,
)
from src.domain.sheet_layout import parse_grid
from src.models import ChatSession, Clarification, ClarificationOption, Draft, DraftLine, Site, UiState

GRID = json.loads((pathlib.Path(__file__).parent / "fixtures" / "sample_grid.json").read_text(encoding="utf-8"))
TODAY = date(2026, 9, 1)
DAY = "2026-08-31"  # вчера; у позиций 17 и 33 здесь есть значения
P_COPPER, P_DRAIN, P_UNIT = 17, 33, 5


def snap(site_id="s1", drop=()):
    s = parse_grid(GRID, site_id=site_id, taken_at=datetime(2026, 9, 1))
    s.positions = [p for p in s.positions if p.id not in drop]
    return s


S1 = snap("s1")
SITES = [Site(site_id="s1", name="Садик", sheet_tab="Садик", crm_value_id="1"),
         Site(site_id="s2", name="Школа", sheet_tab="Школа", crm_value_id="2")]  # fmt: skip


def env(**kw):
    base = {"sites": SITES, "today": TODAY, "load_snapshot": lambda sid: snap(sid, drop=(P_DRAIN,))}
    return CardEnv(**{**base, **kw})


def line(i, pid, mode="add", value=10.0, **kw):
    kw.setdefault("source_text", "t")
    return DraftLine(line_id=i, position_id=pid, mode=mode, value=None if mode == "clear" else value, unit="м", **kw)


def session(lines=(), kind="new", day=TODAY, **kw):
    d = Draft(draft_id="ab12cd34", site_id="s1", day=day, kind=kind, lines=list(lines))
    return ChatSession(chat_id=1, site_id="s1", draft=d, **kw)


def press(s, name, arg=None, e=None, snapshot=S1):
    """Нажимает кнопку так же, как бот: через настоящую строку callback."""
    a = Action(name, None if arg is None else str(arg), draft_id=s.draft.draft_id, version=s.draft.version)
    return apply(s, decode(encode_card(a.draft_id, a.version, name, arg)), snapshot, e or env())


def test_delete_line_2_of_3():
    s = session([line(1, 17), line(2, P_DRAIN), line(3, 18)])
    r = press(s, "del", 2)
    assert [x.line_id for x in r.session.draft.lines] == [1, 3]
    assert len(s.draft.lines) == 3  # входная сессия не изменилась


def test_minus_one_on_one_in_set_mode_becomes_clear():
    s = session([line(1, 17, "set", 1.0)], ui=UiState(view="line_edit", line_id=1))
    ln = press(s, "st", -1).session.draft.lines[0]
    assert (ln.mode, ln.value) == ("clear", None)


def test_step_to_zero_in_add_mode_removes_the_line():
    s = session([line(1, 17, "add", 1.0)], ui=UiState(view="line_edit", line_id=1))
    r = press(s, "st", -1).session
    assert r.draft.lines == [] and r.ui.view == "card"


def test_step_up_from_clear_makes_a_set_line():
    s = session([line(1, 17, "clear")], ui=UiState(view="line_edit", line_id=1))
    ln = press(s, "st", 10).session.draft.lines[0]
    assert (ln.mode, ln.value) == ("set", 10.0)


def test_add_through_browse_then_number():
    s = session()
    s = press(s, "add").session
    assert s.ui.view == "add_browse"
    s = press(s, "nav", 0).session  # раздел ОВ 3
    assert s.ui.browse_path == ["ОВ 3"]
    s = press(s, "nav", 0).session  # первая группа
    s = press(s, "pos", P_COPPER).session
    assert (s.ui.view, s.ui.awaiting_input) == ("line_edit", "number")
    r = apply_text(s, "12,5", S1, env())
    assert r.session.draft.lines[0].value == 12.5 and r.session.draft.lines[0].position_id == P_COPPER
    assert r.session.ui.awaiting_input is None


def test_add_through_search():
    s = press(session(), "find").session
    assert s.ui.awaiting_input == "search"
    r = apply_text(s, "медь", S1, env(search=lambda snapshot, q: [P_COPPER, 18]))
    assert r.session.ui.view == "add_search" and r.session.ui.browse_path == ["медь", "17", "18"]
    s = press(r.session, "pos", P_COPPER).session
    assert s.draft.lines[0].position_id == P_COPPER and s.ui.view == "line_edit"


def test_search_without_results_says_so():
    s = press(session(), "find").session
    r = apply_text(s, "ничего", S1, env(search=lambda snapshot, q: []))
    assert r.notice and r.session.ui.view == "add_search"


def test_date_to_yesterday_changes_day_totals():
    s = session([line(1, P_COPPER, "add", 10.0)])
    before = render(s, S1, env()).text
    s = press(press(s, "date").session, "d", 1).session
    after = render(s, S1, env()).text
    assert s.draft.day == date(2026, 8, 31) and s.draft.day_source == "user"
    assert "за день: 10 м" in before and "за день: 63 м" in after  # 53 записано вчера + 10


def test_date_by_text_and_escape():
    s = press(session([line(1, 17)]), "dother").session
    ok = apply_text(s, "30.08", S1, env(parse_date=lambda t: date(2026, 8, 30)))
    assert ok.session.draft.day == date(2026, 8, 30)
    bad = apply_text(s, "не дата", S1, env(parse_date=lambda t: None))
    assert bad.unparsed_text == "не дата" and bad.session.ui.awaiting_input is None


def test_text_longer_than_40_goes_to_engine():
    s = session([line(1, 17)], ui=UiState(view="line_edit", line_id=1, awaiting_input="number"))
    r = apply_text(s, "5 " + "х" * 45, S1, env())
    assert r.unparsed_text and r.session.ui.awaiting_input is None
    assert apply_text(s, "много", S1, env()).unparsed_text == "много"


def test_number_zero_follows_mode_rules():
    s = session([line(1, 17, "set", 5.0)], ui=UiState(view="line_edit", line_id=1, awaiting_input="number"))
    assert apply_text(s, "0", S1, env()).session.draft.lines[0].mode == "clear"
    s = session([line(1, 17, "add", 5.0)], ui=UiState(view="line_edit", line_id=1, awaiting_input="number"))
    assert apply_text(s, "0", S1, env()).session.draft.lines == []


def test_switch_site_rematches_by_name_and_flags_missing():
    s = session([line(1, 17), line(2, P_DRAIN)])
    s2 = apply(s, Action("s1", "1"), S1, env()).session
    assert s2.site_id == "s2" and s2.draft.site_id == "s2"
    assert s2.draft.lines[0].position_id == 17
    gone = s2.draft.lines[1]
    assert gone.position_id is None and gone.source_text
    view = render(s2, snap("s2", drop=(P_DRAIN,)), env())
    assert "нет на этом объекте" in view.text
    assert not any("Записать" in b.label for row in view.buttons for b in row)
    fixed = press(s2, "del", 2, snapshot=snap("s2", drop=(P_DRAIN,))).session
    view = render(fixed, snap("s2", drop=(P_DRAIN,)), env())
    assert any("Записать" in b.label for row in view.buttons for b in row)


def test_switch_site_fails_safely_without_the_other_catalog():
    s = session([line(1, 17)])
    r = apply(s, Action("s1", "1"), S1, env(load_snapshot=lambda sid: None))
    assert r.notice and r.session.site_id == "s1"


def test_site_index_outside_the_listing_is_refused():
    r = apply(session(), Action("s1", "9"), S1, env())
    assert r.notice and r.session.site_id == "s1"


def test_saved_view_lists_written_values_and_delete_gives_clear():
    s = session(day=date(2026, 8, 31))
    s = press(s, "saved").session
    assert s.draft.kind == "edit_saved"
    ids = {x.position_id: x for x in s.draft.lines}
    assert ids[P_COPPER].mode == "set" and ids[P_COPPER].value == 53.0
    text = render(s, S1, env()).text
    assert "= 53 м" in text and "Исправление записанного" in text
    n = [x.line_id for x in s.draft.lines if x.position_id == P_COPPER][0]
    cleared = press(s, "del", n).session.draft
    assert [x for x in cleared.lines if x.position_id == P_COPPER][0].mode == "clear"


def test_saved_is_refused_while_a_new_report_has_lines():
    r = press(session([line(1, 17)]), "saved")
    assert r.notice and r.session.draft.kind == "new"


def test_stale_callbacks_change_nothing():
    s = session([line(1, 17)])
    old = decode(encode_card("ab12cd34", 1, "del", 1))
    moved = press(s, "num").session  # версия растёт
    for session_, action in [(moved, old), (s, decode(encode_card("ffffffff", 1, "del", 1)))]:
        r = apply(session_, action, S1, env())
        assert r.stale and r.session == session_
    no_draft = ChatSession(chat_id=1, site_id="s1")
    assert apply(no_draft, old, S1, env()).stale


def test_every_editing_action_bumps_the_version():
    s = session([line(1, 17, "add", 5.0)], ui=UiState(view="line_edit", line_id=1))
    steps = [("st", 1), ("num", None), ("mode", None), ("other", None), ("back", None), ("date", None)]
    steps += [("d", 0), ("sites", None), ("add", None), ("find", None), ("browse", None), ("pg", 0)]
    for name, arg in steps:
        r = press(s, name, arg)
        assert not r.stale and r.session.draft.version == s.draft.version + 1, name
        s = r.session


def test_commit_and_cancel_are_intents_and_do_not_change_state():
    s = session([line(1, 17)])
    r = press(s, "ok")
    assert r.intent == CommitIntent("ab12cd34", 1) and r.session.draft.version == 1
    assert press(s, "cancel").intent == CancelIntent("ab12cd34")


def test_commit_is_blocked_for_an_empty_or_unresolved_draft():
    assert press(session(), "ok").notice
    s = session([DraftLine(line_id=1, position_id=None, mode="add", value=3.0, unit="м", candidates=[17, 18])])
    r = press(s, "ok")
    assert r.intent is None and r.notice


def test_candidate_tap_resolves_the_line():
    s = session([DraftLine(line_id=1, position_id=None, mode="add", value=3.0, unit="м", candidates=[17, 18])])
    r = press(s, "cand", "1:18").session
    assert r.draft.lines[0].position_id == 18 and r.draft.lines[0].candidates == []
    assert press(s, "cand", "1:99").session.draft.lines[0].candidates == [17, 18]  # не кандидат: без изменений


def test_mode_toggle_and_other_position_keep_the_line():
    s = session([line(1, 17, "add", 5.0)], ui=UiState(view="line_edit", line_id=1))
    s = press(s, "mode").session
    assert s.draft.lines[0].mode == "set"
    s = press(press(s, "other").session, "pos", 18).session
    assert (s.draft.lines[0].position_id, len(s.draft.lines), s.ui.view) == (18, 1, "line_edit")


def test_back_walks_up_the_catalog():
    s = press(press(session(), "add").session, "nav", 0).session
    s = press(s, "back").session
    assert s.ui.view == "add_browse" and s.ui.browse_path == []
    assert press(s, "back").session.ui.view == "card"


def test_clarification_option_with_position_is_applied_in_python():
    clar = Clarification(question="Какая?", options=[ClarificationOption(label="А", position_id=17),
                                                     ClarificationOption(label="Б")], source_text="м")  # fmt: skip
    s = session(clarification=clar, turn_id="aaaaaaaaaaaa")
    r = apply(s, decode("q1:aaaaaaaaaaaa:0"), S1, env())
    assert r.session.clarification is None and r.session.draft.lines[0].position_id == 17
    other = apply(s, decode("q1:aaaaaaaaaaaa:1"), S1, env())
    assert other.intent == EngineIntent("Б")
    typed = apply(s, decode("q1:aaaaaaaaaaaa:2"), S1, env())
    assert typed.notice and typed.session.clarification is None and typed.intent is None


def _clar_tap(source_text, pos_id=P_COPPER):
    clar = Clarification(question="?", options=[ClarificationOption(label="А", position_id=pos_id)],
                         source_text=source_text)  # fmt: skip
    s = session(clarification=clar, turn_id="aaaaaaaaaaaa")
    return apply(s, decode("q1:aaaaaaaaaaaa:0"), S1, env()).session


def test_clarification_tap_keeps_the_quantity_from_the_source_text():
    unit = next(p.unit for p in S1.positions if p.id == P_COPPER)
    r = _clar_tap(f"медь 30 {unit}")
    ln = r.draft.lines[0]
    assert (ln.position_id, ln.value, ln.unit) == (P_COPPER, 30.0, unit)
    assert r.ui.view == "card" and r.ui.awaiting_input is None


def test_clarification_tap_without_a_quantity_opens_the_line_editor():
    r = _clar_tap("медь")
    assert r.draft.lines[0].value == 1.0 and r.ui.view == "line_edit"


def test_clarification_tap_with_a_wrong_unit_opens_the_line_editor():
    unit = next(p.unit for p in S1.positions if p.id == P_COPPER)
    wrong = "кг" if unit != "кг" else "шт"
    r = _clar_tap(f"медь 30 {wrong}")
    assert r.draft.lines[0].value == 1.0 and r.ui.view == "line_edit"


def test_clarification_from_an_old_turn_is_stale():
    clar = Clarification(question="?", options=[ClarificationOption(label="А")])
    s = session(clarification=clar, turn_id="aaaaaaaaaaaa")
    assert apply(s, decode("q1:bbbbbbbbbbbb:0"), S1, env()).stale


def test_clarification_with_a_site_switches_the_site():
    clar = Clarification(question="?", options=[ClarificationOption(label="Школа", site_id="s2")])
    s = session([line(1, 17)], clarification=clar, turn_id="aaaaaaaaaaaa")
    r = apply(s, decode("q1:aaaaaaaaaaaa:0"), S1, env())
    assert r.session.site_id == "s2"


def test_retry_and_draftless_open_actions():
    assert apply(session(), decode("r1:crm"), S1, env()).intent == RetryIntent("crm")
    empty = ChatSession(chat_id=1, site_id="s1")
    r = apply(empty, decode("r1:add"), S1, env())
    assert r.session.draft is not None and r.session.ui.view == "add_browse"
    assert apply(empty, decode("r1:sites"), S1, env()).session.ui.view == "site_pick"


def test_adding_without_a_site_asks_for_a_site():
    r = apply(ChatSession(chat_id=1), decode("r1:add"), S1, env())
    assert r.notice and r.session.ui.view == "site_pick" and r.session.draft is None


@pytest.mark.parametrize("text", ["abc", "-5", "1e3", "99999999999", "", "1,2,3"])
def test_bad_numbers_escape(text):
    s = session([line(1, 17)], ui=UiState(view="line_edit", line_id=1, awaiting_input="number"))
    assert apply_text(s, text, S1, env()).unparsed_text is not None


def test_candidate_tap_keeps_the_page_of_the_line(monkeypatch):
    from src.config import Config
    from src.models import UiState

    monkeypatch.setattr(Config, "CARD_PAGE_SIZE", 2)

    ok = [line(i, 17 + i) for i in range(1, 4)]  # строки 1-3; строка 9 на странице 2 (индекс 3)
    unresolved = DraftLine(line_id=9, position_id=None, mode="add", value=3.0, unit="м", candidates=[17, 18])
    s = session([*ok, unresolved], ui=UiState(view="card", page=1))
    r = press(s, "cand", "9:18").session
    assert r.draft.lines[-1].position_id == 18 and r.ui.page == 1
