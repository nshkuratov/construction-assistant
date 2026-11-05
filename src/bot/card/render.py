"""Рендерит сессию карточки в текст и кнопки. Чистая функция; текст в HTML (``parse_mode="HTML"``).

Каждый вид начинается с одного заголовка: ``🏗 <объект> · 📅 <дата>``. Итоги дня и предупреждения о
превышении сметы пересчитываются из снимка при каждом рендере; черновику для этого не доверяют.
"""

import html
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.bot.card import browse
from src.bot.card.actions import CardEnv, commit_block, line_state, position_map
from src.bot.card.codec import encode_card, encode_clarification, encode_retry, encode_site
from src.config import Config
from src.models import CatalogSnapshot, ChatSession, DraftLine, Position

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_NAME_LEN = 48
_BUTTON_LEN = 44
_MAX_CANDIDATE_BUTTONS = 5
_EPS = 1e-9


@dataclass(frozen=True)
class Button:
    label: str
    data: str | None = None  # callback data
    url: str | None = None


@dataclass
class CardView:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    parse_mode: str = "HTML"


def format_volume(value: float, unit: str = "") -> str:
    """До 2 знаков после запятой, запятая, без хвостовых нулей: ``15 м``, ``2,35 кг``, ``0,5 м``."""
    text = f"{round(value, 2):.2f}".rstrip("0").rstrip(".").replace(".", ",")
    if text in ("-0", ""):
        text = "0"
    return f"{text} {unit.lower()}".strip()


