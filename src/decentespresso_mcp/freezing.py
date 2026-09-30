"""Freeze and thaw history of a batch, from Decaid's fields or Beanie's events (T57).

Two writers keep it two ways. Decaid has ``freezeDate``/``unfreezeDate`` - one
period. Beanie (github.com/giladger/Beanie, src/domain/beanFreshness.ts and
src/api/gateway.ts at a54a625) keeps a list under ``extras.storageEvents`` -
``[{type: "frozen"|"thawed", at: ISO}]`` - sets ``frozen`` alongside, and never
the two date fields. A batch can therefore have been in the freezer several
times, and only the events know.

Reading: explicit Decaid fields win; otherwise the events. Writing: whatever
this server sets in the fields is mirrored into the events, in Beanie's format,
so Beanie sees it too. ``extras`` is replaced whole on a PUT (T57), so the
events always go out with the rest of ``extras`` as it was read.

Pure functions; the clock is handed in.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

FIELDS = "fields"
EVENTS = "storageEvents"

Period = tuple[date, date | None]


def events_of(raw: Any) -> list[dict[str, str]]:
    """Beanie's normalizeStorageEvents: valid entries only, in time order."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    valid = [e for e in raw if isinstance(e, dict) and e.get("type") in ("frozen", "thawed")
             and _instant(e.get("at")) is not None]
    return sorted(valid, key=lambda e: _instant(e["at"]))


def frozen_periods(batch: dict[str, Any]) -> tuple[list[Period], str | None, bool]:
    """``(periods, source, certain)``; an open period has no end.

    From the fields: one period - or none that can be counted when the batch
    was frozen and thawed without a thaw date, which leaves the age an upper
    bound (``certain`` false, as before). From the events: Beanie's intervals -
    a ``frozen`` opens one, the next ``thawed`` closes it, a second ``frozen``
    without a thaw in between keeps the earlier start.
    """
    frozen_since = _day(batch.get("freeze_date"))
    if frozen_since is not None:
        thawed = _day(batch.get("unfreeze_date"))
        if thawed is not None:
            return [(frozen_since, thawed)], FIELDS, True
        if batch.get("frozen"):
            return [(frozen_since, None)], FIELDS, True
        return [], FIELDS, False
    periods: list[Period] = []
    opened: date | None = None
    events = events_of(batch.get("storage_events"))
    for event in events:
        day = _instant(event["at"]).date()
        if event["type"] == "frozen":
            opened = opened or day
        elif opened is not None:
            periods.append((opened, day))
            opened = None
    if opened is not None:
        periods.append((opened, None))
    if events:
        return periods, EVENTS, True
    return [], None, not batch.get("frozen")


def frozen_days(periods: list[Period], roasted: date, until: date) -> int:
    """Days spent frozen between roast and ``until``, every period summed."""
    total = 0
    for start, end in periods:
        start = max(start, roasted)
        stop = min(end or until, until)
        total += max(0, (stop - start).days)
    return total


def mirrored_events(
    current: list[dict[str, str]], fields: dict[str, Any], now: datetime,
) -> list[dict[str, str]] | None:
    """The events after a write of Decaid's freeze fields, or ``None`` if untouched.

    Beanie's appendBatchStorageEvent: the same type as the latest event
    replaces its time (keeping the time of day, as its date input does);
    another type is appended. A freeze and a thaw in one write come out as
    both, in that order.
    """
    changes: list[tuple[str, Any]] = []
    if fields.get("freezeDate") or fields.get("frozen") is True:
        changes.append(("frozen", fields.get("freezeDate")))
    if fields.get("unfreezeDate") or fields.get("frozen") is False:
        changes.append(("thawed", fields.get("unfreezeDate")))
    if not changes:
        return None
    events = [dict(e) for e in current]
    for kind, day in changes:
        latest = events[-1] if events else None
        previous = _instant(latest["at"]) if latest and latest["type"] == kind else None
        at = _iso(day, previous, now)
        if previous is not None:
            events[-1] = {"type": kind, "at": at}
        else:
            events.append({"type": kind, "at": at})
    return sorted(events, key=lambda e: _instant(e["at"]))


def _iso(day: Any, previous: datetime | None, now: datetime) -> str:
    """``Date.toISOString()`` of the day at the previous (or current) UTC time."""
    clock = (previous or now).astimezone(UTC)
    if day:
        base = _day(day)
        if base is not None:
            clock = datetime(base.year, base.month, base.day, clock.hour, clock.minute,
                             clock.second, tzinfo=UTC)
    return clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _day(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    moment = _instant(value) if isinstance(value, str) and "T" in value else None
    if moment is not None:
        return moment.astimezone(UTC).date()
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


__all__ = ["EVENTS", "FIELDS", "events_of", "frozen_days", "frozen_periods",
           "mirrored_events"]
