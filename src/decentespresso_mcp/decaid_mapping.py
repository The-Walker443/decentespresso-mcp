"""Decaid responses -> archive rows (SPEC §20.4).

Two normalisations are binding here, both arising from the migration in M8
(2/n) and measured against the real 168 shots:

**Time.** The archive keeps UTC throughout. Decaid does not do so uniformly:
shots imported from the de1app already carry their timestamp in UTC, natively
recorded ones carry local time without a zone. Measured across all shots: 88
imported with ``timestamp == createdAt``, 80 native with exactly two hours of
offset. Conversion therefore runs through ``Europe/Berlin`` with full daylight
saving handling - a fixed offset would be wrong at the end of October. Where a
timestamp came from is recorded per shot in ``time_source``.

**Rating.** Decaid creates imported shots with ``enjoyment: 0.0``. That is not
a judgement but "not rated": 75 of the 88 imported ones sit like that, while
real ratings run from 40 to 100 and **not a single** natively recorded shot
ever carries 0.0. A 0 from the import era is therefore archived as ``NULL``.
Without this rule 75 phantom ratings would enter the archive, and guards such
as ``audit_archive`` would take them at face value.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .decaid_client import measurement_times

log = logging.getLogger(__name__)

#: Local time of the machine. As a zone, not an offset - otherwise every shot
#: after the last Sunday in October would be an hour out. If this line fails at
#: startup the time zone database is missing; that is what tzdata is in the
#: dependencies for.
MACHINE_TZ = ZoneInfo("Europe/Berlin")

#: Shots imported from the de1app. The number is the start time as Unix time;
#: the matching in the migration script hangs on it too.
DE1APP_ID = re.compile(r"^de1app-(\d{9,13})$")

#: Values of ``time_source`` in the database.
SOURCE_UTC = "utc"
SOURCE_LOCAL = "local_berlin"

#: Tolerance for the cross-check of ``timestamp`` against ``createdAt``.
_CROSSCHECK_TOLERANCE_S = 120

#: Time series: archive column on the left, path inside ``measurements`` on
#: the right.
MACHINE_FIELDS = {
    "pressure": "pressure",
    "flow_in": "flow",                          # pump flow
    "temp_mix": "mixTemperature",
    "temp_basket": "groupTemperature",
    "target_pressure": "targetPressure",        # new in M8: target per data point
    "target_flow": "targetFlow",
    "target_temp_mix": "targetMixTemperature",
    "target_temp_basket": "targetGroupTemperature",
    "profile_frame": "profileFrame",
}

SCALE_FIELDS = {
    "weight": "weight",
    "flow_out": "weightFlow",                   # derived from the scale
}


def is_import_era(shot_id: str) -> bool:
    """Did this shot come from the de1app import rather than Decaid's own recording?"""
    return bool(DE1APP_ID.match(str(shot_id or "")))


def time_source_of(shot: dict[str, Any]) -> str:
    """Whether a shot's timestamp is UTC or local time.

    The decision rests on the identifier, because that is a stable convention.
    ``createdAt`` serves as a cross-check: if the two disagree, Decaid has
    changed its behaviour, and that should be noticed rather than run quietly
    wrong.
    """
    source = SOURCE_UTC if is_import_era(shot.get("id", "")) else SOURCE_LOCAL

    stamp = _naive(shot.get("timestamp"))
    created = _naive(shot.get("createdAt"))
    if stamp is not None and created is not None:
        drift = abs((stamp - created).total_seconds())
        looks_utc = drift <= _CROSSCHECK_TOLERANCE_S
        if looks_utc != (source == SOURCE_UTC):
            log.warning(
                "time source crosscheck failed",
                extra={"fields": {"shot": shot.get("id"), "assumed": source,
                                  "drift_s": round(drift)}},
            )
    return source


def started_at_utc(shot: dict[str, Any]) -> tuple[str | None, str]:
    """``(ISO8601 in UTC, time_source)`` for one shot.

    For local time inside the ambiguous hour when the clocks go back, the first
    reading is taken (``fold=0``, so still summer time). A decision has to be
    made; the earlier one is the one that fits the rest of the day.
    """
    source = time_source_of(shot)
    naive = _naive(shot.get("timestamp"))
    if naive is None:
        return None, source

    if source == SOURCE_UTC:
        moment = naive.replace(tzinfo=UTC)
    else:
        moment = naive.replace(tzinfo=MACHINE_TZ, fold=0).astimezone(UTC)
    return _iso(moment), source


def normalize_enjoyment(shot: dict[str, Any]) -> float | None:
    """Rating, with the zero rule from the module docstring applied."""
    annotations = shot.get("annotations") or {}
    value = annotations.get("enjoyment")
    if value is None:
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if rating == 0 and is_import_era(shot.get("id", "")):
        # 75 of 88 imported shots sit like this - it is the import default,
        # not a rating.
        return None
    return rating


