"""Единственная точка доступа к часам. Тесты подменяют ``today`` (или передают ``now``)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.config import Config


def today(tz: str | None = None, now: datetime | None = None) -> date:
    """Сегодняшняя дата в ``tz`` (по умолчанию ``Config.CA_TZ``). ``now`` — подставные часы для тестов."""
    zone = ZoneInfo(tz or Config.CA_TZ)
    moment = now.astimezone(zone) if now is not None and now.tzinfo else now
    return (moment or datetime.now(zone)).date()
