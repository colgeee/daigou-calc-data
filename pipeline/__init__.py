"""Builds Daigou Calc's rates.json.gz and fx.json from official public sources."""
from datetime import UTC, date, datetime

VERSION = "1"


def build_date() -> date:
    return datetime.now(UTC).date()


def published_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def next_quarter_start(d: date) -> date:
    """First day of the calendar quarter containing d (rates are effective from quarter starts)."""
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)