def shot_row_from_decaid(detail: dict[str, Any], synced_at: str) -> dict[str, Any]:
    """Detail response -> a row for ``shots``."""
    annotations = detail.get("annotations") or {}
    workflow = detail.get("workflow") or {}
    context = workflow.get("context") or {}
    extras = context.get("extras") or {}

    started, source = started_at_utc(detail)
    dose = _number(annotations.get("actualDoseWeight"))
    yielded = _number(annotations.get("actualYield"))

    times = measurement_times(detail.get("measurements") or [])

    return {
        "id": detail.get("id"),
        "started_at": started,
        "time_source": source,
        "created_at": _iso_or_none(detail.get("createdAt")),
        "updated_at": _iso_or_none(detail.get("updatedAt")),
        "duration_s": round(times[-1], 3) if times else None,
        "stop_reason": _text(detail.get("stopReason")),
        "workflow_id": _text(workflow.get("id")),
        "profile_name": _text((workflow.get("profile") or {}).get("title")),
        # The shot only knows its batch; which bean that is sits on the batch.
        # Ingestion fills bean_id in afterwards.
        "bean_batch_id": _text(context.get("beanBatchId")),
        "bean_id": None,                       # ingestion resolves this via the batch
        "bean_name": _text(context.get("coffeeName")),
        "bean_roaster": _text(context.get("coffeeRoaster")),
        "basket_name": _text(extras.get("basketName")),
        "grinder_model": _text(context.get("grinderModel")),
        "grinder_setting": _text(context.get("grinderSetting")),
        "target_dose_g": _number(context.get("targetDoseWeight")),
        "target_yield_g": _number(context.get("targetYield")),
        "dose_g": dose,
        "yield_g": yielded,
        "ratio": round(yielded / dose, 3) if dose and yielded else None,
        "enjoyment": normalize_enjoyment(detail),
        "notes": _text(annotations.get("espressoNotes")) or _text(detail.get("shotNotes")),
        # Without the measurements: those live in shot_series, and a detail
        # response weighs about 140 kB with them - across all shots that would
        # be a multiple of the rest of the archive, stored twice.
        "raw_json": json.dumps(
            {k: v for k, v in detail.items() if k != "measurements"},
            ensure_ascii=False, separators=(",", ":"),
        ),
        "synced_at": synced_at,
    }


def series_rows_from_decaid(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """``measurements`` -> rows for ``shot_series``.

    ``elapsed`` is derived from the data points' own timestamps, because Decaid
    provides no ``time`` field (T12). ``state``/``substate`` and
    ``profile_frame`` come along - ``metrics`` derives the end of preinfusion
    from them.
    """
    measurements = detail.get("measurements") or []
    times = measurement_times(measurements)
    shot_id = detail.get("id")

    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for elapsed, point in zip(times, measurements, strict=True):
        key = round(elapsed, 3)
        if key in seen:
            # elapsed is part of the primary key.
            continue
        seen.add(key)

        machine = point.get("machine") or {}
        scale = point.get("scale") or {}
        state = machine.get("state") or {}

        row: dict[str, Any] = {"shot_id": shot_id, "elapsed": key}
        for column, field in MACHINE_FIELDS.items():
            row[column] = _number(machine.get(field))
        for column, field in SCALE_FIELDS.items():
            row[column] = _number(scale.get(field))
        row["state"] = _text(state.get("state"))
        row["substate"] = _text(state.get("substate"))
        row["volume"] = _number(point.get("volume"))
        rows.append(row)
    return rows


def bean_row_from_decaid(bean: dict[str, Any], synced_at: str) -> dict[str, Any]:
    """Bean -> a row for ``beans``."""
    return {
        "id": bean.get("id"),
        "name": _text(bean.get("name")),
        "roaster": _text(bean.get("roaster")),
        "species": _text(bean.get("species")),
        "processing": _text(bean.get("processing")),
        "decaf": _flag(bean.get("decaf")),
        "archived": _flag(bean.get("archived")),
        "notes": _text(bean.get("notes")),
        "created_at": _iso_or_none(bean.get("createdAt")),
        "updated_at": _iso_or_none(bean.get("updatedAt")),
        "raw_json": json.dumps(bean, ensure_ascii=False, separators=(",", ":")),
        "synced_at": synced_at,
    }


def batch_row_from_decaid(batch: dict[str, Any], synced_at: str) -> dict[str, Any]:
    """Batch -> a row for ``bean_batches``.

    Decaid keeps no ``unfreezeDate`` field of its own; it appears, if at all,
    in the extras. Frozen time does not count towards bean age, which is why
    both are carried along.
    """
    extras = batch.get("extras") or {}
    return {
        "id": batch.get("id"),
        "bean_id": _text(batch.get("beanId")),
        "roast_date": _date(batch.get("roastDate")),
        "buy_date": _date(batch.get("buyDate")),
        "freeze_date": _date(batch.get("freezeDate")),
        "unfreeze_date": _date(batch.get("unfreezeDate") or extras.get("unfreezeDate")),
        "frozen": _flag(batch.get("frozen")),
        "archived": _flag(batch.get("archived")),
        "created_at": _iso_or_none(batch.get("createdAt")),
        "updated_at": _iso_or_none(batch.get("updatedAt")),
        "raw_json": json.dumps(batch, ensure_ascii=False, separators=(",", ":")),
        "synced_at": synced_at,
    }


# ------------------------------------------------------------------ Helpers


def _naive(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_or_none(value: Any) -> str | None:
    """``createdAt``/``updatedAt`` carry a Z and are therefore already UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _iso(moment)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> int | None:
    """Booleans as 0/1 - SQLite has no BOOLEAN type."""
    if value is None:
        return None
    return 1 if value else 0


def _date(value: Any) -> str | None:
    """Dates stay dates.

    Roast and freeze dates are day-level. Forcing them into a time zone would
    shift them by a day without a time of day ever having been recorded.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:10]
