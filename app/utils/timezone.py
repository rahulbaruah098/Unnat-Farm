"""UnnatFarm timezone utilities.

Storage policy:
- Persist event/audit timestamps in UTC (naive UTC for compatibility with the
  existing PyMongo configuration and historical records).
- Use Asia/Kolkata for business calendar dates and every user-facing timestamp.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
from zoneinfo import ZoneInfo

APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)
UTC = timezone.utc


def utc_now() -> datetime:
    """Return current UTC as a naive datetime for existing Mongo compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


def india_now() -> datetime:
    """Return current timezone-aware datetime in Asia/Kolkata."""
    return datetime.now(APP_TIMEZONE)


def business_today() -> date:
    """Return today's business date in Asia/Kolkata, independent of server TZ."""
    return india_now().date()


def _coerce_datetime(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=APP_TIMEZONE)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
    return None


def to_india_datetime(value):
    """Convert a stored UTC datetime/ISO value to Asia/Kolkata.

    Existing Mongo datetimes are naive and are treated as UTC. Aware datetimes
    keep their actual instant and are converted normally.
    """
    parsed = _coerce_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(APP_TIMEZONE)


def format_ist_datetime(value, fmt="%d %b %Y, %I:%M %p", default="-"):
    converted = to_india_datetime(value)
    if converted is None:
        return default if value in (None, "") else str(value)
    return converted.strftime(fmt)


def format_ist_date(value, fmt="%d %b %Y", default="-"):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime(fmt)
    converted = to_india_datetime(value)
    if converted is None:
        return default if value in (None, "") else str(value)
    return converted.strftime(fmt)
