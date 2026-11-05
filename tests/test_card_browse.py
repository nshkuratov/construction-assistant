from src.bot.card import browse
from tests.test_card_actions import S1


def test_sections_groups_and_positions():
    sections = browse.level_items(S1, [])
    assert [i.label for i in sections] == ["ОВ 3", "ЭМ3"]
    level1 = browse.level_items(S1, ["ОВ 3"])
    assert [i.kind for i in level1].count("nav") == 3  # три группы
    assert any(i.kind == "pos" for i in level1)  # позиции без группы стоят рядом с группами
    assert browse.child_path(S1, ["ОВ 3"], 0) == ["ОВ 3", "Материалы для сист К1-К7"]
    leaf = browse.level_items(S1, ["ОВ 3", "Материалы для сист К1-К7"])
    assert leaf and all(i.kind == "pos" for i in leaf)


def test_child_path_rejects_a_bad_index():
    assert browse.child_path(S1, [], 99) is None
    assert browse.child_path(S1, ["ОВ 3"], -1) is None


def test_paging_boundaries():
    items = list(range(10))
    assert browse.page_slice(items, 0, 4) == ([0, 1, 2, 3], 0, 3)
    assert browse.page_slice(items, 2, 4) == ([8, 9], 2, 3)
    assert browse.page_slice(items, 99, 4)[1] == 2  # значение ограничено
    assert browse.page_slice(items, -3, 4)[1] == 0
    assert browse.page_slice([], 0, 4) == ([], 0, 1)


def test_search_items_skip_unknown_ids():
    items = browse.search_items(S1, [17, 99999])
    assert [i.ref for i in items] == [17]
