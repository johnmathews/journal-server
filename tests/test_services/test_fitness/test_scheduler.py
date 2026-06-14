from datetime import datetime

from journal.services.fitness.scheduler import next_fire_after


def test_next_fire_later_today():
    now = datetime(2026, 6, 14, 9, 0, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 14, 17, 0, 0)


def test_next_fire_rolls_to_tomorrow_when_past_hour():
    now = datetime(2026, 6, 14, 17, 30, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 15, 17, 0, 0)


def test_next_fire_exactly_on_the_hour_rolls_forward():
    now = datetime(2026, 6, 14, 17, 0, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 15, 17, 0, 0)


def test_next_fire_strips_minutes_seconds_micros():
    now = datetime(2026, 6, 14, 8, 45, 13, 500)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 14, 17, 0, 0)
