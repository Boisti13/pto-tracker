"""Public holidays for Germany, by federal state (Bundesland).

Moveable feasts are derived from Easter Sunday (Meeus/Jones/Butcher algorithm)
so this works for any year without a lookup table.
"""
from datetime import date, timedelta

GERMAN_STATES = {
    "BW": "Baden-Württemberg",
    "BY": "Bayern",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}

DEFAULT_STATE = "RP"


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


def _buss_und_bettag(year: int) -> date:
    # The Wednesday falling between 16 and 22 November (i.e. before 23 Nov).
    nov22 = date(year, 11, 22)
    offset = (nov22.weekday() - 2) % 7  # 2 == Wednesday
    return nov22 - timedelta(days=offset)


# Observed in every state.
_NATIONAL_HOLIDAYS = [
    ("Neujahr", lambda year, easter: date(year, 1, 1)),
    ("Karfreitag", lambda year, easter: easter - timedelta(days=2)),
    ("Ostermontag", lambda year, easter: easter + timedelta(days=1)),
    ("Tag der Arbeit", lambda year, easter: date(year, 5, 1)),
    ("Christi Himmelfahrt", lambda year, easter: easter + timedelta(days=39)),
    ("Pfingstmontag", lambda year, easter: easter + timedelta(days=50)),
    ("Tag der Deutschen Einheit", lambda year, easter: date(year, 10, 3)),
    ("1. Weihnachtstag", lambda year, easter: date(year, 12, 25)),
    ("2. Weihnachtstag", lambda year, easter: date(year, 12, 26)),
]

# Observed only in the listed states. (Bavaria's Mariä Himmelfahrt is a
# municipality-by-municipality holiday there, not statewide, so it's left
# out rather than guessed at.)
_STATE_HOLIDAYS = [
    ("Heilige Drei Könige", lambda year, easter: date(year, 1, 6), {"BW", "BY", "ST"}),
    ("Internationaler Frauentag", lambda year, easter: date(year, 3, 8), {"BE", "MV"}),
    (
        "Fronleichnam",
        lambda year, easter: easter + timedelta(days=60),
        {"BW", "BY", "HE", "NW", "RP", "SL"},
    ),
    ("Mariä Himmelfahrt", lambda year, easter: date(year, 8, 15), {"SL"}),
    ("Weltkindertag", lambda year, easter: date(year, 9, 20), {"TH"}),
    (
        "Reformationstag",
        lambda year, easter: date(year, 10, 31),
        {"BB", "HB", "HH", "MV", "NI", "SN", "ST", "SH", "TH"},
    ),
    (
        "Allerheiligen",
        lambda year, easter: date(year, 11, 1),
        {"BW", "BY", "NW", "RP", "SL"},
    ),
    ("Buß- und Bettag", lambda year, easter: _buss_und_bettag(year), {"SN"}),
]


def state_holidays(year: int, state: str = DEFAULT_STATE) -> dict[date, str]:
    """Return {date: name} for all public holidays in `state` in the given year."""
    easter = easter_sunday(year)
    result: dict[date, str] = {}
    for name, fn in _NATIONAL_HOLIDAYS:
        result[fn(year, easter)] = name
    for name, fn, states in _STATE_HOLIDAYS:
        if state in states:
            result[fn(year, easter)] = name
    return result


def holidays_in_range(start: date, end: date, state: str = DEFAULT_STATE) -> dict[date, str]:
    """Holidays covering every year touched by [start, end]."""
    result: dict[date, str] = {}
    for y in range(start.year, end.year + 1):
        result.update(state_holidays(y, state))
    return result


def is_workday(d: date, holidays: dict[date, str]) -> bool:
    return d.weekday() < 5 and d not in holidays


def count_pto_days(start: date, end: date, state: str = DEFAULT_STATE) -> int:
    """Count working days (Mon-Fri, excluding public holidays) in [start, end]."""
    holidays = holidays_in_range(start, end, state)
    n = 0
    d = start
    while d <= end:
        if is_workday(d, holidays):
            n += 1
        d += timedelta(days=1)
    return n
