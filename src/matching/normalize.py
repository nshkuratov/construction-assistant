"""Нормализация текста для разговорных названий работ от прораба.

Только детерминированные правила: нечётких решений о том, КАКАЯ позиция имеется в виду, здесь нет.
Разговорные диаметры ("6-ка") сопоставляются с диаметрами, которые есть в самом каталоге
(``catalog_vocab``), а не с жёстко заданным списком позиций.
"""

import re
from dataclasses import dataclass, field

from src.domain.units import normalize_unit
from src.models import CatalogSnapshot

_LATIN_TO_CYR = str.maketrans("acekmopxyuhtnb", "асекморхуинтпв")
# Замена похожих латинских букв действует только внутри слова, где уже есть кириллица (шум OCR
# в таблице, например "Сберленuе omвepcmuй"); чисто латинское слово ("MDOC", "TEC TUBE") не трогаем.
_CYR = re.compile(r"[а-я]")
_WORD = re.compile(r"[a-zа-я]+")

_UNITS = {
    "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
}  # fmt: skip
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
         "восемьдесят": 80, "девяносто": 90}  # fmt: skip
_HALF = {"полтора": 1.5, "полторы": 1.5}
_SPOKEN_WORDS = "|".join(sorted([*_UNITS, *_TENS, *_HALF], key=len, reverse=True))
_SPOKEN_RUN = re.compile(rf"(?<![а-я])(?:{_SPOKEN_WORDS})(?:\s+(?:{_SPOKEN_WORDS}))*(?:\s+с\s+половиной)?(?![а-я])")
# Порядковые числа берутся только от 10: "второй этаж" прораб числом не называет, "шестнадцатый автомат" — называет.
_ORDINAL = re.compile(
    r"(?<![а-я])(десят|одиннадцат|двенадцат|тринадцат|четырнадцат|пятнадцат|шестнадцат|семнадцат|восемнадцат"
    r"|девятнадцат|двадцат|тридцат)(?:ый|ая|ое|ого|ой|ую|ые|ых)(?![а-я])"
)
_ORDINAL_VALUE = {
    "десят": 10, "одиннадцат": 11, "двенадцат": 12, "тринадцат": 13, "четырнадцат": 14, "пятнадцат": 15,
    "шестнадцат": 16, "семнадцат": 17, "восемнадцат": 18, "девятнадцат": 19, "двадцат": 20, "тридцат": 30,
}  # fmt: skip

_CURRENT = re.compile(r"(?<![\w.])(\d+)\s*(?:а|ампер\w*)(?![\w])")
_DESIGNATOR = re.compile(r"(?<![\w.])[кk](\d+(?:\.\d+)?)(?:\.{2,3}[кk]?(\d+(?:\.\d+)?)|-[кk](\d+))?(?![\w])")
_SPLIT_DECIMAL = re.compile(r"(?<![\d.])(\d{1,2})\s+(\d{2})(?![\d.])")

_CROSS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|х|\*|на)\s*(\d+(?:\.\d+)?)")
_DIAMETER_MARK = re.compile(r"(?:\bд|\bф|ø|диаметр(?:ом|а)?\s*)\s*(\d+(?:\.\d+)?)")
_CROSS_FEATURE = re.compile(r"\b(\d+х\d+(?:\.\d+)?)")

_UNIT_WORDS = "метров|метра|метр|м\\.п|мп|м|штуки|штук|шт|килограммов|килограмма|кг|комплектов|комплект|компл"
_QUANTITY = re.compile(rf"(?<![\w.])(\d+(?:\.\d+)?)\s*({_UNIT_WORDS})(?![\w])")

_UNIT_SPOKEN = {
    "метров": "м", "метра": "м", "метр": "м", "м.п": "м", "мп": "м", "штуки": "шт", "штук": "шт",
    "килограммов": "кг", "килограмма": "кг", "комплектов": "компл", "комплект": "компл",
}  # fmt: skip

