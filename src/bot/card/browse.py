"""Обзор каталога: раздел → группа → позиция, с листанием. Чистые функции над снимком."""

from dataclasses import dataclass

from src.models import CatalogSnapshot, Position

SEARCH_LIMIT = 20


@dataclass(frozen=True)
class Item:
    label: str  # полное название; render его сокращает
    kind: str  # "nav" открывает следующий уровень (ref = индекс на этом уровне); "pos" выбирает позицию (ref = id)
    ref: int


def _unique(values) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        seen.setdefault(v, None)
    return list(seen)


def level_items(snapshot: CatalogSnapshot, path: list[str]) -> list[Item]:
    """Элементы уровня обзора, заданного ``path`` (``[]`` = разделы, ``[section]``, ``[section, group]``)."""
    if not path:
        return [
            Item(s or "Без раздела", "nav", i) for i, s in enumerate(_unique(p.section for p in snapshot.positions))
        ]
    in_section = [p for p in snapshot.positions if p.section == path[0]]
    if len(path) == 1:
        groups = _unique(p.group for p in in_section if p.group)
        items = [Item(g, "nav", i) for i, g in enumerate(groups)]
        return items + [Item(position_label(p), "pos", p.id) for p in in_section if not p.group]
    return [Item(position_label(p), "pos", p.id) for p in in_section if p.group == path[1]]


def child_path(snapshot: CatalogSnapshot, path: list[str], idx: int) -> list[str] | None:
    """Путь после нажатия на элемент навигации ``idx``; ``None``, если ``idx`` не элемент навигации."""
    items = level_items(snapshot, path)
    nav = [i for i in items if i.kind == "nav"]
    if not 0 <= idx < len(nav):
        return None
    if not path:
        sections = _unique(p.section for p in snapshot.positions)
        return [sections[idx]]
    groups = _unique(p.group for p in snapshot.positions if p.section == path[0] and p.group)
    return [path[0], groups[idx]]


def position_label(p: Position) -> str:
    return f"№{p.number} {p.name}" if p.number is not None else p.name


def search_items(snapshot: CatalogSnapshot, ids: list[int]) -> list[Item]:
    by_id = {p.id: p for p in snapshot.positions}
    return [Item(position_label(by_id[i]), "pos", i) for i in ids if i in by_id]


def page_slice(items: list, page: int, size: int) -> tuple[list, int, int]:
    """``(элементы страницы, скорректированная страница, число страниц)``; у пустого списка одна пустая страница."""
    size = max(size, 1)
    pages = max((len(items) + size - 1) // size, 1)
    page = min(max(page, 0), pages - 1)
    return items[page * size : (page + 1) * size], page, pages
