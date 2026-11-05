"""Инструменты сессии: только чтение и обновление ``session.json``. ``propose_draft`` сохраняет черновик
для подтверждения прорабом; ничто здесь не пишет в Google Sheets или Bitrix24.
"""

from datetime import UTC, datetime
from pathlib import Path

from src.config import Config
from src.domain.dates import today
from src.mcp.errors import VALIDATION, ToolError
from src.mcp.registry import register_tool
from src.mcp.wrappers.catalog_tools import (
    active_snapshot,
    check_unit,
    get_position_or_raise,
    known_sites,
    match_site,
    open_store,
    parse_iso_day,
    site_list,
)
from src.models import (
    CatalogSnapshot,
    ChatSession,
    Clarification,
    ClarificationOption,
    Draft,
    DraftLine,
    EngineOutcome,
    Position,
    SkippedItem,
    UiState,
)
from src.session.store import SessionStore, new_draft_id
from src.utils.persistence import append_jsonl, atomic_write_json, safe_load_json

_FORM_TEMPLATE = Path(__file__).resolve().parent.parent / "report_form.md"
_FORM_SECTIONS_MAX = 6
_FORM_EXAMPLES = 4
_EXAMPLE_NAME_MAX = 50
_EXAMPLE_VALUE = 15
NOT_IN_ESTIMATE = "Нет в смете"
_CARD_FREE_LABEL = "другое"  # карточка сама добавляет кнопку свободного ввода; вторая была бы дублем


def _turn_state(store: SessionStore, session: ChatSession) -> dict:
    """Что этот ход уже сделал. Хранится рядом с сессией; сбрасывается при смене ``turn_id``."""
    state = safe_load_json(str(store.dir / "turn_state.json"), default=None) or {}
    if state.get("turn_id") != session.turn_id:
        return {"turn_id": session.turn_id}
    return state


def _save_turn_state(store: SessionStore, state: dict) -> None:
    atomic_write_json(str(store.dir / "turn_state.json"), state)