_COLLOQUIAL_NUMBERS = {
    "двойка": 2, "тройка": 3, "четверка": 4, "пятерка": 5, "шестерка": 6, "семерка": 7, "восьмерка": 8,
    "девятка": 9, "десятка": 10, "одиннадцатка": 11, "двенашка": 12, "двенадцатка": 12,
}  # fmt: skip
_NUMBER_KA = re.compile(r"(?<![\d.])(\d{1,2})\s*-?\s*(?:ка|ку|ки|кой|ке)\b")

_STOPWORDS = {
    "под", "для", "на", "по", "при", "из", "это", "или", "все", "всего", "сделали", "сделал", "было",
    "уложили", "уложил", "смонтировали", "смонтировал", "поставили", "поставил", "установили", "добавь",
    "сегодня", "вчера", "объекте", "мм",
}  # fmt: skip

# Правила основ, которые простым префиксом из 5 букв не выразить. Побеждает первое совпадение.
_STEM_RULES = (
    (re.compile(r"^мед(ь|н|ью|и|ны|ь)"), "мед"),
    (re.compile(r"изол"), "изол"),
    (re.compile(r"^хомут"), "хомут"),
    (re.compile(r"^кабел"), "кабел"),
    (re.compile(r"^фреон"), "фреон"),
)
_STEM_LEN = 5


@dataclass
class Vocab:
    """Числовой словарь, разобранный из названий каталога во время работы."""

    diameters: set[float] = field(default_factory=set)
    sections: set[str] = field(default_factory=set)
    currents: set[int] = field(default_factory=set)


@dataclass
class ParsedQuery:
    text: str
    stems: list[str]
    diameters: set[float]
    sections: set[str]
    currents: set[int] = field(default_factory=set)
    designators: set[str] = field(default_factory=set)
    quantity: float | None = None
    unit: str = ""


