from datetime import date

import pytest
from pydantic import ValidationError

from src.models import ChatSession, CrmEntry, Draft, DraftLine, Position


@pytest.mark.parametrize(("mode", "value"), [("add", 0), ("set", -1), ("add", None), ("clear", 5)])
def test_invalid_line_values_are_rejected(mode, value):
    with pytest.raises(ValidationError):
        DraftLine(line_id=1, position_id=5, mode=mode, value=value)


def test_valid_lines():
    assert DraftLine(line_id=1, position_id=5, mode="add", value=0.5).value == 0.5
    assert DraftLine(line_id=2, position_id=5, mode="clear").value is None


def test_unknown_keys_are_forbidden():
    with pytest.raises(ValidationError):
        DraftLine(line_id=1, position_id=5, mode="add", value=1, volume=2)


def test_draft_version_starts_at_one():
    Draft(draft_id="ab12cd34", site_id="s", day=date(2026, 11, 3))
    with pytest.raises(ValidationError):
        Draft(draft_id="ab12cd34", version=0, site_id="s", day=date(2026, 11, 3))


def test_unresolved_line_carries_candidates():
    line = DraftLine(line_id=1, mode="add", value=3, candidates=[48, 49])
    assert line.position_id is None and line.candidates == [48, 49]


def test_position_totals_are_computed_not_supplied():
    p = Position(id=1, name="x", unit="м", unit_norm="м", planned=10, values={"2026-01-01": 2.5, "2026-01-02": 1})
    assert (p.done_total, p.left) == (3.5, 6.5)
    assert p.model_dump()["left"] == 6.5
    lie = Position(id=1, name="x", unit="м", unit_norm="м", planned=10, done_total=99, left=99)
    assert (lie.done_total, lie.left) == (0.0, 10.0)  # переданные итоги игнорируются
    assert Position.model_validate(p.model_dump()) == p


def test_session_round_trips_through_json():
    s = ChatSession(chat_id=7, draft=Draft(draft_id="a", site_id="s", day=date(2026, 11, 3)))
    assert ChatSession.model_validate_json(s.model_dump_json()) == s


def test_crm_entry_requires_a_value():
    with pytest.raises(ValidationError):
        CrmEntry(site_name="a", crm_value_id="1", day=date(2026, 11, 3), position_key="1|x", position_name="x")


def test_draft_skipped_defaults_empty_and_old_json_loads():
    from src.models import SkippedItem

    d = Draft(draft_id="a", site_id="s", day=date(2026, 11, 3))
    assert d.skipped == []
    old = d.model_dump(mode="json")
    old.pop("skipped")
    assert Draft.model_validate(old).skipped == []
    d.skipped.append(SkippedItem(source_text="бетон 5 кубов", reason="нет в смете"))
    assert Draft.model_validate(d.model_dump(mode="json")).skipped[0].source_text == "бетон 5 кубов"
