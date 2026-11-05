import ast
import pathlib
import re
from datetime import date

import pytest

from src.bot.card import decode, format_volume, render
from src.bot.card.codec import MAX_BYTES
from src.config import Config
from src.models import ChatSession, Clarification, ClarificationOption, DraftLine, Site, UiState
from tests.test_card_actions import S1, SITES, TODAY, env, line, session, snap  # noqa: F401

BANNED = re.compile(
    r"position_id|site_id|draft|commit|snapshot|engine|\bmcp\b|\bcrm\b|\bturn\b|\bmode\b|\bid\b|\badd\b|\bset\b|\bclear\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [(15, "м", "15 м"), (2.35, "кг", "2,35 кг"), (0.5, "м", "0,5 м"), (0.004, "м", "0 м"), (1e6, "шт", "1000000 шт"),
     (12.346, "м", "12,35 м"), (3.0, "", "3")],
)  # fmt: skip
def test_format_volume(value, unit, expected):
    assert format_volume(value, unit) == expected


def all_views():
    """По одной сессии на каждый вид, на реальных данных фикстур, чтобы можно было проверить каждый текст и подпись."""
    lines = [line(1, 17, "add", 10.0), line(2, 33, "set", 30.0), line(3, 18, "clear"),
             DraftLine(line_id=4, position_id=None, mode="add", value=3.0, unit="м", source_text="медь",
                       candidates=[17, 19]),
             DraftLine(line_id=5, position_id=None, mode="add", value=2.0, unit="м", source_text="кабель")]  # fmt: skip
    base = session(lines, day=date(2026, 8, 31))
    base.draft.day_source = "message"
    views = {"card": base}
    for name, ui in {
        "line_edit": UiState(view="line_edit", line_id=1, awaiting_input="number"),
        "line_edit_set": UiState(view="line_edit", line_id=2),
        "browse_root": UiState(view="add_browse"),
        "browse_group": UiState(view="add_browse", browse_path=["ОВ 3"]),
        "browse_pos": UiState(view="add_browse", browse_path=["ОВ 3", "Материалы для сист К1-К7"]),
        "search_wait": UiState(view="add_search", awaiting_input="search"),
        "search_found": UiState(view="add_search", browse_path=["медь", "17", "18"]),
        "search_empty": UiState(view="add_search", browse_path=["нет"]),
        "date": UiState(view="date_pick", awaiting_input="date"),
        "sites": UiState(view="site_pick"),
    }.items():
        views[name] = base.model_copy(update={"ui": ui}, deep=True)
    saved = session(kind="edit_saved", day=date(2026, 8, 31))
    saved.draft.lines = [line(1, 17, "set", 53.0), line(2, 33, "clear")]
    views["saved"] = saved
    views["saved_empty"] = session(kind="edit_saved")
    views["empty"] = session()
    views["no_draft"] = ChatSession(chat_id=1, site_id="s1")
    views["no_site"] = ChatSession(chat_id=1)
    clar = Clarification(question="Какой кабель?", options=[ClarificationOption(label="3х2,5", position_id=54)])
    views["question"] = base.model_copy(update={"clarification": clar, "turn_id": "abcdef123456"}, deep=True)
    return views


def texts_and_buttons(view):
    return [view.text] + [b.label for row in view.buttons for b in row]


@pytest.mark.parametrize("name", list(all_views()))
def test_views_have_a_headline_and_no_banned_words(name):
    s = all_views()[name]
    view = render(s, S1, env())
    assert view.text.startswith("🏗 ")
    assert "📅" in view.text.splitlines()[0]
    for text in texts_and_buttons(view):
        assert not BANNED.search(re.sub(r"<[^>]+>", "", text)), (name, text)


@pytest.mark.parametrize("name", list(all_views()))
def test_every_button_with_an_emoji_has_text(name):
    for row in render(all_views()[name], S1, env()).buttons:
        for b in row:
            assert re.search(r"[0-9A-Za-zА-Яа-яЁё−+]", b.label), b.label


@pytest.mark.parametrize("name", list(all_views()))
def test_every_callback_fits_and_decodes(name):
    for row in render(all_views()[name], S1, env()).buttons:
        for b in row:
            assert b.data is not None and len(b.data.encode()) <= MAX_BYTES
            decode(b.data)


def test_header_shows_site_date_and_message_flag():
    s = all_views()["card"]
    first = render(s, S1, env()).text.splitlines()[0]
    assert first == "🏗 Садик · 📅 <b>31.08.2026 (вчера)</b> — проверьте дату"
    s.draft.day, s.draft.day_source = date(2026, 8, 30), "default"
    assert render(s, S1, env()).text.splitlines()[0] == "🏗 Садик · 📅 30.08.2026 (воскресенье)"


def test_line_formats_and_recomputed_warning():
    text = render(all_views()["card"], S1, env()).text
    assert "= 30 м (за день: 30 м)" in text
    assert "+10 м (за день: 63 м)" in text
    assert "удалить" in text
    # позиция 33: по плану 20, сделано 14, за день записано 7, поэтому установка 30 превышает план (осталось 6 + 7)
    assert "⚠️ Строка 2: больше остатка (осталось 13 м)" in text
    stale_warning = all_views()["card"]
    stale_warning.draft.lines[1].warnings = ["old"]
    assert "old" not in render(stale_warning, S1, env()).text


