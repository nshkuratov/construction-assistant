import json
from pathlib import Path

import pytest

from src.domain.sheet_layout import parse_grid
from src.matching.candidates import AMBIGUITY_MARGIN, find_candidates, is_ambiguous

GRID = json.loads((Path(__file__).parent / "fixtures" / "sample_grid.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snap():
    return parse_grid(GRID, site_id="s")


def top(snap, query, **kw):
    return find_candidates(query, snap, **kw)[0]


def test_copper_pipe_6(snap):
    c = top(snap, "медь 6-ку")
    assert c.position.id == 17 and "Фреонопровод медный" in c.position.name and "Д6,35" in c.position.name


def test_copper_pipe_9(snap):
    assert top(snap, "медь 9-ку").position.id == 18


def test_insulation_is_not_the_copper_pipe(snap):
    c = top(snap, "изоляцию на 6-ку")
    assert c.position.id == 20 and "теплоизоляционная" in c.position.name


@pytest.mark.parametrize("query", ["кабель 3х2,5", "кабель 3 на 2,5", "кабель 3*2.5"])
def test_cable_section(snap, query):
    assert top(snap, query).position.id == 55


def test_freon_clamp_above_drainage_clamp(snap):
    ids = [c.position.id for c in find_candidates("хомутов под фреон", snap)]
    assert ids.index(44) < ids.index(43)


def test_identical_rows_are_both_within_the_margin(snap):
    cands = find_candidates("сверление Д80", snap)
    assert {cands[0].position.id, cands[1].position.id} == {48, 49}
    assert abs(cands[0].score - cands[1].score) <= AMBIGUITY_MARGIN
    assert is_ambiguous(cands)


def test_clear_winner_is_not_ambiguous(snap):
    assert not is_ambiguous(find_candidates("кабель 3х2,5", snap))


def test_conflicting_diameter_ranks_below_every_non_conflicting(snap):
    cands = find_candidates("медь 6-ку", snap, limit=100)
    flags = [c.conflict for c in cands]
    assert any(flags) and flags == sorted(flags)  # сначала False, потом все True
    assert cands[0].position.id == 17 and cands[-1].conflict


def test_unknown_work_gives_no_candidates(snap):
    assert find_candidates("лопата", snap) == []
    assert find_candidates("", snap) == []


def test_unit_in_the_query_is_used(snap):
    c = top(snap, "кабель 4х1,5 56 метров")
    assert c.position.id == 57 and "единица совпала" in c.reasons


def test_wrong_unit_ranks_lower_than_right_unit(snap):
    right = {c.position.id: c.score for c in find_candidates("труба 20", snap, unit="м")}
    wrong = {c.position.id: c.score for c in find_candidates("труба 20", snap, unit="кг")}
    assert right[64] > wrong[64]


def test_limit_and_dict_shape(snap):
    cands = find_candidates("труба", snap, limit=3)
    assert len(cands) == 3
    d = cands[0].to_dict()
    assert {"position_id", "name", "unit", "planned", "done_total", "left", "score", "reasons", "group"} <= set(d)


def test_spoken_cross_section_finds_the_cable(snap):
    assert top(snap, "кабель четыре на полтора").position.id == 57
    assert top(snap, "кабель два на полтора").position.id == 60


def test_breaker_current_picks_the_breaker(snap):
    assert top(snap, "автомат на 16").position.id == 61
    assert top(snap, "шестнадцатый автомат").position.id == 61
    assert top(snap, "автомат 63").position.id in {61, 62, 63}


def test_breaker_without_current_stays_ambiguous(snap):
    assert is_ambiguous(find_candidates("автомат", snap))


def test_bare_current_never_brings_a_position_alone(snap):
    assert all(c.position.id not in {61, 62, 63} for c in find_candidates("хомут 32", snap))


def test_designator_picks_the_unit_and_conflicting_ones_drop(snap):
    found = find_candidates("К7.2 блок", snap)
    assert found[0].position.id == 12
    assert next(c for c in found if c.position.id == 5).conflict


def test_designator_alone_is_enough(snap):
    assert top(snap, "К3.1").position.id == 8


def test_brand_written_in_russian_meets_latin_catalog_word(snap):
    assert top(snap, "К-флекс 9,52").position.id == 21


def test_crossed_misses_are_ambiguous_not_a_silent_pick(snap):
    assert is_ambiguous(find_candidates("медная трубка 6,35", snap))


@pytest.mark.parametrize(("query", "pid"), [("изоляция 6.35", 20), ("изоляция 9,52", 21), ("изоляция на 6-ку", 20)])
def test_insulation_query_with_the_feature_finds_the_tube_clearly(snap, query, pid):
    """Плейбук переписывает "изоляцию на них столько же" в "изоляция <diameter>"; такой запрос однозначен."""
    found = find_candidates(query, snap, unit="м")
    assert found[0].position.id == pid and not is_ambiguous(found)


def test_insulation_query_with_the_copper_word_is_not_clear(snap):
    """Почему плейбук убирает "медь" из переписанного запроса: она подтягивает медную трубу рядом с трубкой."""
    found = find_candidates("изоляцию на них столько же — медь 6-ка", snap, unit="м")
    assert is_ambiguous(found)
