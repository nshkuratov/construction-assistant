"""Нормализация единиц. Единицы сравниваются без учёта регистра и без завершающей точки."""

_ALIASES = {
    "шт": "шт",
    "штук": "шт",
    "м": "м",
    "м.п": "м",
    "п.м": "м",
    "кг": "кг",
    "компл": "компл",
    "комплект": "компл",
    "сист": "сист",
    "система": "сист",
}


def normalize_unit(unit: str | None) -> str:
    """Возвращает каноническую единицу, а для неизвестной — очищенный текст в нижнем регистре.

    ``None`` и пустая строка дают ``""``. ``М``/``м`` и ``компл.``/``компл`` приводятся к одному значению.
    """
    if not unit:
        return ""
    text = " ".join(str(unit).split()).lower().rstrip(".").strip()
    return _ALIASES.get(text, text)


def units_match(a: str | None, b: str | None) -> bool:
    na, nb = normalize_unit(a), normalize_unit(b)
    return bool(na) and na == nb