def normalize_text(text: str) -> str:
    """Нижний регистр, ``ё``→``е``, десятичная запятая → точка, единая запись сечения (``3х2.5``)."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = _fold_lookalikes(text)
    text = _spoken_numbers(text)
    return _CROSS.sub(lambda m: f"{m.group(1)}х{m.group(2)}", text)


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _spoken_run_to_digits(match: re.Match) -> str:
    words = match.group(0).split()
    half = words[-2:] == ["с", "половиной"]
    if half:
        words = words[:-2]
    out: list[float] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in _TENS and i + 1 < len(words) and 0 < _UNITS.get(words[i + 1], 99) < 10:
            out.append(_TENS[w] + _UNITS[words[i + 1]])
            i += 2
            continue
        out.append(_UNITS.get(w) or _TENS.get(w) or _HALF[w])
        i += 1
    if half:
        out[-1] += 0.5
    return " ".join(_fmt(v) for v in out)


def _spoken_numbers(text: str) -> str:
    text = _ORDINAL.sub(lambda m: str(_ORDINAL_VALUE[m.group(1)]), text)
    return _SPOKEN_RUN.sub(_spoken_run_to_digits, text)


def _fold_lookalikes(text: str) -> str:
    def fold(m: re.Match) -> str:
        word = m.group(0)
        return word.translate(_LATIN_TO_CYR) if _CYR.search(word) else word

    return re.sub(r"[a-zа-я]+", fold, text)


def stem(word: str) -> str:
    for pattern, result in _STEM_RULES:
        if pattern.search(word):
            return result
    return word[:_STEM_LEN]


def catalog_vocab(snapshot: CatalogSnapshot) -> Vocab:
    vocab = Vocab()
    for p in snapshot.positions:
        text = normalize_text(p.name)
        vocab.diameters.update(float(x) for x in _DIAMETER_MARK.findall(text))
        vocab.sections.update(_CROSS_FEATURE.findall(text))
        vocab.currents.update(int(x) for x in _CURRENT.findall(text))
    return vocab


def position_features(name: str) -> tuple[set[float], set[str]]:
    text = normalize_text(name)
    return {float(x) for x in _DIAMETER_MARK.findall(text)}, set(_CROSS_FEATURE.findall(text))


def position_codes(name: str) -> tuple[set[int], set[str]]:
    """Токи автоматов (А) и обозначения узлов (``к3.1``), записанные в названии позиции."""
    text = normalize_text(name)
    return {int(x) for x in _CURRENT.findall(text)}, designators(text)


def designators(text: str) -> set[str]:
    """Обозначения узлов в нормализованном тексте; диапазон (``К1-К6``, ``К6.1...К6.4``) раскрывается."""
    found: set[str] = set()
    for m in _DESIGNATOR.finditer(text):
        first, last_decimal, last_int = m.group(1), m.group(2), m.group(3)
        found.add(f"к{first}")
        if last_int and "." not in first and int(last_int) > int(first):
            found.update(f"к{n}" for n in range(int(first), int(last_int) + 1))
        elif last_decimal and "." in first and "." in last_decimal:
            head, low = first.split(".")
            head2, high = last_decimal.split(".")
            if head == head2 and int(high) > int(low):
                found.update(f"к{head}.{n}" for n in range(int(low), int(high) + 1))
            else:
                found.add(f"к{last_decimal}")
        elif last_decimal:
            found.add(f"к{last_decimal}")
    return found


_CYR_TO_LAT = str.maketrans("абвгдезийклмнопрстуфхцчшэюя", "abvgdezijklmnoprstufhccsejj")


def to_latin(word: str) -> str:
    """Грубая транслитерация кириллицы в латиницу: марка по-русски ("флекс") находит "FLEX" в каталоге."""
    return word.replace("кс", "x").translate(_CYR_TO_LAT)


def word_stems(text: str) -> list[str]:
    """Основы значимых слов (длина ≥ 3, не стоп-слова). Вход должен быть нормализован."""
    return [stem(w) for w in _WORD.findall(text) if len(w) >= 3 and w not in _STOPWORDS]


def parse_query(raw: str, vocab: Vocab) -> ParsedQuery:
    """Разбирает разговорную фразу на основы слов, числовые признаки и необязательное количество."""
    text = normalize_text(raw)

    quantity, unit = None, ""
    match = _QUANTITY.search(text)
    if match:
        quantity, unit = float(match.group(1)), normalize_unit(_UNIT_SPOKEN.get(match.group(2), match.group(2)))
        text = text[: match.start()] + " " + text[match.end() :]

    text = _SPLIT_DECIMAL.sub(lambda m: _join_split_decimal(m, vocab), text)

    currents: set[int] = {int(m.group(1)) for m in _CURRENT.finditer(text)}
    text = _CURRENT.sub(" ", text)

    codes = designators(text)
    text = _DESIGNATOR.sub(" ", text)

    diameters: set[float] = set()
    for m in _DIAMETER_MARK.finditer(text):
        diameters.add(float(m.group(1)))
    text = _DIAMETER_MARK.sub(" ", text)

    sections = set(_CROSS_FEATURE.findall(text))
    text = _CROSS_FEATURE.sub(" ", text)

    for word, number in _COLLOQUIAL_NUMBERS.items():
        if word in text:
            diameters.update(_expand_colloquial(number, vocab))
            text = text.replace(word, " ")
    for m in _NUMBER_KA.finditer(text):
        diameters.update(_expand_colloquial(int(m.group(1)), vocab))
    text = _NUMBER_KA.sub(" ", text)

    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", text):
        value = float(m.group(1))
        if value in vocab.diameters:
            diameters.add(value)
        elif value == int(value):
            diameters.update(d for d in vocab.diameters if int(d) == int(value))
        if value == int(value) and int(value) in vocab.currents:
            currents.add(int(value))

    return ParsedQuery(
        text=text,
        stems=word_stems(text),
        diameters=diameters,
        sections=sections,
        currents=currents,
        designators=codes,
        quantity=quantity,
        unit=unit,
    )


def _join_split_decimal(m: re.Match, vocab: Vocab) -> str:
    """ "9 52" → "9.52", если такой диаметр есть в каталоге (диктовка теряет разделитель)."""
    joined = f"{m.group(1)}.{m.group(2)}"
    return joined if float(joined) in vocab.diameters else m.group(0)


def _expand_colloquial(number: int, vocab: Vocab) -> set[float]:
    """Диаметры каталога с целой частью ``number`` ("6-ка" → 6.35, если в каталоге есть Д6,35)."""
    found = {d for d in vocab.diameters if int(d) == number}
    return found or {float(number)}
