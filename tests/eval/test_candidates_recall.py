"""Офлайн-уровень оценки по эталонному набору: детерминированный поиск кандидатов по образцу каталога.

Движок (LLM) выбирает среди кандидатов, поэтому контракт поиска такой: нужная позиция входит
в первую тройку, а фраза, которая не называет одну позицию, помечается как неоднозначная (или ничего не находит).
"""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.domain.date_parse import resolve_date
from src.domain.sheet_layout import parse_grid
from src.matching.candidates import AMBIGUITY_MARGIN, find_candidates, is_ambiguous

GRID_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "sample_grid.json"
GOLDEN_PATH = Path(__file__).with_name("golden_set.yaml")
CASES = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]
FIXED_TODAY = date(2026, 11, 3)
TOP1_FLOOR = 0.85
TOP1_ALL_LINES_GUARD = 0.85

# Оставшиеся пробелы поиска.
TOP3_GAPS: dict[tuple[str, str], str] = {}
TOP1_GAPS = {
    (
        "cu-dec",
        "медная трубка 6,35",
    ): "'медная' and 'трубка' each match a different row: flagged ambiguous, the agent asks",
}
WRONG_PICK_GAPS: dict[tuple[str, str], str] = {}


@pytest.fixture(scope="module")
def snapshot():
    return parse_grid(json.loads(GRID_PATH.read_text(encoding="utf-8")), site_id="sad")


def _expected_lines(case: dict) -> list[dict]:
    return [*case.get("lines", []), *case.get("resolved", [])]


LINES = [(c["id"], line) for c in CASES for line in _expected_lines(c)]


def _outcome(line: dict, snapshot) -> tuple[list[int], bool]:
    found = find_candidates(line["query"], snapshot, unit=line["unit"])
    return [c.position.id for c in found], is_ambiguous(found)


def _gap_param(case_id: str, line: dict, gaps: dict):
    key = (case_id, line["query"])
    marks = [pytest.mark.xfail(strict=True, reason=gaps[key])] if key in gaps else []
    return pytest.param(case_id, line, id=f"{case_id}:{line['query']}", marks=marks)


@pytest.mark.parametrize("case_id,line", [_gap_param(i, ln, TOP3_GAPS) for i, ln in LINES])
def test_expected_position_in_top3(snapshot, case_id, line):
    ids, _ = _outcome(line, snapshot)
    assert line["position"] in ids[:3], f"{line['query']!r}: expected {line['position']}, got {ids[:5]}"


def _resolved_or_asked(line: dict, snapshot) -> bool:
    """True, если движок закончит верным выбором или вопросом, где есть нужная строка."""
    found = find_candidates(line["query"], snapshot, unit=line["unit"])
    ids = [c.position.id for c in found]
    if not found:
        return False
    if not is_ambiguous(found):
        return ids[0] == line["position"]
    tied = [c.position.id for c in found if found[0].score - c.score <= AMBIGUITY_MARGIN]
    return line["position"] in tied[:3]


@pytest.mark.parametrize("case_id,line", [_gap_param(i, ln, TOP1_GAPS) for i, ln in LINES])
def test_expected_position_is_top1(snapshot, case_id, line):
    ids, _ = _outcome(line, snapshot)
    assert ids[:1] == [line["position"]], f"{line['query']!r}: expected {line['position']}, got {ids[:5]}"


@pytest.mark.parametrize("case_id,line", [_gap_param(i, ln, WRONG_PICK_GAPS) for i, ln in LINES])
def test_clear_result_is_never_a_wrong_pick(snapshot, case_id, line):
    found = find_candidates(line["query"], snapshot, unit=line["unit"])
    if found and not is_ambiguous(found):
        assert found[0].position.id == line["position"], f"{line['query']!r}: silent wrong pick {found[0].position.id}"
    else:
        pytest.skip("ambiguous or empty: the harness asks")


def test_top1_floors(snapshot):
    lines = [ln for cid, ln in LINES]
    keys = [(cid, ln["query"]) for cid, ln in LINES]
    top1 = [_outcome(ln, snapshot)[0][:1] == [ln["position"]] for ln in lines]
    known = [k in TOP1_GAPS for k in keys]
    outside_gaps = [ok for ok, gap in zip(top1, known, strict=True) if not gap]
    all_rate = sum(top1) / len(lines)
    pick_or_ask = sum(_resolved_or_asked(ln, snapshot) for ln in lines) / len(lines)
    print(
        f"\nlines={len(lines)} top1_all={all_rate:.3f} top1_outside_known_gaps={sum(outside_gaps)}/{len(outside_gaps)}"
    )
    print(f"resolved_or_asked={pick_or_ask:.3f}")
    assert sum(outside_gaps) / len(outside_gaps) >= TOP1_FLOOR
    assert all_rate >= TOP1_ALL_LINES_GUARD
    assert pick_or_ask >= TOP1_FLOOR


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"] == "question"], ids=lambda c: c["id"])
def test_ambiguous_phrase_is_flagged(snapshot, case):
    for query in case["queries"]:
        found = find_candidates(query, snapshot)
        assert not found or is_ambiguous(found), f"{query!r} would resolve to {found[0].position.id}"
        if case["candidates"]:
            top2 = {c.position.id for c in found[:2]}
            assert top2 <= set(case["candidates"]), f"{query!r}: top-2 {top2} not in {case['candidates']}"


@pytest.mark.parametrize("case", [c for c in CASES if "day" in c], ids=lambda c: c["id"])
def test_named_day_resolves(case):
    resolved = resolve_date(case["day_phrase"], FIXED_TODAY)
    assert resolved is not None and resolved.day.isoformat() == case["day"]
    assert not resolved.future


def test_golden_set_shape():
    assert len(CASES) >= 40
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert sum("day" in c for c in CASES) >= 5
    assert any(len(c.get("lines", [])) >= 3 for c in CASES)
    questions = [c for c in CASES if c["expect"] == "question"]
    assert questions and all("queries" in c and "candidates" in c for c in questions)


# --- судья для живого уровня: чистая функция над сохранённой сессией -------------------------------------
