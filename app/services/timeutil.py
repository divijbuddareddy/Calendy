"""DST-safe time helpers (PRD 20.1).

Conventions: every stored instant is a *naive UTC* datetime. Local wall-clock times are
interpreted with IANA zones via zoneinfo -- never with fixed UTC offsets.

Policies
  * Spring-forward (nonexistent local time): `wall_to_utc_all` returns [] -> the occurrence is
    skipped. `wall_to_utc` (single value, used for range boundaries) maps it to the instant
    just after the gap.
  * Fall-back (ambiguous local time): `wall_to_utc_all` returns BOTH instants (fold=0 first);
    labels carry the UTC offset so the two slots are distinguishable.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC = dt.timezone.utc


@lru_cache(maxsize=512)
def get_zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)


def is_valid_tz(tz: Optional[str]) -> bool:
    if not tz or not isinstance(tz, str) or len(tz) > 64:
        return False
    try:
        get_zone(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def validate_tz(tz: Optional[str], default: str = "UTC") -> str:
    """Return tz if it is a valid IANA identifier, else `default`."""
    return tz if is_valid_tz(tz) else default


@lru_cache(maxsize=1)
def all_timezones() -> list[str]:
    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
    return ["UTC"] + zones


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(tzinfo=None)


def to_naive_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_iso_utc(value: str) -> dt.datetime:
    """Parse an ISO-8601 string ('Z' or offset; naive = UTC) into naive UTC. Raises ValueError."""
    if not isinstance(value, str) or not value:
        raise ValueError("empty datetime")
    v = value.strip().replace("z", "Z")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return to_naive_utc(dt.datetime.fromisoformat(v))


def iso_z(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return to_naive_utc(value).replace(microsecond=0).isoformat() + "Z"


def utc_to_local(value: dt.datetime, tz: str) -> dt.datetime:
    """naive UTC -> aware local datetime."""
    return value.replace(tzinfo=UTC).astimezone(get_zone(validate_tz(tz)))


def parse_hhmm(value: str) -> int:
    """'09:30' -> minutes since midnight ('24:00' -> 1440)."""
    h, m = value.strip().split(":")[:2]
    mins = int(h) * 60 + int(m)
    if not 0 <= mins <= 1440:
        raise ValueError(f"invalid time {value!r}")
    return mins


def _local(date: dt.date, minutes: int, z: ZoneInfo, fold: int) -> dt.datetime:
    day = date + dt.timedelta(days=minutes // 1440)
    m = minutes % 1440
    return dt.datetime(day.year, day.month, day.day, m // 60, m % 60, fold=fold, tzinfo=z)


def _roundtrips(local: dt.datetime) -> bool:
    back = local.astimezone(UTC).astimezone(local.tzinfo)
    return back.replace(tzinfo=None, fold=0) == local.replace(tzinfo=None, fold=0)


def is_nonexistent(date: dt.date, minutes: int, tz: str) -> bool:
    z = get_zone(tz)
    return not _roundtrips(_local(date, minutes, z, 0)) and not _roundtrips(_local(date, minutes, z, 1))


def is_ambiguous_local(date: dt.date, minutes: int, tz: str) -> bool:
    z = get_zone(tz)
    a, b = _local(date, minutes, z, 0), _local(date, minutes, z, 1)
    return a.utcoffset() != b.utcoffset() and _roundtrips(a) and _roundtrips(b)


def wall_to_utc_all(date: dt.date, minutes: int, tz: str) -> list[dt.datetime]:
    """All naive-UTC instants for a local wall time: [] (nonexistent), [x] or [earlier, later] (ambiguous)."""
    z = get_zone(tz)
    out = []
    for fold in (0, 1):
        loc = _local(date, minutes, z, fold)
        if _roundtrips(loc):
            u = loc.astimezone(UTC).replace(tzinfo=None)
            if u not in out:
                out.append(u)
    return sorted(out)


def wall_to_utc(date: dt.date, minutes: int, tz: str, prefer: str = "earliest") -> dt.datetime:
    """Single instant for a range boundary. Ambiguous -> earliest/latest; nonexistent -> just after the gap."""
    vals = wall_to_utc_all(date, minutes, tz)
    if vals:
        return vals[0] if prefer == "earliest" else vals[-1]
    # nonexistent: interpret with the pre-transition offset, which lands after the gap
    z = get_zone(tz)
    return _local(date, minutes, z, 0).astimezone(UTC).replace(tzinfo=None)


def local_midnight_utc(date: dt.date, tz: str) -> dt.datetime:
    return wall_to_utc(date, 0, tz, "earliest")


def utc_offset_str(value: dt.datetime, tz: str) -> str:
    off = utc_to_local(value, tz).utcoffset() or dt.timedelta(0)
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


def tz_abbrev(value: dt.datetime, tz: str) -> str:
    """Abbreviation like 'GMT', 'BST', 'EST', 'IST'; numeric zones fall back to 'UTC+05:30'."""
    name = utc_to_local(value, tz).tzname() or ""
    if not name or name[0] in "+-" or name.isdigit():
        return utc_offset_str(value, tz)
    return name


def instant_is_ambiguous(value: dt.datetime, tz: str) -> bool:
    """True if this instant's local wall time occurs twice in tz (fall-back hour)."""
    loc = utc_to_local(value, tz)
    mins = loc.hour * 60 + loc.minute
    return is_ambiguous_local(loc.date(), mins, validate_tz(tz))


def slot_label(value: dt.datetime, tz: str, hour12: bool = False) -> str:
    """'14:00 GMT' / '2:00pm GMT'; ambiguous fall-back times add the UTC offset: '01:30 BST (UTC+01:00)'."""
    tz = validate_tz(tz)
    loc = utc_to_local(value, tz)
    if hour12:
        t = loc.strftime("%I:%M%p").lstrip("0").lower()
    else:
        t = loc.strftime("%H:%M")
    label = f"{t} {tz_abbrev(value, tz)}"
    if instant_is_ambiguous(value, tz):
        label += f" ({utc_offset_str(value, tz)})"
    return label


def format_local(value: Optional[dt.datetime], tz: str, fmt: str = "%A %d %B %Y, %H:%M") -> str:
    if value is None:
        return ""
    tz = validate_tz(tz)
    return f"{utc_to_local(value, tz).strftime(fmt)} {tz_abbrev(value, tz)}"


def local_date(value: dt.datetime, tz: str) -> dt.date:
    return utc_to_local(value, tz).date()
