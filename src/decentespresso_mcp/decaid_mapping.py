"""Decaid responses -> archive rows (SPEC §5).

Two normalisations are binding here, both measured against the whole archive:

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

**Scale.** The archive keeps ratings on Decaid's 0-10 scale (T46). Read from a
tablet still on 0-100, they are converted by Decaid's own migration rule, and
the values that rule cannot decide are kept as they are and marked - see
:func:`enjoyment_of`.
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
    "target_pressure": "targetPressure",        # the target at each data point
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


#: The scale Decaid keeps ``annotations.enjoyment`` on. Up to 0.8.6 it passed
#: de1app's and Visualizer's 0-100 through; decentespresso/decaid#887 made it
#: Decaid's own 0-10 field (T46). The archive keeps 0-10 throughout.
SCALE_100 = "0-100"
SCALE_10 = "0-10"
ENJOYMENT_MAX = 10.0


def _untouched_import(shot: dict[str, Any]) -> bool:
    """Decaid's own test for a de1app import nobody has edited since (#887).

    Revision stamps set at import time and no content change afterwards. The
    strings are compared as Decaid compares them in its migration.
    """
    created, updated = shot.get("createdAt"), shot.get("updatedAt")
    # Decaid's prefix test, not is_import_era: the two must pick the same rows.
    return (str(shot.get("id") or "").startswith("de1app-")
            and bool(created) and bool(updated)
            and created != shot.get("timestamp") and str(updated) <= str(created))


def enjoyment_of(shot: dict[str, Any], scale: str = SCALE_100) -> tuple[float | None, bool]:
    """``(rating on 0-10, ambiguous)``, with the zero rule applied first.

    On a 0-100 tablet the conversion is Decaid's own schema-6 rule, so the
    archive holds before the update what the tablet will hold after it: over
    10 is divided by ten, always; 10 and below only on an untouched de1app
    import. Any other value from 1 to 10 cannot be told apart - a low 0-100
    rating, DYE2's raw star index (dye2#7) or a 0-10 write by DYE2 0.1.15 on a
    0.8.6 tablet all look the same. Decaid leaves those as they are, and so
    does this; ``ambiguous`` says so. 0 is 0 on either scale.
    """
    annotations = shot.get("annotations") or {}
    value = annotations.get("enjoyment")
    if value is None:
        return None, False
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None, False
    if rating == 0 and is_import_era(shot.get("id", "")):
        # 75 of 88 imported shots sit like this - it is the import default,
        # not a rating.
        return None, False
    if scale == SCALE_10 or rating == 0:
        return rating, False
    if rating > ENJOYMENT_MAX or _untouched_import(shot):
        return round(min(rating / 10, ENJOYMENT_MAX), 3), False
    return rating, True


def normalize_enjoyment(shot: dict[str, Any], scale: str = SCALE_100) -> float | None:
    """The rating on the archive's 0-10 scale; see :func:`enjoyment_of`."""
    return enjoyment_of(shot, scale)[0]


def annotation_fields(shot: dict[str, Any], scale: str = SCALE_100) -> dict[str, Any]:
    """The columns that come from ``annotations``, from a detail or a list item.

    Separate because the list carries annotations too: re-reading them
    after Decaid rescaled its ratings needs no detail request (T46).
    """
    annotations = shot.get("annotations") or {}
    dose = _number(annotations.get("actualDoseWeight"))
    yielded = _number(annotations.get("actualYield"))
    enjoyment, ambiguous = enjoyment_of(shot, scale)
    return {
        "dose_g": dose,
        "yield_g": yielded,
        "ratio": round(yielded / dose, 3) if dose and yielded else None,
        "enjoyment": enjoyment,
        "enjoyment_ambiguous": int(ambiguous),
        "notes": _text(annotations.get("espressoNotes")) or _text(shot.get("shotNotes")),
    }


