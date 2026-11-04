import pytest

from src.domain.units import normalize_unit, units_match


def test_case_insensitive():
    assert normalize_unit("М") == normalize_unit("м") == "м"


def test_trailing_dot_insensitive():
    assert normalize_unit("компл.") == normalize_unit("компл") == "компл"


@pytest.mark.parametrize("raw", ["шт", "ШТ", "кг", "сист", " м "])
def test_known_units_are_stable(raw):
    assert normalize_unit(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_is_empty(raw):
    assert normalize_unit(raw) == ""


def test_unit_mismatch_and_blank_never_match():
    assert not units_match("кг", "м")
    assert not units_match("", "")
    assert units_match("М", "м.")
