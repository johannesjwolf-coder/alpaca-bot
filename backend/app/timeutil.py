"""Zeit-Hilfen. Intern wird ausschliesslich UTC verwendet."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def trading_day(now: datetime) -> str:
    """Handelstag nach Börsenzeit New York (Tageswechsel um 00:00 ET)."""
    return now.astimezone(NEW_YORK).date().isoformat()
