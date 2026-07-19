"""Project an OMH ``effective_time_frame`` onto queryable, FHIR-shaped columns.

An OMH data point carries its timing under ``body.effective_time_frame`` in one of two
shapes (see the IEEE 1752 / Open mHealth ``time-frame`` and ``time-interval`` schemas):

    effective_time_frame
    ├── date_time                                   -> FHIR effectiveDateTime (an instant)
    └── time_interval                               -> FHIR effectivePeriod (start + end)
        ├── start_date_time + end_date_time
        ├── start_date_time + duration
        ├── end_date_time + duration
        └── date + part_of_day

``extract_effective_time_frame`` flattens either shape into the three columns the
Observation model elevates out of the JSON blob:
``(effective_date_time, effective_period_start, effective_period_end)``. An instant sets
only the first; every interval form sets the two period bounds. The ``omh_data`` blob
remains the source of truth, so these columns are a derived index -- an unrecognised or
malformed time frame simply yields ``(None, None, None)`` (the row is not time-queryable)
rather than raising.

Timezone rule: a parsed value with no offset is interpreted as UTC, matching the legacy
Open mHealth convention that a missing timezone means UTC.
"""

import calendar
from datetime import UTC, datetime, timedelta

from django.utils.dateparse import parse_date, parse_datetime

# part_of_day windows as [start_hour, end_hour) within the given (UTC) day. ``evening`` runs
# to hour 24, i.e. the following midnight -- expressed as an hour offset so it stays valid.
_PART_OF_DAY_WINDOWS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 24),
    "night": (0, 6),
}

# Duration units from duration-unit-value-1.0.json that map to a fixed number of seconds.
# Calendar units (``Mo``, ``yr``) depend on the anchor date and are handled separately.
_DURATION_SECONDS = {
    "ps": 1e-12,
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "sec": 1,
    "min": 60,
    "h": 3600,
    "d": 86400,
    "wk": 604800,
}


def extract_effective_time_frame(omh_data):
    """Return ``(effective_date_time, effective_period_start, effective_period_end)``.

    Each element is an aware ``datetime`` or ``None``. A malformed or absent time frame
    yields ``(None, None, None)``.
    """
    body = omh_data.get("body") if isinstance(omh_data, dict) else None
    time_frame = body.get("effective_time_frame") if isinstance(body, dict) else None
    if not isinstance(time_frame, dict):
        return (None, None, None)

    if "date_time" in time_frame:
        return (_ensure_aware(_parse_dt(time_frame.get("date_time"))), None, None)

    interval = time_frame.get("time_interval")
    if isinstance(interval, dict):
        return _interval_to_period(interval)

    return (None, None, None)


def _interval_to_period(interval):
    start = _ensure_aware(_parse_dt(interval.get("start_date_time")))
    end = _ensure_aware(_parse_dt(interval.get("end_date_time")))
    duration = interval.get("duration")

    if start and end:
        return (None, start, end)
    if start and _is_duration(duration):
        return (None, start, _shift(start, duration, +1))
    if end and _is_duration(duration):
        return (None, _shift(end, duration, -1), end)

    day = _parse_day(interval.get("date"))
    part = interval.get("part_of_day")
    if day is not None and part in _PART_OF_DAY_WINDOWS:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        start_hour, end_hour = _PART_OF_DAY_WINDOWS[part]
        return (None, day_start + timedelta(hours=start_hour), day_start + timedelta(hours=end_hour))

    return (None, None, None)


def _parse_dt(value):
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value)
    except ValueError:
        return None


def _parse_day(value):
    if not isinstance(value, str):
        return None
    try:
        return parse_date(value)
    except ValueError:
        return None


def _ensure_aware(value):
    """A datetime with no tzinfo is UTC per the legacy Open mHealth convention."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _is_duration(duration):
    return isinstance(duration, dict) and duration.get("unit") is not None and duration.get("value") is not None


def _shift(anchor, duration, sign):
    """Move ``anchor`` by an OMH duration; ``sign`` is +1 forward, -1 back. Returns None for
    an unrecognised unit so the caller drops the (unqueryable) bound."""
    unit = duration.get("unit")
    try:
        value = float(duration.get("value"))
    except (TypeError, ValueError):
        return None
    if unit in _DURATION_SECONDS:
        return anchor + timedelta(seconds=sign * value * _DURATION_SECONDS[unit])
    if unit == "Mo":
        return _add_months(anchor, sign * int(value))
    if unit == "yr":
        return _add_months(anchor, sign * int(value) * 12)
    return None


def _add_months(anchor, months):
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)
