"""Builds Daigou Calc's rates.json.gz from official public sources."""
from datetime import UTC, date, datetime

VERSION = "1"


def build_date() -> date:
    return datetime.now(UTC).date()


def published_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def quarter_start(d: date) -> date:
    """First day of the calendar quarter containing d (rates are effective from quarter
    starts). Named for what it returns: the previous name, `next_quarter_start`, described
    a quarter this never computes -- `effectiveDate` is the quarter the build sits in."""
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)
