"""Time helpers. Protocol times are timezone-aware ISO-8601 and ranges half-open."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .errors import ValidationError


def parse_dt(value: Any, *, field: str = "time") -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} must be ISO 8601 date/time", details={"value": value}) from exc
    else:
        raise ValidationError(f"{field} is required")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError(f"{field} must include a UTC offset", details={"value": value})
    return dt


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def within(value: Any, start: Any, end: Any) -> bool:
    t = parse_dt(value)
    lo = parse_dt(start, field="from") if not isinstance(start, datetime) else start
    hi = parse_dt(end, field="until") if not isinstance(end, datetime) else end
    return lo <= t < hi


def require_int(value: Any, *, field: str, minimum: Optional[int] = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer number of cents", details={"value": value})
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field} must be >= {minimum}", details={"value": value})
    return value


def optional_text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)
