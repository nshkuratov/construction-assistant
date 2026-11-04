from datetime import UTC, date, datetime

from src.config import Config
from src.domain.dates import today


def test_today_uses_the_injected_clock_in_the_configured_zone():
    late_utc = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
    assert today("Europe/Moscow", now=late_utc) == date(2026, 11, 4)  # +03:00 переходит через полночь
    assert today("UTC", now=late_utc) == date(2026, 11, 3)


def test_today_defaults_to_config_tz(monkeypatch):
    monkeypatch.setattr(Config, "CA_TZ", "Pacific/Kiritimati")  # +14:00
    late_utc = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert today(now=late_utc) == date(2026, 11, 4)


def test_today_without_injection_is_a_date():
    assert isinstance(today(), date)
