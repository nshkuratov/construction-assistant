"""Инструменты каталога только для чтения: объекты, даты, поиск кандидатов, остаток объёма, записанные значения.

Здесь ничего не импортируется из адаптеров Sheets или Bitrix и ничего не пишется в хранилища. Каталог
берётся из ``<CA_CHAT_DIR>/snapshots/<site_id>.json``, который бот записал до хода.
"""

import json
from datetime import date

from rapidfuzz import fuzz

from src.config import Config
from src.domain.date_parse import resolve_date as parse_date_phrase
from src.domain.dates import today
from src.domain.units import units_match
from src.matching.candidates import Candidate, find_candidates, is_ambiguous
from src.matching.normalize import normalize_text
from src.mcp.errors import VALIDATION, ToolError
from src.mcp.registry import register_tool
from src.models import CatalogSnapshot, ChatSession, Position, Site
from src.session.store import SessionStore

_SITE_MATCH_MIN = 70
_SITE_MATCH_GAP = 10
_QUERY_LIMIT_PER_REMAINING = 5


def open_store() -> SessionStore:
    if not Config.CA_CHAT_DIR:
        raise ToolError("no_chat", "CA_CHAT_DIR is not set: the tool has no chat to work on")
    return SessionStore.from_chat_dir(Config.CA_CHAT_DIR)


def known_sites(store: SessionStore, session: ChatSession) -> dict[str, str]:
    """site_id → отображаемое имя: каждый снимок, записанный ботом, плюс активный объект.

    Имена берутся из необязательного ``sites.json`` (список ``Site``) в каталоге чата; объект
    без имени там показывается по своему id.
    """
    names: dict[str, str] = {}
    sites_file = store.dir / "sites.json"
    if sites_file.exists():
        for raw in json.loads(sites_file.read_text(encoding="utf-8")):
            site = Site.model_validate(raw)
            names[site.site_id] = site.name
    snapshots = store.dir / "snapshots"
    ids = {p.stem for p in snapshots.glob("*.json")} if snapshots.is_dir() else set()
    if session.site_id:
        ids.add(session.site_id)
    return {sid: names.get(sid, sid) for sid in sorted(ids, key=lambda i: (names.get(i, i).lower(), i))}


def active_snapshot(store: SessionStore, session: ChatSession) -> CatalogSnapshot:
    if not session.site_id:
        raise ToolError("no_site", "no site is selected: call list_sites and select_site first")
    snapshot = store.read_snapshot(session.site_id)
    if snapshot is None:
        raise ToolError("no_snapshot", f"no catalog snapshot for site {session.site_id}")
    return snapshot


def position_dict(p: Position) -> dict:
    return {
        "position_id": p.id,
        "number": p.number,
        "name": p.name,
        "unit": p.unit,
        "section": p.section,
        "group": p.group,
        "planned": p.planned,
        "done_total": p.done_total,
        "left": p.left,
    }


def get_position_or_raise(snapshot: CatalogSnapshot, position_id: int) -> Position:
    for p in snapshot.positions:
        if p.id == position_id:
            return p
    raise ToolError("not_found", f"position {position_id} does not exist on this site")


def parse_iso_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ToolError(VALIDATION, f"day must be ISO YYYY-MM-DD, got {text!r}") from None


def site_list(store: SessionStore, session: ChatSession) -> list[dict]:
    sites = known_sites(store, session)
    return [
        {"number": i, "site_id": sid, "name": name, "active": sid == session.site_id}
        for i, (sid, name) in enumerate(sites.items(), start=1)
    ]


def list_sites(_params: dict) -> dict:
    store = open_store()
    session = store.load_session()
    return {"sites": site_list(store, session), "active_site_id": session.site_id}


def match_site(sites: list[dict], text: str) -> list[dict]:
    """Объекты, которые называет ``text``: номер в списке, id, точное имя, иначе явный победитель нечёткого поиска."""
    raw = text.strip()
    if raw.isdigit():
        return [s for s in sites if s["number"] == int(raw)]
    norm = normalize_text(raw)
    exact = [s for s in sites if s["site_id"] == raw or normalize_text(s["name"]) == norm]
    if exact:
        return exact
    scored = sorted(((fuzz.WRatio(norm, normalize_text(s["name"])), s) for s in sites), key=lambda t: -t[0])
    scored = [t for t in scored if t[0] >= _SITE_MATCH_MIN]
    if not scored:
        return []
    if len(scored) == 1 or scored[0][0] - scored[1][0] >= _SITE_MATCH_GAP:
        return [scored[0][1]]
    return [s for score, s in scored if scored[0][0] - score < _SITE_MATCH_GAP]


def resolve_date(params: dict) -> dict:
    result = parse_date_phrase(params["text"], today())
    if result is None:
        raise ToolError("unparsed_date", f"cannot read a date in {params['text']!r}: ask the foreman")
    out = {"day": result.day.isoformat(), "future": result.future}
    if result.future:
        out["warning"] = "Дата в будущем: уточните у прораба."
    return out


def _candidate_entry(query: str, candidates: list[Candidate]) -> dict:
    return {
        "query": query,
        "candidates": [c.to_dict() for c in candidates],
        "ambiguous": is_ambiguous(candidates),
        "found": bool(candidates),
    }


def find_positions(params: dict) -> dict:
    store = open_store()
    snapshot = active_snapshot(store, store.load_session())
    unit, limit = params.get("unit"), params["limit"]
    results = []
    for query in params["queries"]:
        candidates = find_candidates(query, snapshot, unit=unit, limit=limit)
        results.append(_candidate_entry(query, candidates))
    return {"results": results}


def get_position(params: dict) -> dict:
    store = open_store()
    snapshot = active_snapshot(store, store.load_session())
    return {"position": position_dict(get_position_or_raise(snapshot, params["position_id"]))}


def _remaining_entry(label: str, p: Position) -> dict:
    return {"query": label, "ambiguous": False, "found": True, "position": position_dict(p)}


def get_remaining(params: dict) -> dict:
    store = open_store()
    snapshot = active_snapshot(store, store.load_session())
    ids = params.get("position_ids") or ([params["position_id"]] if params.get("position_id") is not None else [])
    results = [_remaining_entry(str(i), get_position_or_raise(snapshot, i)) for i in ids]
    for query in params.get("queries") or []:
        candidates = find_candidates(query, snapshot, limit=_QUERY_LIMIT_PER_REMAINING)
        if candidates and not is_ambiguous(candidates):
            results.append(_remaining_entry(query, candidates[0].position))
        else:
            results.append(_candidate_entry(query, candidates))
    return {"results": results}


def get_entries(params: dict) -> dict:
    store = open_store()
    snapshot = active_snapshot(store, store.load_session())
    day = parse_iso_day(params["day"]).isoformat()
    entries = [{**position_dict(p), "value": p.values[day]} for p in snapshot.positions if day in p.values]
    return {"day": day, "entries": entries, "has_column": day in snapshot.date_columns}


def check_unit(position: Position, unit: str) -> None:
    if unit and not units_match(unit, position.unit_norm):
        raise ToolError(
            "unit_mismatch",
            f"unit {unit!r} does not fit position {position.id} ({position.name[:40]}), which is in {position.unit!r}",
        )


for _name, _handler in {
    "list_sites": list_sites,
    "resolve_date": resolve_date,
    "find_positions": find_positions,
    "get_position": get_position,
    "get_remaining": get_remaining,
    "get_entries": get_entries,
}.items():
    register_tool(_name, _handler)