def get_session(_params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    sections: list[str] = []
    site_name = None
    if session.site_id:
        site_name = known_sites(store, session).get(session.site_id)
        snapshot = store.read_snapshot(session.site_id)
        if snapshot:
            sections = list(dict.fromkeys(p.section for p in snapshot.positions if p.section))
    return {
        "chat_id": session.chat_id,
        "site_id": session.site_id,
        "site_name": site_name,
        "today": today().isoformat(),
        "timezone": Config.CA_TZ,
        "sections": sections,
        "draft": session.draft,
        "clarification": session.clarification,
        "ui": session.ui,
        "history": session.history,
        "turn_id": session.turn_id,
    }


def select_site(params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    sites = site_list(store, session)
    if not sites:
        raise ToolError("not_found", "no sites are loaded")
    matches = match_site(sites, params["site"])
    if not matches:
        raise ToolError("not_found", f"no site matches {params['site']!r}")
    if len(matches) > 1:
        return {"selected": False, "candidates": matches}

    def _switch(s: ChatSession) -> None:
        s.site_id = matches[0]["site_id"]
        s.ui = UiState()
        s.clarification = None

    store.update_session(_switch)
    return {"selected": True, "site": matches[0]}


def _over_estimate(p: Position, mode: str, value: float, day_iso: str) -> str | None:
    if p.planned is None or mode == "clear":
        return None
    day_value = p.values.get(day_iso, 0.0)
    done_before = p.done_total - (day_value if mode == "set" else 0.0)
    if done_before + value <= p.planned:
        return None
    left = max(p.planned - done_before, 0.0)
    return f"Больше остатка: осталось {left:g} {p.unit}, в отчёте {value:g} {p.unit}"


def _build_line(arg: dict, line_id: int, snapshot: CatalogSnapshot, day_iso: str) -> DraftLine:
    mode, value = arg["mode"], arg.get("value")
    if arg.get("position_id") is None:
        candidates = arg.get("candidates") or []
        if not candidates:
            raise ToolError(VALIDATION, "a line needs position_id, or candidates when it is unresolved")
        for cid in candidates:
            get_position_or_raise(snapshot, cid)
        return DraftLine(
            line_id=line_id, mode=mode, value=value, unit=arg.get("unit", ""),
            source_text=arg.get("source_text", ""), candidates=candidates,
        )  # fmt: skip
    p = get_position_or_raise(snapshot, arg["position_id"])
    check_unit(p, arg.get("unit", ""))
    warnings = []
    if value is not None and (warning := _over_estimate(p, mode, value, day_iso)):
        warnings.append(warning)
    return DraftLine(
        line_id=line_id, position_id=p.id, mode=mode, value=value, unit=p.unit,
        source_text=arg.get("source_text", ""), warnings=warnings,
    )  # fmt: skip


def propose_draft(params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    snapshot = active_snapshot(store, session)
    site_id = session.site_id
    assert site_id is not None
    old = session.draft if session.draft and session.draft.site_id == site_id else None

    if params.get("day"):
        day, day_source = parse_iso_day(params["day"]), "message"
    elif old:
        day, day_source = old.day, old.day_source
    else:
        day, day_source = today(), "default"
    day_iso = day.isoformat()

    kept = list(old.lines) if old and not params["replace"] else []
    new_args = params["lines"]
    ids = [a["position_id"] for a in new_args if a.get("position_id") is not None]
    if len(ids) != len(set(ids)):
        raise ToolError(VALIDATION, "a position appears twice in the draft lines")
    kept = [ln for ln in kept if ln.position_id is None or ln.position_id not in ids]
    next_id = max((ln.line_id for ln in kept), default=0) + 1
    lines = kept + [_build_line(a, next_id + i, snapshot, day_iso) for i, a in enumerate(new_args)]
    if not lines:
        raise ToolError(VALIDATION, "a draft needs at least one line")

    kept_skipped = list(old.skipped) if old and not params["replace"] else []
    new_skipped = [SkippedItem(**s) for s in params.get("skipped") or []]
    skipped = kept_skipped + [s for s in new_skipped if s.source_text not in {k.source_text for k in kept_skipped}]

    draft = Draft(
        draft_id=old.draft_id if old else new_draft_id(),
        version=(old.version + 1) if old else 1,
        site_id=site_id,
        day=day,
        day_source=day_source,
        kind=old.kind if old else "new",
        lines=lines,
        skipped=skipped,
    )

    def _store(s: ChatSession) -> None:
        s.draft = draft
        s.clarification = None
        s.ui = UiState()

    saved = store.update_session(_store)
    state = _turn_state(store, saved)
    state["draft_stored"] = True
    _save_turn_state(store, state)
    return {"draft": draft, "warnings": [w for ln in lines for w in ln.warnings]}


def ask_clarification(params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    options = []
    snapshot = store.read_snapshot(session.site_id) if session.site_id else None
    sites = known_sites(store, session)
    for raw in params["options"]:
        if raw.get("position_id") is not None:
            if snapshot is None:
                raise ToolError("no_snapshot", "a position option needs a loaded catalog")
            get_position_or_raise(snapshot, raw["position_id"])
        if raw.get("site_id") is not None and raw["site_id"] not in sites:
            raise ToolError("not_found", f"site {raw['site_id']!r} is not loaded")
        day = parse_iso_day(raw["day"]) if raw.get("day") else None
        if raw.get("position_id") is None and raw.get("site_id") is None and day is None:
            label = raw["label"].strip()
            if label.lower() == _CARD_FREE_LABEL:
                continue
            if label != NOT_IN_ESTIMATE:
                raise ToolError(
                    VALIDATION,
                    f"option {raw['label']!r} is not in the sheet: an option needs a position_id, a site_id or a "
                    f"day, or the label {NOT_IN_ESTIMATE!r}. Never invent work names",
                )
        options.append(
            ClarificationOption(
                label=raw["label"], position_id=raw.get("position_id"), site_id=raw.get("site_id"), day=day
            )
        )
    if options and all(o.position_id is not None for o in options):
        # Нажатие на вариант позиции потеряло бы количество из сообщения.
        raise ToolError(
            "validation",
            "options that are only positions are not allowed: call propose_draft with a line that has "
            "`candidates` (the top position ids), the value and no position_id, then finish_turn(outcome='question')",
        )
    clarification = Clarification(
        question=params["question"], options=options, source_text=params.get("source_text", "")
    )

    def _store(s: ChatSession) -> None:
        s.clarification = clarification
        s.ui = UiState()

    store.update_session(_store)
    return {"clarification": clarification}


def _form_text(store: SessionStore, session: ChatSession) -> str:
    snapshot = active_snapshot(store, session)
    site_name = known_sites(store, session).get(session.site_id or "", session.site_id)
    sections = list(dict.fromkeys(p.section for p in snapshot.positions if p.section))
    first_by_section = {}
    for p in snapshot.positions:
        first_by_section.setdefault(p.section, p)
    chosen = list(first_by_section.values())[:_FORM_EXAMPLES]
    for p in snapshot.positions:
        if len(chosen) >= _FORM_EXAMPLES:
            break
        if p not in chosen:
            chosen.append(p)
    examples = "\n".join(
        f"• {p.name.split(',')[0][:_EXAMPLE_NAME_MAX].strip()} — {_EXAMPLE_VALUE} {p.unit}" for p in chosen
    )
    return (
        _FORM_TEMPLATE.read_text(encoding="utf-8")
        .format(site=site_name, sections=", ".join(sections[:_FORM_SECTIONS_MAX]), examples=examples)
        .strip()
    )


def send_report_form(_params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    text = _form_text(store, session)
    state = _turn_state(store, session)
    state["form_text"] = text
    _save_turn_state(store, state)
    return {"form": text}


def report_friction(params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    append_jsonl(
        Config.FRICTION_LOG,
        {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "chat_id": session.chat_id,
            "turn_id": session.turn_id,
            "summary": params["summary"],
            "details": params.get("details"),
        },
    )
    return {"logged": True}


def finish_turn(params: dict) -> dict:
    store = open_store()
    outcome, message = params["outcome"], params["message"]
    session = store.load_session()
    state = _turn_state(store, session)

    if session.turn_outcome is not None:
        raise ToolError("already_finished", "finish_turn was already called in this turn")
    if outcome == "draft" and not state.get("draft_stored"):
        raise ToolError("no_draft", "outcome 'draft' needs propose_draft in this turn")
    if outcome == "question":
        unresolved = bool(session.draft and any(ln.candidates for ln in session.draft.lines))
        if session.clarification is None and not unresolved:
            raise ToolError("no_question", "outcome 'question' needs ask_clarification or an unresolved line")
    if outcome == "form":
        if not state.get("form_text"):
            raise ToolError("no_form", "outcome 'form' needs send_report_form in this turn")
        message = message or state["form_text"]
    if outcome in ("answer", "error") and not message.strip():
        raise ToolError(VALIDATION, f"outcome {outcome!r} needs a message")

    def _finish(s: ChatSession) -> None:
        s.turn_outcome = EngineOutcome(outcome=outcome, message=message)

    store.update_session(_finish)
    return {"finished": True, "outcome": outcome}


for _name, _handler in {
    "get_session": get_session,
    "select_site": select_site,
    "propose_draft": propose_draft,
    "ask_clarification": ask_clarification,
    "send_report_form": send_report_form,
    "report_friction": report_friction,
    "finish_turn": finish_turn,
}.items():
    register_tool(_name, _handler)
