"""Point-in-time discipline — two-layer filter [start, end+1 day) half-open UTC."""

from __future__ import annotations

import datetime as dt


def _parse_date(value: str | dt.date | dt.datetime | None) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Try common formats
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return (
                dt.datetime.strptime(s.split("T")[0], "%Y-%m-%d").date()
                if "T" in s and fmt == "%Y-%m-%d"
                else dt.datetime.strptime(s[:19], fmt[:19]).date()
            )  # type: ignore[unused-ignore]
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def in_window(
    observation_date: str | dt.date | dt.datetime | None,
    as_of: str | dt.date | dt.datetime,
    *,
    start: str | dt.date | dt.datetime | None = None,
) -> bool:
    """Half-open UTC window: [start, as_of+1 day).

    If observation_date is None → not in window (quarantined / PIT-invisible).
    If as_of is None → ValueError.
    """
    obs = _parse_date(observation_date)
    if obs is None:
        return False
    a = _parse_date(as_of)
    if a is None:
        raise ValueError("as_of must be a valid date")
    s = _parse_date(start) if start is not None else None
    # obs <= as_of and (start is None or obs >= start)
    if obs > a:  # type: ignore[operator]
        return False
    if s is not None and obs < s:  # type: ignore[operator]
        return False
    return True


def withhold_live_profile(
    observation_date: str | dt.date | dt.datetime | None,
    as_of: str | dt.date | dt.datetime,
) -> bool:
    """Return True if observation is beyond as_of and must be withheld."""
    return not in_window(observation_date, as_of)


def assert_not_stale(
    price_date: str | dt.date | dt.datetime | None, as_of: str | dt.date | dt.datetime
) -> None:
    if withhold_live_profile(price_date, as_of):
        raise ValueError(f"price_date {price_date} is beyond as_of {as_of} — stale")


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()
