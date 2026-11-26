"""Date range parsing, timezone handling, timeline bucketing, and cap settlement windows."""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
import re
from dataclasses import dataclass

from .httpkit import ApiError, iso_z

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

UTC = dt.timezone.utc
TZ_RE = re.compile(r"^[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_SPAN_DAYS = 400
GRANULARITIES = ("day", "week")
PERIODS = ("daily", "weekly", "monthly", "custom", "all")
WINDOWS = ("calendar", "rolling")
RESET_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CYCLE_DAYS_MAX = 92
CYCLE_DAYS_DEFAULT = 30
CYCLE_EPOCH = dt.date(1970, 1, 1)


def valid_timezone(name) -> bool:
    if not isinstance(name, str) or not TZ_RE.match(name) or len(name) > 64:
        return False
    if name == "UTC":
        return True
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def get_zone(name: str):
    if name == "UTC" or not valid_timezone(name):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # pragma: no cover
        return UTC


def today_in(tz_name: str, now: dt.datetime | None = None) -> dt.date:
    now = now or dt.datetime.now(UTC)
    return now.astimezone(get_zone(tz_name)).date()


def parse_date(value: str, field: str) -> dt.date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError("bad_request", "Parameter '%s' must be a date in YYYY-MM-DD format." % field)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ApiError("bad_request", "Parameter '%s' is not a valid calendar date." % field)


def parse_rfc3339(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        frac, rest = "", ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                rest = tail[i:]
                break
            frac += ch
        text = head + ("." + frac[:6] if frac else "") + rest
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def local_midnight_utc(day: dt.date, tz) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(UTC)


@dataclass(frozen=True)
class DateRange:
    start: dt.date
    end: dt.date
    granularity: str
    tz_name: str

    @property
    def tz(self):
        return get_zone(self.tz_name)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def bucket_days(self) -> int:
        return 7 if self.granularity == "week" else 1

    def start_ts(self) -> dt.datetime:
        return local_midnight_utc(self.start, self.tz)

    def end_ts(self) -> dt.datetime:
        return local_midnight_utc(self.end + dt.timedelta(days=1), self.tz)

    def buckets(self) -> list[dt.date]:
        step = dt.timedelta(days=self.bucket_days)
        out, cur = [], self.start
        while cur <= self.end:
            out.append(cur)
            cur += step
        return out

    def bucket_labels(self) -> list[str]:
        return [d.isoformat() for d in self.buckets()]

    def bucket_index(self, day) -> int | None:
        if day is None:
            return None
        if isinstance(day, str):
            try:
                day = dt.date.fromisoformat(day[:10])
            except ValueError:
                return None
        if isinstance(day, dt.datetime):
            day = day.astimezone(self.tz).date()
        if day < self.start or day > self.end:
            return None
        return (day - self.start).days // self.bucket_days

    def meta(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "granularity": self.granularity,
            "timezone": self.tz_name,
        }


def parse_range(params: dict, tz_name: str, default_days: int = 14, now: dt.datetime | None = None) -> DateRange:
    params = params or {}
    granularity = (params.get("granularity") or "day").strip().lower()
    if granularity not in GRANULARITIES:
        raise ApiError("bad_request", "Parameter 'granularity' must be 'day' or 'week'.")
    today = today_in(tz_name, now)
    end_raw = params.get("end") or ""
    start_raw = params.get("start") or ""
    end = parse_date(end_raw, "end") if end_raw else today
    if start_raw:
        start = parse_date(start_raw, "start")
    else:
        days = max(1, min(int(default_days or 14), MAX_SPAN_DAYS))
        start = end - dt.timedelta(days=days - 1)
    if start > end:
        raise ApiError("bad_request", "Parameter 'start' must not be after 'end'.")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ApiError("bad_request", "The date range must not exceed %d days." % MAX_SPAN_DAYS)
    return DateRange(start, end, granularity, tz_name)


# ---------------------------------------------------------------------------
# Cap settlement windows (daily / weekly / monthly / custom / all)
# ---------------------------------------------------------------------------

def reset_hour_minute(at) -> tuple[int, int]:
    m = RESET_TIME_RE.match(str(at or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def reset_weekday(weekday) -> int:
    try:
        value = int(weekday)
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value <= 6 else 0


def reset_day_of_month(day) -> int:
    try:
        value = int(day)
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 31 else 1


def cycle_length(days) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return CYCLE_DAYS_DEFAULT
    return value if 1 <= value <= CYCLE_DAYS_MAX else CYCLE_DAYS_DEFAULT


def cycle_start_date(value) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if DATE_RE.match(text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            pass
    return CYCLE_EPOCH


def _month_anchor(year: int, month: int, day: int, hour: int, minute: int, tz) -> dt.datetime:
    last_day = _calendar.monthrange(year, month)[1]
    return dt.datetime(year, month, min(day, last_day), hour, minute, tzinfo=tz)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def calendar_start(period: str, local_now: dt.datetime, weekday: int = 0, at: str = "00:00",
                   day: int = 1, cycle_days: int = CYCLE_DAYS_DEFAULT, cycle_start: str = "") -> dt.datetime:
    hour, minute = reset_hour_minute(at)
    tz = local_now.tzinfo or UTC
    if period == "daily":
        anchor = dt.datetime.combine(local_now.date(), dt.time(hour, minute), tzinfo=tz)
        if local_now < anchor:
            anchor = dt.datetime.combine(local_now.date() - dt.timedelta(days=1), dt.time(hour, minute), tzinfo=tz)
        return anchor
    if period == "weekly":
        wd = reset_weekday(weekday)
        delta = (local_now.weekday() - wd) % 7
        anchor = dt.datetime.combine(local_now.date() - dt.timedelta(days=delta), dt.time(hour, minute), tzinfo=tz)
        if local_now < anchor:
            anchor = dt.datetime.combine(anchor.date() - dt.timedelta(days=7), dt.time(hour, minute), tzinfo=tz)
        return anchor
    if period == "monthly":
        d = reset_day_of_month(day)
        anchor = _month_anchor(local_now.year, local_now.month, d, hour, minute, tz)
        if local_now < anchor:
            py, pm = _prev_month(local_now.year, local_now.month)
            anchor = _month_anchor(py, pm, d, hour, minute, tz)
        return anchor
    if period == "custom":
        length = cycle_length(cycle_days)
        origin = cycle_start_date(cycle_start)
        diff = (local_now.date() - origin).days
        steps = diff // length
        anchor_day = origin + dt.timedelta(days=steps * length)
        anchor = dt.datetime.combine(anchor_day, dt.time(hour, minute), tzinfo=tz)
        if local_now < anchor:
            anchor = dt.datetime.combine(anchor_day - dt.timedelta(days=length), dt.time(hour, minute), tzinfo=tz)
        return anchor
    return dt.datetime(2020, 1, 1, 0, 0, tzinfo=UTC)


def calendar_end(period: str, start_local: dt.datetime, weekday: int = 0, at: str = "00:00",
                 day: int = 1, cycle_days: int = CYCLE_DAYS_DEFAULT) -> dt.datetime | None:
    hour, minute = reset_hour_minute(at)
    tz = start_local.tzinfo or UTC
    if period == "daily":
        return dt.datetime.combine(start_local.date() + dt.timedelta(days=1), dt.time(hour, minute), tzinfo=tz)
    if period == "weekly":
        return dt.datetime.combine(start_local.date() + dt.timedelta(days=7), dt.time(hour, minute), tzinfo=tz)
    if period == "monthly":
        ny, nm = _next_month(start_local.year, start_local.month)
        return _month_anchor(ny, nm, reset_day_of_month(day), hour, minute, tz)
    if period == "custom":
        length = cycle_length(cycle_days)
        return dt.datetime.combine(start_local.date() + dt.timedelta(days=length), dt.time(hour, minute), tzinfo=tz)
    return None


def period_window(period: str, window: str = "calendar", now: dt.datetime | None = None,
                  tz: str = "UTC", weekday: int = 0, at: str = "00:00", day: int = 1,
                  cycle_days: int = CYCLE_DAYS_DEFAULT, cycle_start: str = "") -> dict:
    """Computes current settlement window: {start, end, period_end, next_reset, timezone, period, window}."""
    if period not in PERIODS:
        raise ApiError("bad_request", "Parameter 'period' must be one of: %s." % ", ".join(PERIODS))
    if window not in WINDOWS:
        raise ApiError("bad_request", "Parameter 'window' must be 'calendar' or 'rolling'.")
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    zone = get_zone(tz)
    if period == "all":
        start = dt.datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
        return {
            "start": start,
            "end": now,
            "period_end": None,
            "next_reset": None,
            "start_iso": iso_z(start),
            "end_iso": iso_z(now),
            "period_end_iso": None,
            "next_reset_iso": None,
            "timezone": tz,
            "period": period,
            "window": window,
        }
    if window == "rolling":
        delta_days = {"daily": 1, "weekly": 7, "monthly": 30, "custom": cycle_length(cycle_days)}[period]
        start = now - dt.timedelta(days=delta_days)
        next_reset = now + dt.timedelta(days=delta_days)
        return {
            "start": start,
            "end": now,
            "period_end": next_reset,
            "next_reset": next_reset,
            "start_iso": iso_z(start),
            "end_iso": iso_z(now),
            "period_end_iso": iso_z(next_reset),
            "next_reset_iso": iso_z(next_reset),
            "timezone": tz,
            "period": period,
            "window": window,
        }
    local_now = now.astimezone(zone)
    start_local = calendar_start(period, local_now, weekday=weekday, at=at, day=day,
                                 cycle_days=cycle_days, cycle_start=cycle_start)
    end_local = calendar_end(period, start_local, weekday=weekday, at=at, day=day, cycle_days=cycle_days)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC) if end_local is not None else None
    return {
        "start": start_utc,
        "end": now,
        "period_end": end_utc,
        "next_reset": end_utc,
        "start_iso": iso_z(start_utc),
        "end_iso": iso_z(now),
        "period_end_iso": iso_z(end_utc),
        "next_reset_iso": iso_z(end_utc),
        "timezone": tz,
        "period": period,
        "window": window,
    }


def month_bounds(month: str, tz_name: str = "UTC") -> tuple[dt.datetime, dt.datetime]:
    """(start_utc, end_utc) of calendar month YYYY-MM in tz_name."""
    year, mon = int(month[:4]), int(month[5:7])
    zone = get_zone(tz_name)
    start_local = dt.datetime(year, mon, 1, 0, 0, tzinfo=zone)
    ny, nm = _next_month(year, mon)
    end_local = dt.datetime(ny, nm, 1, 0, 0, tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
