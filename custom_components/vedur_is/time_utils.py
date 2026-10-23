"""UTC boundaries for Vedur timestamps, which often omit an offset."""

from datetime import datetime, timezone


def utc_naive(value: datetime) -> datetime:
    """Normalize source timestamps to the integration's internal UTC timeline."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def utc_now() -> datetime:
    """Return UTC independently of the host operating system timezone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_isoformat(value: datetime) -> str:
    """Return an unambiguous RFC 3339 timestamp for Home Assistant."""
    return utc_naive(value).replace(tzinfo=timezone.utc).isoformat()