def test_no_warning_within_the_plan():
    s = session([line(1, 33, "set", 5.0)])
    assert "⚠️" not in render(s, S1, env()).text


def test_unresolved_line_has_candidate_buttons_and_hides_commit():
    view = render(all_views()["card"], S1, env())
    assert "❓ 4. медь — 3 м: выберите" in view.text
    labels = [b.label for row in view.buttons for b in row]
    assert "🔍 Другая" in labels and "✅ Записать" not in labels
    assert sum(b.data.split(":")[3] == "cand" for row in view.buttons for b in row) == 2


def test_empty_draft_shows_hint_and_hides_commit():
    view = render(all_views()["empty"], S1, env())
    assert "пока нет строк" in view.text
    labels = [b.label for row in view.buttons for b in row]
    assert "➕ Добавить" in labels and "✖️ Отмена" in labels and "✅ Записать" not in labels


def test_commit_shown_for_a_valid_draft():
    labels = [b.label for row in render(session([line(1, 17)]), S1, env()).buttons for b in row]
    assert "✅ Записать" in labels


def test_no_sites_message():
    view = render(all_views()["sites"], S1, env(sites=[]))
    assert "Нет доступных объектов — обратитесь к администратору" in view.text


def test_site_buttons_are_indexes():
    view = render(all_views()["sites"], S1, env())
    assert [b.data for row in view.buttons for b in row if b.data.startswith("s1:")] == ["s1:0", "s1:1"]


def test_date_view_offers_today_yesterday_and_last_days():
    labels = [b.label for row in render(all_views()["date"], S1, env()).buttons for b in row]
    assert labels[:2] == ["Сегодня", "Вчера"] and "26.08 ср" in labels and len(labels) == 10


def test_sixty_lines_page_and_fit_in_a_message(monkeypatch):
    monkeypatch.setattr(Config, "CARD_PAGE_SIZE", 8)
    s = session([line(i, 17 + i % 3, "add", 1.0 + i, source_text="м") for i in range(1, 61)])
    seen = set()
    for page in range(8):
        s.ui = UiState(page=page)
        view = render(s, S1, env())
        assert len(view.text) <= 4096
        seen.update(re.findall(r"^(\d+)\.", view.text, re.MULTILINE))
        assert all(len(b.data.encode()) <= MAX_BYTES for row in view.buttons for b in row)
    assert len(seen) == 60


def test_page_buttons_only_where_needed(monkeypatch):
    monkeypatch.setattr(Config, "CARD_PAGE_SIZE", 2)
    s = session([line(i, 17) for i in range(1, 6)])
    labels = [b.label for row in render(s, S1, env()).buttons for b in row]
    assert "Стр. 2 ▶" in labels and not any(label.startswith("◀") for label in labels)


def test_long_cyrillic_names_keep_callbacks_short():
    long_site = Site(site_id="0123abcd", name="Очень длинное название объекта " * 5, sheet_tab="x", crm_value_id="1")
    s = all_views()["sites"]
    s.site_id = "0123abcd"
    for e in (env(sites=[long_site] * 3),):
        for b in (b for row in render(s, S1, e).buttons for b in row):
            assert len(b.data.encode()) <= MAX_BYTES and len(b.label) <= 50


def test_html_in_names_is_escaped():
    s = session([DraftLine(line_id=1, position_id=None, mode="add", value=1.0, unit="м", source_text="<b>x</b> & y")])
    text = render(s, S1, env()).text
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


def test_no_telegram_import_under_the_card_package():
    for path in pathlib.Path("src/bot/card").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] in ("telegram", "aiogram") for n in names), path


# --- подписи, которые отличают позиции, регистр единиц, повторное количество, подсказки ----------------


def _labels_of(view):
    return [b.label for row in view.buttons for b in row]


def test_short_name_keeps_the_tail_that_tells_positions_apart():
    from src.bot.card import short_name

    by_id = {p.id: p.name for p in S1.positions}
    a, b = short_name(by_id[17], 44), short_name(by_id[18], 44)
    assert a != b and a.endswith("TEC TUBE cips") and "6,35" in a and "9,52" in b and len(a) <= 44 and "…" in a
    assert short_name("короткое имя", 44) == "короткое имя"


def test_labels_are_unique_for_the_whole_fixture_catalog():
    from src.bot.card import unique_labels

    entries = [(p.name, f"№{p.number or p.id}") for p in S1.positions]
    for limit in (38, 44, 120):
        labels = unique_labels(entries, limit)
        assert len(set(labels)) == len(labels) == len(entries)
        assert all(len(label) <= limit for label in labels)


def test_same_name_positions_get_a_number_suffix_in_buttons():
    s = session([DraftLine(line_id=1, mode="add", value=3.0, unit="шт", source_text="сверление Д80 — 3 шт",
                           candidates=[48, 49])])  # fmt: skip
    labels = _labels_of(render(s, S1, env()))
    cand = [x for x in labels if "Д80" in x or "Сбер" in x or "сверл" in x.lower()]
    assert len(cand) == 2 and len(set(cand)) == 2


