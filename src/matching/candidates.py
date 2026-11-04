"""Поиск кандидатов: ранжирует позиции каталога для разговорного названия работы.

Python только ранжирует, выбирает харнесс. Числовой признак, который противоречит запросу (просили
диаметр 6.35, а в названии 9.52), опускает позицию ниже всех непротиворечащих. Если два лучших
балла ближе, чем ``AMBIGUITY_MARGIN``, вызывающий код должен спросить прораба, а не выбирать.
"""

import math
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from src.domain.units import normalize_unit
from src.matching.normalize import (
    ParsedQuery,
    catalog_vocab,
    normalize_text,
    parse_query,
    position_codes,
    position_features,
    to_latin,
    word_stems,
)
from src.models import CatalogSnapshot, Position

AMBIGUITY_MARGIN = 0.08
_STEM_MATCH_RATIO = 80
_CONFLICT_PENALTY = 10.0
_W_OVERLAP, _W_PARTIAL, _W_NUMERIC = 0.55, 0.2, 0.3
_UNIT_BONUS, _UNIT_PENALTY = 0.1, 0.15


@dataclass
class Candidate:
    position: Position
    score: float
    reasons: list[str] = field(default_factory=list)
    conflict: bool = False
    missed: frozenset[str] = frozenset()
    featured: bool = False

    def to_dict(self) -> dict:
        p = self.position
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
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "conflict": self.conflict,
        }


def is_ambiguous(candidates: list[Candidate]) -> bool:
    """True, если лучший кандидат не опережает второго с явным отрывом."""
    if len(candidates) < 2:
        return False
    first, second = candidates[0], candidates[1]
    if second.conflict:
        return False
    gap = first.score - second.score
    # Обе позиции совпали по числу, но каждая не содержит слово запроса, которое есть у другой
    # ("медная трубка": в одной строке "медный", в другой "трубка"). По весам слов не понять, какое
    # важнее, поэтому порог удваивается.
    crossed = (
        first.featured and second.featured and bool(first.missed - second.missed) and bool(second.missed - first.missed)
    )
    return gap <= AMBIGUITY_MARGIN * (2 if crossed else 1)


def _stem_matches(query_stem: str, position_stems: set[str]) -> bool:
    if query_stem in position_stems or to_latin(query_stem) in position_stems:
        return True
    return len(query_stem) >= 4 and any(fuzz.ratio(query_stem, s) >= _STEM_MATCH_RATIO for s in position_stems)


def find_candidates(query: str, snapshot: CatalogSnapshot, unit: str | None = None, limit: int = 8) -> list[Candidate]:
    parsed = parse_query(query, catalog_vocab(snapshot))
    return rank(parsed, snapshot, unit or parsed.unit or None, limit)


def rank(parsed: ParsedQuery, snapshot: CatalogSnapshot, unit: str | None, limit: int) -> list[Candidate]:
    positions = snapshot.positions
    texts = {p.id: normalize_text(f"{p.name} {p.group or ''}") for p in positions}
    stems = {pid: set(word_stems(t)) for pid, t in texts.items()}
    doc_freq: dict[str, int] = {}
    for st in stems.values():
        for s in st:
            doc_freq[s] = doc_freq.get(s, 0) + 1
    weight = {q: math.log(1 + len(positions) / (1 + doc_freq.get(q, 0))) for q in parsed.stems}
    total_weight = sum(weight.values())
    want_unit = normalize_unit(unit)
    plain_query = " ".join(parsed.stems)

    out: list[Candidate] = []
    for p in positions:
        reasons: list[str] = []
        matched = [q for q in parsed.stems if _stem_matches(q, stems[p.id])]
        overlap = sum(weight[q] for q in matched) / total_weight if total_weight else 0.0
        if matched:
            reasons.append("слова: " + ", ".join(matched))
        partial = fuzz.partial_ratio(plain_query, texts[p.id]) / 100 if plain_query else 0.0

        diameters, sections = position_features(p.name)
        numeric = 0.0
        conflict = False
        if parsed.diameters and diameters:
            if parsed.diameters & diameters:
                numeric, _ = 1.0, reasons.append("диаметр совпал")
            else:
                conflict = True
                reasons.append("диаметр не совпал")
        if parsed.sections and sections:
            if parsed.sections & sections:
                numeric, _ = 1.0, reasons.append("сечение совпало")
            else:
                conflict = True
                reasons.append("сечение не совпало")

        currents, codes = position_codes(p.name)
        current_hit = False
        if parsed.currents and currents:
            if parsed.currents & currents:
                current_hit, _ = True, reasons.append("ток совпал")
            else:
                conflict = True
                reasons.append("ток не совпал")
        if parsed.designators and codes:
            if parsed.designators & codes:
                numeric, _ = 1.0, reasons.append("обозначение совпало")
            else:
                conflict = True
                reasons.append("обозначение не совпало")
        # Голый ток ("32") встречается и у труб, и у крепежа, поэтому сам по себе он позицию не добавляет.
        listed_alone = numeric > 0
        if current_hit:
            numeric = 1.0

        score = _W_OVERLAP * overlap + _W_PARTIAL * partial + _W_NUMERIC * numeric
        if want_unit:
            if p.unit_norm == want_unit:
                score += _UNIT_BONUS
                reasons.append("единица совпала")
            else:
                score -= _UNIT_PENALTY
                reasons.append("единица другая")
        if not matched and not listed_alone:
            continue
        if conflict:
            score -= _CONFLICT_PENALTY
        out.append(Candidate(p, score, reasons, conflict, frozenset(parsed.stems) - set(matched), listed_alone))

    out.sort(key=lambda c: (-c.score, c.position.id))
    return out[:limit]
