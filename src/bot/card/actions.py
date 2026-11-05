"""Конечный автомат карточки. ``apply`` и ``apply_text`` чистые: они возвращают изменённую копию сессии.

Здесь ничего не пишется в хранилище. «Записать» и «Отмена» возвращаются вызывающему как намерения.
Поиск, разбор даты и каталог другого объекта вынесены в швы :class:`CardEnv`, поэтому пакет не
зависит от инструментов сопоставления и от загрузчика объектов.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.bot.card import browse
from src.bot.card.codec import Action
from src.domain.units import units_match
from src.matching.normalize import catalog_vocab, parse_query
from src.models import CatalogSnapshot, ChatSession, ClarificationOption, Draft, DraftLine, Position, Site, UiState
from src.session.store import new_draft_id

TEXT_ESCAPE_LEN = 40
MAX_VOLUME = 10_000_000
_NUMBER = re.compile(r"^\d+([.,]\d+)?$")

SearchFn = Callable[[CatalogSnapshot, str], list[int]]  # id позиций, лучшие первыми
ParseDateFn = Callable[[str], date | None]
LoadSnapshotFn = Callable[[str], CatalogSnapshot | None]


@dataclass(frozen=True)
class CommitIntent:
    draft_id: str
    version: int


@dataclass(frozen=True)
class CancelIntent:
    draft_id: str


@dataclass(frozen=True)
class EngineIntent:
    text: str  # прораб выбрал «Другое»: вызывающий отправляет это движку


@dataclass(frozen=True)
class RetryIntent:
    kind: str  # "engine" | "crm"


@dataclass
class CardEnv:
    sites: list[Site] = field(default_factory=list)  # список, по индексам которого работают кнопки s1
    today: date = field(default_factory=date.today)
    search: SearchFn | None = None
    parse_date: ParseDateFn | None = None
    load_snapshot: LoadSnapshotFn | None = None


@dataclass
class ApplyResult:
    session: ChatSession
    stale: bool = False  # нажатие относится к старой карточке: ничего не изменилось, перерисовать
    intent: CommitIntent | CancelIntent | EngineIntent | RetryIntent | None = None
    notice: str | None = None  # короткое сообщение для прораба
    unparsed_text: str | None = None  # текст уходит движку как обычное сообщение


@dataclass
class _Out:
    intent: object | None = None
    notice: str | None = None
    bump: bool = True


def position_map(snapshot: CatalogSnapshot) -> dict[int, Position]:
    return {p.id: p for p in snapshot.positions}


def line_state(line: DraftLine, snapshot: CatalogSnapshot) -> str:
    """``"unresolved"`` (есть кандидаты), ``"missing"`` (на этом объекте нет позиции) или ``"ok"``."""
    if line.candidates:
        return "unresolved"
    if line.position_id is None or line.position_id not in position_map(snapshot):
        return "missing"
    return "ok"


def commit_block(draft: Draft, snapshot: CatalogSnapshot) -> str | None:
    """Почему «Записать» заблокировано, либо ``None``."""
    if not draft.lines:
        return "В отчёте нет строк."
    states = {line_state(line, snapshot) for line in draft.lines}
    if "unresolved" in states:
        return "Выберите позицию в строках с вопросом."
    if "missing" in states:
        return "Удалите строки, которых нет на этом объекте."
    return None


def _norm_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _next_line_id(draft: Draft) -> int:
    return max((line.line_id for line in draft.lines), default=0) + 1


def _line(draft: Draft, line_id: int | None) -> DraftLine | None:
    return next((x for x in draft.lines if x.line_id == line_id), None)


def _new_draft(session: ChatSession, day: date) -> Draft:
    return Draft(draft_id=new_draft_id(), site_id=session.site_id or "", day=day)


def saved_lines(snapshot: CatalogSnapshot, day: date) -> list[DraftLine]:
    key = day.isoformat()
    return [
        DraftLine(line_id=i, position_id=p.id, mode="set", value=p.values[key], unit=p.unit, source_text=p.name)
        for i, p in enumerate((p for p in snapshot.positions if p.values.get(key, 0) > 0), start=1)
    ]


def _reset_ui(s: ChatSession, view: str = "card") -> None:
    s.ui = UiState(view=view)


def _ensure_draft(s: ChatSession, env: CardEnv) -> Draft:
    if s.draft is None:
        s.draft = _new_draft(s, env.today)
    return s.draft


def _add_line(s: ChatSession, draft: Draft, pos: Position) -> None:
    existing = next((x for x in draft.lines if x.position_id == pos.id), None)
    if existing is None:
        existing = DraftLine(
            line_id=_next_line_id(draft),
            position_id=pos.id,
            mode="set" if draft.kind == "edit_saved" else "add",
            value=1.0,
            unit=pos.unit,
            source_text=pos.name,
        )
        draft.lines.append(existing)
    s.ui = UiState(view="line_edit", line_id=existing.line_id, awaiting_input="number")


def _set_value(draft: Draft, line: DraftLine, value: float) -> bool:
    """Задаёт значение строки; ``True``, если строку убрали из черновика."""
    if value <= 0:
        if line.mode == "add":
            draft.lines.remove(line)
            return True
        line.mode, line.value = "clear", None
        return False
    if line.mode == "clear":
        line.mode = "set"
    line.value = round(value, 6)
    return False


# --- handlers -------------------------------------------------------------------------------------


def _h_add(s, a, snap, env):
    if not s.site_id:
        _reset_ui(s, "site_pick")
        return _Out(notice="Сначала выберите объект.")
    _ensure_draft(s, env)
    s.ui = UiState(view="add_browse")
    return _Out()


def _h_browse(s, a, snap, env):
    s.ui = UiState(view="add_browse", line_id=s.ui.line_id)
    return _Out()


def _h_find(s, a, snap, env):
    s.ui = UiState(view="add_search", line_id=s.ui.line_id, awaiting_input="search")
    return _Out()


def _h_other(s, a, snap, env):
    line_id = a.int_arg() if a.arg is not None else s.ui.line_id
    s.ui = UiState(view="add_search", line_id=line_id, awaiting_input="search")
    return _Out()


def _h_date(s, a, snap, env):
    _reset_ui(s, "date_pick")
    return _Out()


def _h_sites(s, a, snap, env):
    _reset_ui(s, "site_pick")
    return _Out()


def _h_saved(s, a, snap, env):
    if not s.site_id:
        _reset_ui(s, "site_pick")
        return _Out(notice="Сначала выберите объект.")
    if s.draft is not None and s.draft.kind == "new" and s.draft.lines:
        return _Out(notice="Сначала запишите или отмените отчёт.", bump=False)
    day, source = (s.draft.day, s.draft.day_source) if s.draft else (env.today, "default")
    s.draft = Draft(
        draft_id=new_draft_id(), site_id=s.site_id, day=day, day_source=source, kind="edit_saved",
        lines=saved_lines(snap, day),
    )  # fmt: skip
    _reset_ui(s)
    return _Out()


def _h_ok(s, a, snap, env):
    why = commit_block(s.draft, snap)
    if why:
        return _Out(notice=why, bump=False)
    return _Out(intent=CommitIntent(s.draft.draft_id, s.draft.version), bump=False)


def _h_cancel(s, a, snap, env):
    return _Out(intent=CancelIntent(s.draft.draft_id), bump=False)


def _h_back(s, a, snap, env):
    ui = s.ui
    if ui.view == "line_edit":
        _reset_ui(s)
    elif ui.view in ("add_browse", "add_search"):
        if ui.view == "add_browse" and ui.browse_path:
            s.ui = UiState(view="add_browse", line_id=ui.line_id, browse_path=ui.browse_path[:-1])
        elif ui.line_id is not None:
            s.ui = UiState(view="line_edit", line_id=ui.line_id)
        else:
            _reset_ui(s)
    else:
        _reset_ui(s)
    return _Out()


def _h_del(s, a, snap, env):
    line = _line(s.draft, a.int_arg())
    if line is None:
        return _Out(bump=False)
    if s.draft.kind == "edit_saved":
        line.mode, line.value = "clear", None
    else:
        s.draft.lines.remove(line)
    _reset_ui(s)
    return _Out()


def _h_edit(s, a, snap, env):
    line = _line(s.draft, a.int_arg())
    if line is None or line_state(line, snap) != "ok":
        return _Out(bump=False)
    s.ui = UiState(view="line_edit", line_id=line.line_id)
    return _Out()


def _h_step(s, a, snap, env):
    line = _line(s.draft, s.ui.line_id)
    if line is None:
        return _Out(bump=False)
    delta = a.int_arg()
    if line.mode == "clear":
        if delta <= 0:
            return _Out(bump=False)
        removed = _set_value(s.draft, line, float(delta))
    else:
        removed = _set_value(s.draft, line, (line.value or 0) + delta)
    s.ui = UiState() if removed else UiState(view="line_edit", line_id=line.line_id)
    return _Out()


def _h_num(s, a, snap, env):
    s.ui.awaiting_input = "number"
    return _Out()


def _h_mode(s, a, snap, env):
    line = _line(s.draft, s.ui.line_id)
    if line is not None and line.mode in ("add", "set"):
        line.mode = "set" if line.mode == "add" else "add"
    return _Out()


def _h_day(s, a, snap, env):
    _set_day(s, snap, env.today - timedelta(days=a.int_arg()))
    return _Out()


def _set_day(s: ChatSession, snap: CatalogSnapshot, day: date) -> None:
    s.draft.day, s.draft.day_source = day, "user"
    if s.draft.kind == "edit_saved":
        s.draft.lines = saved_lines(snap, day)
    _reset_ui(s)


def _h_dother(s, a, snap, env):
    s.ui = UiState(view="date_pick", awaiting_input="date")
    return _Out()


def _h_nav(s, a, snap, env):
    path = browse.child_path(snap, s.ui.browse_path, a.int_arg())
    if path is None:
        return _Out(bump=False)
    s.ui = UiState(view="add_browse", line_id=s.ui.line_id, browse_path=path)
    return _Out()


def _h_pos(s, a, snap, env):
    pos = position_map(snap).get(a.int_arg())
    if pos is None:
        return _Out(bump=False)
    line = _line(s.draft, s.ui.line_id)
    if line is not None:  # «Другая позиция»: строка остаётся, меняется её позиция
        line.position_id, line.candidates, line.unit = pos.id, [], pos.unit
        s.ui = UiState(view="line_edit", line_id=line.line_id)
    else:
        _add_line(s, s.draft, pos)
    return _Out()


def _h_cand(s, a, snap, env):
    try:
        line_id, pos_id = (int(x) for x in (a.arg or "").split(":"))
    except ValueError:
        return _Out(bump=False)
    line = _line(s.draft, line_id)
    pos = position_map(snap).get(pos_id)
    if line is None or pos is None or pos_id not in line.candidates:
        return _Out(bump=False)
    line.position_id, line.candidates, line.unit = pos.id, [], pos.unit
    page = s.ui.page
    _reset_ui(s)
    s.ui.page = page
    return _Out()


def _h_page(s, a, snap, env):
    s.ui.page = max(a.int_arg(), 0)
    return _Out()


def _h_retry(s, a, snap, env):
    return _Out(intent=RetryIntent(a.arg or ""), bump=False)


def _h_site(s, a, snap, env):
    idx = a.int_arg()
    if not 0 <= idx < len(env.sites):
        return _Out(notice="Список объектов изменился, откройте его заново.", bump=False)
    return _switch_site(s, snap, env, env.sites[idx].site_id)


def _switch_site(s: ChatSession, snap: CatalogSnapshot, env: CardEnv, site_id: str) -> _Out:
    if site_id == s.site_id:
        _reset_ui(s)
        return _Out(bump=False)
    new_snap = None
    if s.draft is not None:
        new_snap = env.load_snapshot(site_id) if env.load_snapshot else None
        if new_snap is None:
            return _Out(notice="Не удалось загрузить данные объекта, попробуйте ещё раз.", bump=False)
    s.site_id = site_id
    if s.draft is not None:
        s.draft.site_id = site_id
        if s.draft.kind == "edit_saved":
            s.draft.lines = saved_lines(new_snap, s.draft.day)
        else:
            _rematch(s.draft, snap, new_snap)
    _reset_ui(s)
    return _Out()


def _rematch(draft: Draft, old: CatalogSnapshot, new: CatalogSnapshot) -> None:
    old_map = position_map(old)

    def find(pid: int) -> list[int]:
        src = old_map.get(pid)
        if src is None:
            return []
        return [
            p.id for p in new.positions if _norm_name(p.name) == _norm_name(src.name) and units_match(p.unit, src.unit)
        ]

    for line in draft.lines:
        ids = [pid for old_id in (line.candidates or [line.position_id]) if old_id is not None for pid in find(old_id)]
        if line.position_id is not None and not line.candidates and line.position_id in old_map:
            line.source_text = line.source_text or old_map[line.position_id].name
        line.position_id, line.candidates = None, []
        if len(ids) == 1:
            line.position_id = ids[0]
            line.unit = next(p.unit for p in new.positions if p.id == ids[0])
        elif ids:
            line.candidates = ids


def _apply_source_quantity(s, draft, pos, source_text, snap) -> None:
    """Сохраняет количество из сообщения прораба, если оно ясно; иначе редактор строки остаётся открытым."""
    parsed = parse_query(source_text, catalog_vocab(snap))
    line = next((x for x in draft.lines if x.position_id == pos.id), None)
    if line is None or parsed.quantity is None or parsed.quantity <= 0 or not units_match(parsed.unit, pos.unit):
        return
    line.value, line.source_text = round(parsed.quantity, 6), source_text
    _reset_ui(s)


def _h_clar(s, a, snap, env):
    clar = s.clarification
    if clar is None or s.turn_id != a.turn or not 0 <= a.int_arg() <= len(clar.options):
        return None  # устарело
    if a.int_arg() == len(clar.options):  # «Другое»: следующий свободный текст уходит движку
        s.clarification = None
        return _Out(notice="Напишите, что вы имели в виду.", bump=False)
    opt: ClarificationOption = clar.options[a.int_arg()]
    s.clarification = None
    if opt.position_id is None and opt.site_id is None and opt.day is None:
        return _Out(intent=EngineIntent(opt.label), bump=False)
    out = _Out()
    if opt.site_id is not None:
        out = _switch_site(s, snap, env, opt.site_id)
        if out.notice:
            s.clarification = clar
            return out
    if opt.day is not None and s.draft is not None:
        _set_day(s, snap, opt.day)
    if opt.position_id is not None:
        pos = position_map(snap).get(opt.position_id)
        if pos is not None:
            draft = _ensure_draft(s, env)
            unresolved = next((x for x in draft.lines if opt.position_id in x.candidates), None)
            if unresolved is not None:
                unresolved.position_id, unresolved.candidates, unresolved.unit = pos.id, [], pos.unit
                _reset_ui(s)
            else:
                is_new = all(x.position_id != pos.id for x in draft.lines)
                _add_line(s, draft, pos)
                if is_new:
                    _apply_source_quantity(s, draft, pos, clar.source_text, snap)
    return out


_HANDLERS: dict[str, Callable] = {
    "add": _h_add, "browse": _h_browse, "find": _h_find, "other": _h_other, "date": _h_date,
    "sites": _h_sites, "saved": _h_saved, "ok": _h_ok, "cancel": _h_cancel, "back": _h_back,
    "del": _h_del, "edit": _h_edit, "st": _h_step, "num": _h_num, "mode": _h_mode, "d": _h_day,
    "dother": _h_dother, "nav": _h_nav, "pos": _h_pos, "cand": _h_cand, "pg": _h_page,
    "retry": _h_retry, "s1": _h_site, "clar": _h_clar,
}  # fmt: skip
_NEEDS_DRAFT = frozenset(
    {
        "date",
        "ok",
        "cancel",
        "del",
        "edit",
        "st",
        "num",
        "mode",
        "d",
        "dother",
        "nav",
        "pos",
        "cand",
        "browse",
        "find",
        "other",
    }
)


def apply(session: ChatSession, action: Action, snapshot: CatalogSnapshot, env: CardEnv) -> ApplyResult:
    """Применяет одно нажатие кнопки. Устаревшее нажатие возвращает сессию без изменений и ``stale=True``."""
    s = session.model_copy(deep=True)
    if action.draft_id is not None and (
        s.draft is None or s.draft.draft_id != action.draft_id or s.draft.version != action.version
    ):
        return ApplyResult(session, stale=True)
    if action.name in _NEEDS_DRAFT and s.draft is None:
        return ApplyResult(session, stale=True)
    handler = _HANDLERS.get(action.name)
    if handler is None:
        return ApplyResult(session, stale=True)
    out = handler(s, action, snapshot, env)
    if out is None:
        return ApplyResult(session, stale=True)
    if out.bump and s.draft is not None:
        s.draft.version += 1
    return ApplyResult(s, intent=out.intent, notice=out.notice)


def parse_number(text: str) -> float | None:
    text = text.strip()
    if not _NUMBER.match(text):
        return None
    value = float(text.replace(",", "."))
    return value if value <= MAX_VOLUME else None


def apply_text(session: ChatSession, text: str, snapshot: CatalogSnapshot, env: CardEnv) -> ApplyResult:
    """Обрабатывает текстовое сообщение, пока карточка ждёт ввода.

    Текст, который не разбирается или длиннее 40 символов, снимает ожидание и возвращается как
    ``unparsed_text`` для движка.
    """
    s = session.model_copy(deep=True)
    wait = s.ui.awaiting_input
    text = text.strip()

    def escape() -> ApplyResult:
        s.ui.awaiting_input = None
        return ApplyResult(s, unparsed_text=text)

    if wait is None or s.draft is None or len(text) > TEXT_ESCAPE_LEN:
        return escape()
    if wait == "number":
        value = parse_number(text)
        line = _line(s.draft, s.ui.line_id)
        if value is None or line is None:
            return escape()
        removed = _set_value(s.draft, line, value)
        s.ui = UiState() if removed else UiState(view="line_edit", line_id=line.line_id)
    elif wait == "date":
        day = env.parse_date(text) if env.parse_date else None
        if day is None:
            return escape()
        _set_day(s, snapshot, day)
    else:
        if env.search is None:
            return escape()
        ids = env.search(snapshot, text)[: browse.SEARCH_LIMIT]
        s.ui = UiState(view="add_search", line_id=s.ui.line_id, browse_path=[text, *map(str, ids)])
        s.draft.version += 1
        return ApplyResult(s, notice=None if ids else "Ничего не найдено. Попробуйте другое слово.")
    s.draft.version += 1
    return ApplyResult(s)
