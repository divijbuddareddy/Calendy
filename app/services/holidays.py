"""Static public-holiday lists (2026-2027) used by the availability editor.

GB = England & Wales bank holidays, US = federal holidays (observed dates),
IN = major national/gazetted holidays (lunar-calendar dates are approximate).
"""
import datetime as dt

COUNTRIES = {"GB": "United Kingdom (England & Wales)", "US": "United States", "IN": "India"}

_RAW = {
    "GB": [
        ("2026-01-01", "New Year's Day"), ("2026-04-03", "Good Friday"), ("2026-04-06", "Easter Monday"),
        ("2026-05-04", "Early May bank holiday"), ("2026-05-25", "Spring bank holiday"),
        ("2026-08-31", "Summer bank holiday"), ("2026-12-25", "Christmas Day"), ("2026-12-28", "Boxing Day (substitute)"),
        ("2027-01-01", "New Year's Day"), ("2027-03-26", "Good Friday"), ("2027-03-29", "Easter Monday"),
        ("2027-05-03", "Early May bank holiday"), ("2027-05-31", "Spring bank holiday"),
        ("2027-08-30", "Summer bank holiday"), ("2027-12-27", "Christmas Day (substitute)"),
        ("2027-12-28", "Boxing Day (substitute)"),
    ],
    "US": [
        ("2026-01-01", "New Year's Day"), ("2026-01-19", "Martin Luther King Jr. Day"),
        ("2026-02-16", "Washington's Birthday"), ("2026-05-25", "Memorial Day"), ("2026-06-19", "Juneteenth"),
        ("2026-07-03", "Independence Day (observed)"), ("2026-09-07", "Labor Day"), ("2026-10-12", "Columbus Day"),
        ("2026-11-11", "Veterans Day"), ("2026-11-26", "Thanksgiving Day"), ("2026-12-25", "Christmas Day"),
        ("2027-01-01", "New Year's Day"), ("2027-01-18", "Martin Luther King Jr. Day"),
        ("2027-02-15", "Washington's Birthday"), ("2027-05-31", "Memorial Day"),
        ("2027-06-18", "Juneteenth (observed)"), ("2027-07-05", "Independence Day (observed)"),
        ("2027-09-06", "Labor Day"), ("2027-10-11", "Columbus Day"), ("2027-11-11", "Veterans Day"),
        ("2027-11-25", "Thanksgiving Day"), ("2027-12-24", "Christmas Day (observed)"),
    ],
    "IN": [
        ("2026-01-26", "Republic Day"), ("2026-03-04", "Holi"), ("2026-03-21", "Id-ul-Fitr"),
        ("2026-04-03", "Good Friday"), ("2026-08-15", "Independence Day"), ("2026-10-02", "Gandhi Jayanti"),
        ("2026-10-20", "Dussehra"), ("2026-11-08", "Diwali"), ("2026-12-25", "Christmas Day"),
        ("2027-01-26", "Republic Day"), ("2027-03-10", "Id-ul-Fitr"), ("2027-03-22", "Holi"),
        ("2027-03-26", "Good Friday"), ("2027-08-15", "Independence Day"), ("2027-10-02", "Gandhi Jayanti"),
        ("2027-10-09", "Dussehra"), ("2027-10-29", "Diwali"), ("2027-12-25", "Christmas Day"),
    ],
}


def holidays_for(country: str, year: int | None = None) -> list[tuple[dt.date, str]]:
    rows = [(dt.date.fromisoformat(d), n) for d, n in _RAW.get((country or "").upper(), [])]
    if year:
        rows = [r for r in rows if r[0].year == year]
    return sorted(rows)
