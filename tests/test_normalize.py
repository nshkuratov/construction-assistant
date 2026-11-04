import json
from pathlib import Path

import pytest

from src.domain.sheet_layout import parse_grid
from src.matching.normalize import Vocab, catalog_vocab, normalize_text, parse_query

GRID = json.loads((Path(__file__).parent / "fixtures" / "sample_grid.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vocab() -> Vocab:
    return catalog_vocab(parse_grid(GRID, site_id="s"))


@pytest.mark.parametrize("raw", ["3 на 2,5", "3х2.5", "3x2,5", "3*2,5", "3 х 2,5"])
def test_cross_section_has_one_spelling(raw):
    assert normalize_text(f"кабель {raw}") == "кабель 3х2.5"


def test_yo_and_case_and_decimal_comma():
    assert normalize_text("ШЕСТЁРКА 6,35") == "шестерка 6.35"


def test_latin_lookalikes_fold_only_inside_cyrillic_words():
    assert normalize_text("Сберленuе") == "сберление"
    assert normalize_text("MDOC TEC") == "mdoc tec"


def test_vocabulary_comes_from_catalog_names(vocab):
    assert {6.35, 9.52, 12.7, 80.0} <= vocab.diameters
    assert "3х2.5" in vocab.sections


@pytest.mark.parametrize(
    ("phrase", "diameter"),
    [("6-ка", 6.35), ("6ка", 6.35), ("шестерка", 6.35), ("шестёрка", 6.35), ("9-ку", 9.52), ("девятка", 9.52),
     ("12-ка", 12.7), ("двенашка", 12.7)],
)  # fmt: skip
def test_colloquial_diameter_maps_to_catalog_diameter(vocab, phrase, diameter):
    assert diameter in parse_query(f"медь {phrase}", vocab).diameters


def test_colloquial_diameter_without_catalog_match_keeps_the_number():
    assert parse_query("6-ка", Vocab()).diameters == {6.0}


@pytest.mark.parametrize(
    ("phrase", "value", "unit"),
    [("кабель 15 м", 15, "м"), ("кабель 56 метров", 56, "м"), ("хомуты 10 шт", 10, "шт"), ("кабель 2,5 м", 2.5, "м")],
)
def test_quantity_and_unit_are_extracted(vocab, phrase, value, unit):
    q = parse_query(phrase, vocab)
    assert (q.quantity, q.unit) == (value, unit)


def test_millimetres_are_not_metres(vocab):
    assert parse_query("труба 20 мм", vocab).quantity is None


def test_stems_unify_word_forms(vocab):
    assert parse_query("медь", vocab).stems == parse_query("медная", vocab).stems
    assert parse_query("изоляция", vocab).stems == parse_query("теплоизоляционная", vocab).stems
    assert parse_query("хомут", vocab).stems == parse_query("хомутов", vocab).stems


def test_empty_text_gives_nothing(vocab):
    q = parse_query("   ", vocab)
    assert q.stems == [] and q.diameters == set() and q.quantity is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("кабель три на полтора", "кабель 3х1.5"),
        ("кабель два на два с половиной", "кабель 2х2.5"),
        ("кабель четыре на шесть", "кабель 4х6"),
        ("автомат тридцать два ампера", "автомат 32 ампера"),
        ("шестнадцатый автомат", "16 автомат"),
        ("двадцать пять метров", "25 метров"),
    ],
)
def test_spoken_numbers_become_digits(raw, expected):
    assert normalize_text(raw) == expected


def test_small_ordinal_and_plain_words_are_left_alone():
    assert normalize_text("второй этаж") == "второй этаж"
    assert normalize_text("пятерка") == "пятерка"


def test_number_words_do_not_change_catalog_features():
    snap = parse_grid(GRID, site_id="s")
    for p in snap.positions:
        assert normalize_text(p.name) == normalize_text(normalize_text(p.name))


def test_vocabulary_has_breaker_currents(vocab):
    assert {16, 20, 32} <= vocab.currents


def test_split_decimal_diameter(vocab):
    assert parse_query("мед 9 52", vocab).diameters == {9.52}


def test_split_digits_not_in_catalog_stay_separate(vocab):
    assert parse_query("мед 9 53", vocab).diameters == {9.0}  # 9.53 не диаметр из каталога; а "9" отдельно диаметр


def test_bare_integer_widens_to_catalog_diameter_only_without_exact_match(vocab):
    assert parse_query("фреонопровод 12", vocab).diameters == {12.7}
    assert parse_query("труба 20", vocab).diameters == {20.0}  # точное совпадение главнее, без расширения до 20.x


def test_number_not_in_catalog_is_neither_current_nor_diameter(vocab):
    q = parse_query("автомат 17", vocab)
    assert q.currents == set() and q.diameters == set()


def test_current_with_ampere_word_and_bare_current(vocab):
    assert parse_query("автомат 16А", vocab).currents == {16}
    assert parse_query("автомат на 20", vocab).currents == {20}


def test_quantity_next_to_current_is_not_a_current(vocab):
    q = parse_query("автомат 16А 3 шт", vocab)
    assert q.currents == {16} and q.quantity == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("к3.1 блок", {"к3.1"}),
        ("к1,к2,к4,к5 наружный", {"к1", "к2", "к4", "к5"}),
        ("к6.1...к6.4 внутренний", {"к6.1", "к6.2", "к6.3", "к6.4"}),
        ("наружных блоков к1-к3", {"к1", "к2", "к3"}),
        ("кабель", set()),
    ],
)
def test_designators(text, expected):
    from src.matching.normalize import designators

    assert designators(text) == expected


def test_designator_is_removed_from_numbers(vocab):
    q = parse_query("К3.1 внутренний блок", vocab)
    assert q.designators == {"к3.1"} and q.diameters == set()
