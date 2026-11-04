"""Помощники для времени в UTC.

Время здесь — обычные настенные часы в UTC. Идентификаторы запусков и метки времени берут их
отсюда, чтобы во всём коде был один источник времени. ``parse_iso8601`` — единственный
детерминированный разборщик строк ISO-8601 (любая фильтрация по давности идёт через него).
"""

from datetime import UTC, datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return now_utc().isoformat()


def parse_iso8601(value: str | None) -> datetime | None:
    """Разбирает метку времени ISO-8601 в datetime UTC с часовым поясом или возвращает ``None``.

    Для пустого или неразбираемого значения возвращает ``None`` вместо исключения: источник с
    битой датой не должен ронять детерминированный конвейер. Завершающая ``Z`` допустима
    (``datetime.fromisoformat`` понимает её с 3.11, но нормализуем на всякий случай). Наивное
    время считается UTC, чтобы расчёты давности всегда сравнивали значения с часовым поясом.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