def test_card_lines_and_candidate_buttons_never_repeat_a_label():
    ids = [17, 18, 19]
    view = render(session([line(i, pid) for i, pid in enumerate(ids, 1)]), S1, env())
    names = [x.split(" — ")[0] for x in view.text.splitlines() if x[:1].isdigit()]
    assert len(names) == 3 and len(set(names)) == 3
    unresolved = DraftLine(line_id=9, mode="add", value=3.0, unit="м", source_text="медь", candidates=ids)
    buttons = _labels_of(render(session([unresolved]), S1, env()))[:3]
    assert len(set(buttons)) == 3


def test_search_and_browse_lists_have_unique_labels():
    ids = [p.id for p in S1.positions]
    s = session([])
    s.ui = UiState(view="add_search", browse_path=["q", *map(str, ids)])
    seen = []
    for page in range(-(-len(ids) // Config.CARD_PAGE_SIZE)):
        s.ui.page = page
        seen += [b.label for row in render(s, S1, env()).buttons for b in row if b.label[:1] in "№"]
    assert len(seen) == len(set(seen)) and len(seen) >= 8


def test_clarification_buttons_with_equal_names_get_the_number():
    clar = Clarification(
        question="Какое?",
        options=[ClarificationOption(label="Сверление Д80", position_id=48),
                 ClarificationOption(label="Сверление Д80", position_id=49)],
    )  # fmt: skip
    s = session([]).model_copy(update={"clarification": clar, "turn_id": "abcdef123456"})
    labels = _labels_of(render(s, S1, env()))
    assert labels[0] != labels[1] and "№" in labels[0]
    assert labels.count("✍️ Другое") == 1


def test_unit_is_shown_in_lower_case_everywhere():
    assert format_volume(56, "М") == "56 м"
    s = session([line(1, 55, "add", 56.0)])
    view = render(s, S1, env())
    assert "56 м" in view.text and " М" not in view.text.replace("М8", "")


def test_unit_case_is_display_only():
    assert next(p for p in S1.positions if p.id == 55).unit == "М"


def test_unresolved_line_shows_the_quantity_once():
    s = session([DraftLine(line_id=1, mode="add", value=3.0, unit="шт", source_text="сверление Д80 — 3 шт",
                           candidates=[48, 49])])  # fmt: skip
    text = render(s, S1, env()).text
    assert text.count("3 шт") == 1
    other = session([DraftLine(line_id=1, mode="add", value=3.0, unit="шт", source_text="сверление Д80 три штуки",
                               candidates=[48, 49])])  # fmt: skip
    assert render(other, S1, env()).text.count("3 шт") == 1


def test_check_the_date_hint_is_not_shown_for_today():
    s = session([line(1, 17)], day=TODAY)
    s.draft.day_source = "message"
    assert "проверьте дату" not in render(s, S1, env()).text
    s.draft.day = date(2026, 8, 31)
    assert "проверьте дату" in render(s, S1, env()).text


def test_paged_card_hints_at_a_question_on_another_page(monkeypatch):
    monkeypatch.setattr(Config, "CARD_PAGE_SIZE", 2)
    lines = [line(1, 17), line(2, 18), DraftLine(line_id=3, mode="add", value=1.0, unit="м", source_text="x",
                                                 candidates=[19, 20])]  # fmt: skip
    s = session(lines)
    text = render(s, S1, env()).text
    assert "Есть вопрос на стр. 2" in text
    s.ui.page = 1
    assert "Есть вопрос на стр." not in render(s, S1, env()).text


def test_card_shows_skipped_works_above_the_buttons():
    from src.models import SkippedItem

    s = session([line(1, 17, "add", 10.0)])
    s.draft.skipped = [SkippedItem(source_text="бетон 5 кубов", reason="нет в смете")]
    view = render(s, snap("s1"), env())
    assert "🚫 бетон 5 кубов — нет в смете, не будет записано" in view.text
    assert any(b.label == "✅ Записать" for row in view.buttons for b in row)
    assert view.text.index("🚫 бетон") > view.text.index("1. ")


def test_unit_mismatch_question_gives_the_reason_with_one_quantity():
    ln = DraftLine(line_id=1, position_id=None, mode="add", value=10.0, unit="шт", source_text="кабель 3х2,5",
                   candidates=[17, 18])  # fmt: skip
    text = render(session([ln]), snap("s1"), env()).text
    assert "указано 10 шт, а в смете позиция в м — выберите или исправьте" in text
    assert text.count("10 шт") == 1


def test_edit_screen_shows_the_unit_in_lower_case():
    s = session([line(1, 17, "add", 10.0)], ui=UiState(view="line_edit", line_id=1))
    text = render(s, snap("s1"), env()).text
    assert "Единица: м" in text
    snapshot = snap("s1")
    for p in snapshot.positions:
        p.unit = "М"
    assert "Единица: м\n" in render(s, snapshot, env()).text
