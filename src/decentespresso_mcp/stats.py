"""Statistics over a period (SPEC §9.4).

Deliberately lean. The archive answers "what did I pull, from what, and how did
it taste" - not every question that could be asked of it. Anything that needs a
chart does not belong in a tool response.

Pure functions over rows, like ``guards``: no network, no database, and the
clock is handed in. What this module does *not* do is decide what a shot is -
that is ``is_real_shot``, and it is the only judgement here.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

#: Profiles that are maintenance rather than coffee. Matched case-insensitively
#: as substrings of the profile name, because the names are free text on the
#: machine ("Cleaning/Forward Flush x5", "Test/temperature calibration").
MAINTENANCE_PATTERNS = ("clean", "flush", "rinse", "descal", "calibrat", "purge")

#: A shot that produced essentially nothing is not a shot for statistics.
#: Measured across 171 shots: 22 sit below 5 g. Of those, only 12 carry a
#: maintenance profile name - the other 10 ran under "Default" or "D-Flow" and
#: are aborts. Filtering on the name alone would have counted them as coffee,
#: which is why there are two criteria rather than the one the brief names.
MIN_REAL_YIELD_G = 5.0

#: How many entries a top list carries. More than this and the answer stops
#: being a summary.
TOP_N = 5

_SHORTHAND = re.compile(r"^(\d+)\s*([hdwmy])$", re.I)
_UNITS = {"h": "hours", "d": "days", "w": "weeks", "m": "days", "y": "days"}
_SCALE = {"h": 1, "d": 1, "w": 1, "m": 30, "y": 365}


def parse_period(period: str, *, now: datetime) -> tuple[datetime, datetime]:
    """``"30d"`` or an ISO date -> ``(from, to)``, both UTC.

    An ISO date means "since then". A shorthand means "the last N of these".
    """
    text = (period or "").strip()
    match = _SHORTHAND.match(text)
    if match:
        count, unit = int(match.group(1)), match.group(2).lower()
        delta = timedelta(**{_UNITS[unit]: count * _SCALE[unit]})
        return now - delta, now

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{period!r} is neither an ISO date (2026-09-01) nor a shorthand "
            "like 30d, 12h, 4w"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, now


def is_real_shot(shot: Mapping[str, Any]) -> bool:
    """Coffee someone meant to drink.

    Two criteria, because one is not enough (see ``MIN_REAL_YIELD_G``): the
    profile must not be a maintenance profile, and something must have come out.
    A shot with no recorded yield counts as real - the scale may simply have
    been off, and dropping it would quietly shrink the archive.
    """
    name = (shot.get("profile_name") or "").lower()
    if any(pattern in name for pattern in MAINTENANCE_PATTERNS):
        return False
    yielded = shot.get("yield_g")
    if yielded is not None and yielded < MIN_REAL_YIELD_G:
        return False
    return True


def summarise(
    shots: Sequence[Mapping[str, Any]],
    *,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    """The figures for one period. ``shots`` are already filtered to it."""
    real = [s for s in shots if is_real_shot(s)]
    excluded = len(shots) - len(real)
    days = max(1.0, (until - since).total_seconds() / 86400)

    rated = [s["enjoyment"] for s in real if s.get("enjoyment") is not None]

    return {
        "from": _iso(since),
        "to": _iso(until),
        "days": round(days, 1),
        "shots": len(real),
        "excluded": excluded,
        "per_day": round(len(real) / days, 2),
        "enjoyment": {
            "rated": len(rated),
            "unrated": len(real) - len(rated),
            "mean": _round(statistics.fmean(rated), 1) if rated else None,
        },
        "averages": {
            "dose_g": _avg(real, "dose_g", 1),
            "yield_g": _avg(real, "yield_g", 1),
            "ratio": _avg(real, "ratio", 2),
            "duration_s": _avg(real, "duration_s", 1),
        },
        "beans": _top(real, "bean_name"),
        "profiles": _top(real, "profile_name"),
        "grind_by_bean": _grind_by_bean(real),
        "busiest_day": _busiest_day(shots),
    }


def compare(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    """Deltas against the period of the same length before this one."""
    deltas: dict[str, Any] = {
        "shots": current["shots"] - previous["shots"],
        "per_day": _round(current["per_day"] - previous["per_day"], 2),
    }
    for key in ("dose_g", "yield_g", "ratio", "duration_s"):
        a, b = current["averages"][key], previous["averages"][key]
        deltas[key] = _round(a - b, 2) if a is not None and b is not None else None
    a, b = current["enjoyment"]["mean"], previous["enjoyment"]["mean"]
    deltas["enjoyment_mean"] = (
        _round(a - b, 1) if a is not None and b is not None else None
    )
    return {"previous": previous, "deltas": deltas}


def batch_usage(
    shots: Sequence[Mapping[str, Any]],
    batches: Mapping[str, Mapping[str, Any]],
    *,
    at: datetime,
) -> list[dict[str, Any]]:
    """What each batch was used for in the period.

    **Not** an estimate of how much is left. The brief asks for "~N shots
    remaining" from Decaid's ``weightRemaining`` and the batch's mean dose, and
    that field does not exist: a batch carries ``id``, ``beanId``, ``roastDate``,
    ``buyDate``, ``freezeDate``, ``frozen``, ``archived`` and the two timestamps,
    and nothing else - verified against both the list and the single-item
    endpoint. Without a starting weight there is no honest remainder to compute,
    so what is reported is what was actually used.
    """
    by_batch: dict[str, list[Mapping[str, Any]]] = {}
    for shot in shots:
        if not is_real_shot(shot):
            continue
        batch_id = shot.get("bean_batch_id")
        if batch_id:
            by_batch.setdefault(str(batch_id), []).append(shot)

    out = []
    for batch_id, used in sorted(by_batch.items(), key=lambda kv: -len(kv[1])):
        batch = batches.get(batch_id) or {}
        doses = [d for s in used if (d := s.get("dose_g")) is not None]
        entry: dict[str, Any] = {
            "batch_id": batch_id,
            "bean": used[0].get("bean_name"),
            "shots": len(used),
            "coffee_used_g": _round(sum(doses), 1) if doses else None,
            "mean_dose_g": _round(statistics.fmean(doses), 1) if doses else None,
        }
        roast = _date(batch.get("roast_date"))
        if roast is not None:
            entry["days_since_roast"] = (at.date() - roast).days
        if batch.get("frozen"):
            entry["frozen"] = True
        out.append(entry)
    return out


# ------------------------------------------------------------------ Helpers


def _top(shots: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for shot in shots:
        label = shot.get(key)
        if label:
            groups.setdefault(str(label), []).append(shot)

    rows = []
    for label, group in groups.items():
        rated = [s["enjoyment"] for s in group if s.get("enjoyment") is not None]
        rows.append({
            "name": label,
            "shots": len(group),
            "mean_enjoyment": _round(statistics.fmean(rated), 1) if rated else None,
            "rated": len(rated),
        })
    rows.sort(key=lambda r: (-r["shots"], r["name"]))
    return rows[:TOP_N]


def _same_setting(a: str, b: str) -> bool:
    """Is this the same position on the dial, written differently?

    The field is free text typed on a tablet, and the archive holds "2.7",
    "2.70" and "2,8" for the same grinder. Comparing the strings counted 13
    changes on a bean that was actually moved 9 times, which turns a useful
    figure into noise.
    """
    def value(text: str) -> float | str:
        try:
            return float(text.strip().replace(",", "."))
        except ValueError:
            return text.strip().lower()
    return value(a) == value(b)


def _grind_by_bean(shots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Which grind settings a bean was pulled at, oldest first.

    The settings are reported as they were typed - "4,2" is what the operator
    wrote - but two spellings of the same number do not count as a change.
    """
    ordered: dict[str, list[str]] = {}
    for shot in sorted(shots, key=lambda s: s.get("started_at") or ""):
        bean = shot.get("bean_name")
        grind = shot.get("grinder_setting")
        if not bean or not grind:
            continue
        settings = ordered.setdefault(str(bean), [])
        if not settings or not _same_setting(settings[-1], str(grind)):
            settings.append(str(grind))
    return [
        {"bean": bean, "settings": settings, "changes": len(settings) - 1}
        for bean, settings in sorted(ordered.items(), key=lambda kv: -len(kv[1]))
        if settings
    ][:TOP_N]


def _busiest_day(shots: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Counted over *all* shots, maintenance included.

    A day with thirty flushes was a busy day at the machine even though it
    produced three coffees, and hiding that would make the number confusing
    rather than clean.
    """
    per_day: dict[str, list[Mapping[str, Any]]] = {}
    for shot in shots:
        started = shot.get("started_at")
        if started:
            per_day.setdefault(str(started)[:10], []).append(shot)
    if not per_day:
        return None
    day, group = max(per_day.items(), key=lambda kv: len(kv[1]))
    real = sum(1 for s in group if is_real_shot(s))
    return {"date": day, "total": len(group), "real_shots": real,
            "maintenance": len(group) - real}


def _avg(shots: Sequence[Mapping[str, Any]], key: str, digits: int) -> float | None:
    values = [v for s in shots if (v := s.get(key)) is not None]
    return _round(statistics.fmean(values), digits) if values else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(value: Any):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None