def short_name(name: str, limit: int = _NAME_LEN) -> str:
    """Начало и конец вокруг «…»: часть, по которой различаются позиции (диаметр, сечение), часто стоит в конце."""
    name = " ".join(name.split())
    if len(name) <= limit:
        return name
    tail_len = max((limit - 1) // 2, 1)
    tail = name[-tail_len:]
    if " " in tail and name[-tail_len - 1] != " ":
        tail = tail.split(" ", 1)[1]  # не начинать хвост с середины слова
    head = name[: limit - 1 - len(tail)].rstrip()
    return f"{head}…{tail}"


def unique_labels(entries: list[tuple[str, str]], limit: int) -> list[str]:
    """Сокращает каждую пару ``(name, tag)`` до ``limit``; совпавшие подписи получают в конце тег (``№44``)."""
    labels = [short_name(name, limit) for name, _ in entries]
    counts = {label: labels.count(label) for label in labels}
    return [
        label if counts[label] == 1 else f"{short_name(name, limit - len(tag) - 1)} {tag}"
        for label, (name, tag) in zip(labels, entries, strict=True)
    ]


def _tag(pos: Position) -> str:
    return f"№{pos.number if pos.number is not None else pos.id}"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def day_label(day: date, today: date) -> str:
    if day == today:
        return "сегодня"
    if day == today - timedelta(days=1):
        return "вчера"
    return _WEEKDAYS[day.weekday()]


def _site_name(session: ChatSession, env: CardEnv) -> str:
    for site in env.sites:
        if site.site_id == session.site_id:
            return site.name
    return "объект не выбран"


def _header(session: ChatSession, env: CardEnv) -> str:
    draft = session.draft
    day = draft.day if draft else env.today
    when = f"{day:%d.%m.%Y} ({day_label(day, env.today)})"
    if draft is not None and draft.day_source == "message" and day != env.today:
        when = f"<b>{when}</b> — проверьте дату"
    return f"🏗 {_esc(short_name(_site_name(session, env), 40))} · 📅 {when}"


class _Buttons:
    """Собирает ряды кнопок; кнопки черновика несут id и версию черновика для проверки устаревания."""

    def __init__(self, session: ChatSession):
        self.draft = session.draft

    def c(self, label: str, name: str, arg: int | str | None = None) -> Button:
        if self.draft is None:
            return Button(label, encode_retry(name))
        return Button(label, encode_card(self.draft.draft_id, self.draft.version, name, arg))


def _effect(line: DraftLine, pos: Position, day: date) -> tuple[float, float | None]:
    """``(значение за день после записи, остаток объёма до этой строки)``; второе равно ``None``, если плана нет."""
    existing = pos.values.get(day.isoformat(), 0.0)
    if line.mode == "add":
        after = existing + (line.value or 0)
        base = pos.done_total
    elif line.mode == "set":
        after = line.value or 0
        base = pos.done_total - existing
    else:
        return 0.0, None
    return after, None if pos.planned is None else pos.planned - base


def _warns(line: DraftLine, pos: Position, day: date) -> str | None:
    if line.mode == "clear":
        return None
    _, available = _effect(line, pos, day)
    if available is None or (line.value or 0) <= available + _EPS:
        return None
    return f"осталось {format_volume(max(available, 0), pos.unit)}"


def _line_labels(draft, snap: CatalogSnapshot) -> dict[int, str]:
    """Уникальное в черновике имя каждой сопоставленной строки (подпись не меняется при смене страницы)."""
    pmap = position_map(snap)
    ok = [(ln.line_id, pmap[ln.position_id]) for ln in draft.lines if line_state(ln, snap) == "ok"]
    labels = unique_labels([(p.name, _tag(p)) for _, p in ok], _NAME_LEN)
    return {line_id: label for (line_id, _), label in zip(ok, labels, strict=True)}


def _unit_mismatch(line: DraftLine, snap: CatalogSnapshot) -> str | None:
    """Единицы кандидатов, когда единица прораба не подходит ни к одной из них."""
    pmap = position_map(snap)
    units = list(dict.fromkeys(pmap[c].unit.lower() for c in line.candidates if c in pmap))
    if line.unit and units and line.unit.lower() not in units:
        return "/".join(units)
    return None


def _unresolved_text(n: int, line: DraftLine, snap: CatalogSnapshot) -> str:
    source = _esc(short_name(line.source_text))
    if (units := _unit_mismatch(line, snap)) is not None:
        said = format_volume(line.value or 0, line.unit)
        return f"❓ {n}. {source}: указано {said}, а в смете позиция в {units} — выберите или исправьте"
    quantity = format_volume(line.value or 0, line.unit)
    if quantity.lower() in line.source_text.lower():
        return f"❓ {n}. {source}: выберите"
    return f"❓ {n}. {source} — {quantity}: выберите"


def _line_text(n: int, line: DraftLine, snap: CatalogSnapshot, day: date, label: str = "") -> str:
    state = line_state(line, snap)
    if state == "unresolved":
        return _unresolved_text(n, line, snap)
    if state == "missing":
        return f"🚫 {n}. {_esc(short_name(line.source_text))} — нет на этом объекте"
    pos = position_map(snap)[line.position_id]
    after, _ = _effect(line, pos, day)
    if line.mode == "add":
        change = f"+{format_volume(line.value or 0, pos.unit)}"
    elif line.mode == "set":
        change = f"= {format_volume(line.value or 0, pos.unit)}"
    else:
        change = "удалить"
    text = f"{n}. {_esc(label or short_name(pos.name, _NAME_LEN))} — {change}"
    if line.mode != "clear":
        text += f" (за день: {format_volume(after, pos.unit)})"
    return text


def _view_card(session: ChatSession, snap: CatalogSnapshot, env: CardEnv) -> CardView:
    draft = session.draft
    b = _Buttons(session)
    lines_text: list[str] = []
    rows: list[list[Button]] = []
    if draft is None or not draft.lines:
        if draft is not None and draft.kind == "edit_saved":
            hint = "За этот день ничего не записано. Нажмите «Добавить» или смените дату."
        else:
            hint = "В отчёте пока нет строк. Напишите, что сделано, или нажмите «Добавить»."
        lines_text.append(hint)
    else:
        size = Config.CARD_PAGE_SIZE
        indexed = list(enumerate(draft.lines, start=1))
        page_items, page, pages = browse.page_slice(indexed, session.ui.page, size)
        labels = _line_labels(draft, snap)
        for n, line in page_items:
            lines_text.append(_line_text(n, line, snap, draft.day, labels.get(line.line_id, "")))
        warnings = []
        for n, line in indexed:
            if line_state(line, snap) == "ok":
                warn = _warns(line, position_map(snap)[line.position_id], draft.day)
                if warn:
                    warnings.append(f"⚠️ Строка {n}: больше остатка ({warn})")
        lines_text += [""] + warnings if warnings else []
        lines_text += [f"🚫 {_esc(short_name(x.source_text))} — нет в смете, не будет записано" for x in draft.skipped]
        if pages > 1:
            lines_text.append(f"Страница {page + 1} из {pages}")
            other = next((i // size for i, (_, ln) in enumerate(indexed) if i // size != page and ln.candidates), None)
            if other is not None:
                lines_text.append(f"❓ Есть вопрос на стр. {other + 1}")
        pair_row: list[Button] = []
        for n, line in page_items:
            state = line_state(line, snap)
            if state == "unresolved":
                found = [
                    position_map(snap)[pid]
                    for pid in line.candidates[:_MAX_CANDIDATE_BUTTONS]
                    if pid in position_map(snap)
                ]
                names = unique_labels([(p.name, _tag(p)) for p in found], _BUTTON_LEN)
                for pos, label in zip(found, names, strict=True):
                    rows.append([b.c(label, "cand", f"{line.line_id}:{pos.id}")])
                rows.append([b.c("🔍 Другая", "other", line.line_id), b.c(f"🗑 {n}", "del", line.line_id)])
                continue
            if state == "ok":
                pair_row.append(b.c(f"✏️ {n}", "edit", line.line_id))
            pair_row.append(b.c(f"🗑 {n}", "del", line.line_id))
            if len(pair_row) >= 4:
                rows.append(pair_row)
                pair_row = []
        if pair_row:
            rows.append(pair_row)
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(b.c(f"◀ Стр. {page}", "pg", page - 1))
            if page < pages - 1:
                nav.append(b.c(f"Стр. {page + 2} ▶", "pg", page + 1))
            rows.append(nav)
    if draft is None:
        rows.append([b.c("➕ Добавить", "add"), b.c("🏗 Объект", "sites")])
        rows.append([b.c("📋 Записанное", "saved")])
        return CardView("\n".join([_header(session, env), "", *lines_text]), rows)
    rows.append([b.c("➕ Добавить", "add"), b.c("📅 Дата", "date")])
    rows.append([b.c("🏗 Объект", "sites")] + ([b.c("📋 Записанное", "saved")] if draft.kind == "new" else []))
    last = [b.c("✖️ Отмена", "cancel")]
    if commit_block(draft, snap) is None:
        last.insert(0, b.c("✅ Записать", "ok"))
    rows.append(last)
    title = "✏️ Исправление записанного" if draft.kind == "edit_saved" else None
    head = [_header(session, env)] + ([title] if title else [])
    return CardView("\n".join([*head, "", *lines_text]), rows)


def _view_line_edit(session: ChatSession, snap: CatalogSnapshot, env: CardEnv) -> CardView:
    draft = session.draft
    line = next((x for x in draft.lines if x.line_id == session.ui.line_id), None)
    if line is None or line_state(line, snap) != "ok":
        return _view_card(session, snap, env)
    pos = position_map(snap)[line.position_id]
    b = _Buttons(session)
    after, available = _effect(line, pos, draft.day)
    existing = pos.values.get(draft.day.isoformat(), 0.0)
    text = [_header(session, env), "", f"✏️ {_esc(short_name(pos.name, 120))}", f"Единица: {_esc(pos.unit.lower())}"]
    if line.mode == "add":
        text.append(f"Сейчас: +{format_volume(line.value or 0, pos.unit)}")
    elif line.mode == "set":
        text.append(f"Сейчас: = {format_volume(line.value or 0, pos.unit)}")
    else:
        text.append("Сейчас: удалить")
    if existing:
        text.append(f"Уже записано за день: {format_volume(existing, pos.unit)}")
    if line.mode != "clear":
        text.append(f"Будет за день: {format_volume(after, pos.unit)}")
    if pos.planned is not None:
        text.append(f"Осталось по плану: {format_volume(pos.left, pos.unit)}")
    warn = _warns(line, pos, draft.day)
    if warn:
        text.append(f"⚠️ Больше остатка ({warn})")
    if session.ui.awaiting_input == "number":
        text.append(f"Напишите число в {_esc(pos.unit)}, например 12,5.")
    rows = [
        [b.c("−10", "st", -10), b.c("−1", "st", -1), b.c("+1", "st", 1), b.c("+10", "st", 10)],
        [b.c("⌨️ Ввести число", "num"), b.c("🔁 Другая позиция", "other")],
    ]
    if existing and line.mode != "clear":
        label = "↔️ Заменить значение" if line.mode == "add" else "↔️ Добавить к записанному"
        rows.append([b.c(label, "mode")])
    rows.append([b.c("⬅️ Назад", "back")])
    return CardView("\n".join(text), rows)


def _path_text(path: list[str]) -> str:
    return " › ".join(_esc(short_name(p, 30)) for p in path)


def _item_buttons(b: _Buttons, items: list[browse.Item]) -> list[list[Button]]:
    labels = unique_labels(
        [(i.label, f"№{i.ref}" if i.kind == "pos" else f"({n})") for n, i in enumerate(items, 1)], _BUTTON_LEN
    )
    return [[b.c(label, i.kind, i.ref)] for label, i in zip(labels, items, strict=True)]


def _page_nav(b: _Buttons, page: int, pages: int) -> list[Button]:
    nav = []
    if page > 0:
        nav.append(b.c(f"◀ Стр. {page}", "pg", page - 1))
    if page < pages - 1:
        nav.append(b.c(f"Стр. {page + 2} ▶", "pg", page + 1))
    return nav


def _view_browse(session: ChatSession, snap: CatalogSnapshot, env: CardEnv) -> CardView:
    b = _Buttons(session)
    path = session.ui.browse_path
    items, page, pages = browse.page_slice(browse.level_items(snap, path), session.ui.page, Config.CARD_PAGE_SIZE)
    prompt = ["Выберите раздел:", "Выберите группу или позицию:", "Выберите позицию:"][min(len(path), 2)]
    text = [_header(session, env), "", f"📂 {_path_text(path)}" if path else "📂 Каталог", prompt]
    if not items:
        text.append("Здесь нет позиций.")
    if pages > 1:
        text.append(f"Страница {page + 1} из {pages}")
    rows = _item_buttons(b, items)
    nav = _page_nav(b, page, pages)
    if nav:
        rows.append(nav)
    rows.append([b.c("🔍 Поиск", "find")])
    rows.append([b.c("⬅️ Назад", "back")])
    return CardView("\n".join(text), rows)


def _view_search(session: ChatSession, snap: CatalogSnapshot, env: CardEnv) -> CardView:
    b = _Buttons(session)
    path = session.ui.browse_path
    query, ids = (path[0], [int(x) for x in path[1:]]) if path else ("", [])
    text = [_header(session, env), ""]
    rows: list[list[Button]] = []
    if session.ui.awaiting_input == "search":
        text.append("🔍 Напишите название позиции, например «кабель 3 на 2,5».")
    else:
        items, page, pages = browse.page_slice(browse.search_items(snap, ids), session.ui.page, Config.CARD_PAGE_SIZE)
        text.append(f"🔍 Найдено по запросу «{_esc(short_name(query, 30))}»:" if items else "🔍 Ничего не найдено.")
        rows = _item_buttons(b, items)
        nav = _page_nav(b, page, pages)
        if nav:
            rows.append(nav)
        rows.append([b.c("🔍 Новый поиск", "find")])
    rows.append([b.c("📂 По разделам", "browse")])
    rows.append([b.c("⬅️ Назад", "back")])
    return CardView("\n".join(text), rows)


def _view_date(session: ChatSession, snap: CatalogSnapshot, env: CardEnv) -> CardView:
    b = _Buttons(session)
    text = [_header(session, env), "", "📅 Выберите дату отчёта:"]
    if session.ui.awaiting_input == "date":
        text.append("Или напишите дату, например 02.11 или «позавчера».")
    days = [
        b.c(
            f"{(env.today - timedelta(days=n)):%d.%m} {_WEEKDAYS_SHORT[(env.today - timedelta(days=n)).weekday()]}",
            "d",
            n,
        )
        for n in range(2, 8)
    ]
    rows = [[b.c("Сегодня", "d", 0), b.c("Вчера", "d", 1)], days[:3], days[3:]]
    rows.append([b.c("⌨️ Другая дата", "dother")])
    rows.append([b.c("⬅️ Назад", "back")])
    return CardView("\n".join(text), rows)


def _view_sites(session: ChatSession, snap: CatalogSnapshot, env: CardEnv) -> CardView:
    b = _Buttons(session)
    text = [_header(session, env), ""]
    rows: list[list[Button]] = []
    if not env.sites:
        text.append("Нет доступных объектов — обратитесь к администратору.")
    else:
        text.append("🏗 Выберите объект:")
        if session.draft is not None and session.draft.lines:
            text.append("Строки отчёта будут найдены на новом объекте по названию.")
        for idx, site in enumerate(env.sites):
            mark = "✔ " if site.site_id == session.site_id else ""
            rows.append([Button(f"{mark}{short_name(site.name, _BUTTON_LEN)}", encode_site(idx))])
    rows.append([b.c("⬅️ Назад", "back")])
    return CardView("\n".join(text), rows)


def render_clarification(session: ChatSession, env: CardEnv, snapshot: CatalogSnapshot | None = None) -> CardView:
    """Вопрос движка с одной кнопкой на вариант; последняя кнопка позволяет прорабу ввести ответ текстом."""
    clar = session.clarification
    turn = session.turn_id or ""
    text = [_header(session, env), "", f"❓ {_esc(clar.question)}"]
    pmap = position_map(snapshot) if snapshot is not None else {}
    tags = [_tag(pmap[o.position_id]) if o.position_id in pmap else f"({i})" for i, o in enumerate(clar.options, 1)]
    labels = unique_labels([(o.label, t) for o, t in zip(clar.options, tags, strict=True)], _BUTTON_LEN)
    rows = [[Button(label, encode_clarification(turn, i))] for i, label in enumerate(labels)]
    rows.append([Button("✍️ Другое", encode_clarification(turn, len(clar.options)))])
    return CardView("\n".join(text), rows)


_VIEWS = {
    "card": _view_card,
    "saved": _view_card,
    "line_edit": _view_line_edit,
    "add_browse": _view_browse,
    "add_search": _view_search,
    "date_pick": _view_date,
    "site_pick": _view_sites,
}


def render(session: ChatSession, snapshot: CatalogSnapshot, env: CardEnv) -> CardView:
    """Рендерит вид, в котором находится сессия; открытый вопрос на уточнение идёт первым."""
    if session.clarification is not None and session.ui.view == "card":
        return render_clarification(session, env, snapshot)
    view = session.ui.view
    if view != "site_pick" and session.draft is None and view != "card":
        view = "card"
    return _VIEWS[view](session, snapshot, env)


__all__ = ["Button", "CardView", "format_volume", "render", "render_clarification", "short_name", "unique_labels"]
