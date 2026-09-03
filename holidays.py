"""Public holidays for Rhineland-Palatinate (RLP), Germany.

Moveable feasts are derived from Easter Sunday (Meeus/Jones/Butcher algorithm)
so this works for any year without a lookup table.
"""
from datetime import date, timedelta


def easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def rlp_holidays(year: int) -> dict[date, str]:
    """Return {date: name} for all RLP public holidays in the given year."""
    easter = easter_sunday(year)
    return {
        date(year, 1, 1): "Neujahr",
        easter - timedelta(days=2): "Karfreitag",
        easter + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        easter + timedelta(days=39): "Christi Himmelfahrt",
        easter + timedelta(days=50): "Pfingstmontag",
        easter + timedelta(days=60): "Fronleichnam",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 10, 31): "Reformationstag",
        date(year, 11, 1): "Allerheiligen",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }


def holidays_in_range(start: date, end: date) -> dict[date, str]:
    """Holidays covering every year touched by [start, end]."""
    result: dict[date, str] = {}
    for y in range(start.year, end.year + 1):
        result.update(rlp_holidays(y))
    return result


def is_workday(d: date, holidays: dict[date, str]) -> bool:
    return d.weekday() < 5 and d not in holidays


def count_pto_days(start: date, end: date) -> int:
    """Count working days (Mon-Fri, excluding RLP holidays) in [start, end]."""
    holidays = holidays_in_range(start, end)
    n = 0
    d = start
    while d <= end:
        if is_workday(d, holidays):
            n += 1
        d += timedelta(days=1)
    return n