def shot_row_from_decaid(
    detail: dict[str, Any], synced_at: str, scale: str = SCALE_100,
) -> dict[str, Any]:
    """Detail response -> a row for ``shots``. ``scale`` is the tablet's (T46)."""
    workflow = detail.get("workflow") or {}
    context = workflow.get("context") or {}
    extras = context.get("extras") or {}

    started, source = started_at_utc(detail)

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
        **annotation_fields(detail, scale),
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
    """Bean -> a row for ``beans``.

    Origin comes as four separate fields plus an altitude range. Decaid's UI
    shows grey placeholder text in the empty ones ("washed, natural, honey…"),
    but the API omits an unset field entirely rather than sending the
    placeholder - checked across every bean on the live instance. So the
    ``.get`` default of ``None`` is the whole protection needed, and a test
    pins it: a placeholder imported as a value would be a fact nobody typed.
    """
    return {
        "id": bean.get("id"),
        "name": _text(bean.get("name")),
        "roaster": _text(bean.get("roaster")),
        "species": _text(bean.get("species")),
        "processing": _text(bean.get("processing")),
        "decaf": _flag(bean.get("decaf")),
        "archived": _flag(bean.get("archived")),
        "notes": _text(bean.get("notes")),
        "country": _text(bean.get("country")),
        "region": _text(bean.get("region")),
        "producer": _text(bean.get("producer")),
        "variety": _json_list(bean.get("variety")),
        "altitude": _json_list(bean.get("altitude")),
        "decaf_process": _text(bean.get("decafProcess")),
        "created_at": _iso_or_none(bean.get("createdAt")),
        "updated_at": _iso_or_none(bean.get("updatedAt")),
        "raw_json": json.dumps(bean, ensure_ascii=False, separators=(",", ":")),
        "synced_at": synced_at,
    }


def batch_row_from_decaid(batch: dict[str, Any], synced_at: str) -> dict[str, Any]:
    """Batch -> a row for ``bean_batches``.

    Four dates, and they mean different things: ``roastDate`` is when it was
    roasted, ``buyDate`` when it was bought, ``openDate`` when the bag was
    opened, ``bestBeforeDate`` what the label claims. Only the first drives the
    bean-age guard; the rest are carried because they are the context a person
    reads them in.

    ``openDate`` is the one that was missing. Decaid sets it where the DE1 app
    used to write a purchase date, so ``buy_date`` stayed null on batches that
    plainly had a date - which is how this surfaced.

    ``unfreezeDate`` is a real field in the API (it was once noted here as not
    existing, wrongly - it is simply absent from the response while unset). The
    extras fallback stays for archives written before that was understood.
    """
    extras = batch.get("extras") or {}
    return {
        "id": batch.get("id"),
        "bean_id": _text(batch.get("beanId")),
        "roast_date": _date(batch.get("roastDate")),
        "buy_date": _date(batch.get("buyDate")),
        "open_date": _date(batch.get("openDate")),
        "best_before_date": _date(batch.get("bestBeforeDate")),
        "freeze_date": _date(batch.get("freezeDate")),
        "unfreeze_date": _date(batch.get("unfreezeDate") or extras.get("unfreezeDate")),
        "frozen": _flag(batch.get("frozen")),
        "archived": _flag(batch.get("archived")),
        "weight_g": _number(batch.get("weight")),
        "weight_remaining_g": _number(batch.get("weightRemaining")),
        "roast_level": _text(batch.get("roastLevel")),
        # "Harvest date or season" per the API, and the real value is "2026".
        # Kept as text: parsing it would either fail or invent a January first.
        "harvest_date": _text(batch.get("harvestDate")),
        "quality_score": _number(batch.get("qualityScore")),
        "price": _number(batch.get("price")),
        "currency": _text(batch.get("currency")),
        "notes": _text(batch.get("notes")),
        # Beanie's freeze/thaw history (T57); anything but a list is none.
        "storage_events": (json.dumps(extras["storageEvents"], separators=(",", ":"))
                           if isinstance(extras.get("storageEvents"), list) else None),
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


def _json_list(value: Any) -> str | None:
    """A list field (``variety``, ``altitude``) as JSON text, or None.

    An empty list is None rather than ``"[]"``: "no varieties recorded" and
    "recorded as none" are the same thing here, and null is how the rest of
    the archive says it.
    """
    if not isinstance(value, list) or not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
