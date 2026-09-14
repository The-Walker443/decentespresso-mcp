"""Decaid-Antworten -> Archivzeilen (SPEC ss20.4).

Zwei Normalisierungen sind hier bindend, beide aus der Migration in M8 (2/n)
hervorgegangen und an den echten 168 Bezuegen nachgemessen:

**Zeit.** Das Archiv fuehrt durchgehend UTC. Decaid tut das nicht einheitlich:
aus der de1app importierte Bezuege tragen ihren Zeitstempel bereits in UTC,
selbst aufgezeichnete in Ortszeit ohne Zeitzonenangabe. Gemessen ueber alle
Bezuege: 88 importierte mit ``timestamp == createdAt``, 80 native mit genau
zwei Stunden Versatz. Umgerechnet wird deshalb ueber ``Europe/Berlin`` mit
voller Sommerzeitbehandlung - ein fester Versatz waere Ende Oktober falsch.
Woher ein Zeitstempel kam, steht je Bezug in ``time_source``.

**Bewertung.** Decaid legt importierte Bezuege mit ``enjoyment: 0.0`` an. Das
ist kein Urteil, sondern "nicht bewertet": 75 der 88 importierten stehen so da,
waehrend echte Bewertungen bei 40 bis 100 liegen und **kein einziger** nativ
aufgezeichneter Bezug je 0.0 traegt. Eine 0 aus der Import-Aera wird deshalb als
``NULL`` archiviert. Ohne diese Regel kaemen 75 Scheinbewertungen ins Archiv,
und Waechter wie ``audit_archive`` wuerden sie fuer bare Muenze nehmen.
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

#: Ortszeit der Maschine. Als Zeitzone, nicht als Versatz - sonst laege jeder
#: Bezug nach dem letzten Oktobersonntag eine Stunde daneben. Scheitert die
#: Zeile beim Start, fehlt die Zeitzonendatenbank; dafuer haengt tzdata in den
#: Abhaengigkeiten.
MACHINE_TZ = ZoneInfo("Europe/Berlin")

#: Aus der de1app importierte Bezuege. Die Zahl ist der Startzeitpunkt als
#: Unixzeit; daran haengt auch die Zuordnung im Migrationsskript.
DE1APP_ID = re.compile(r"^de1app-(\d{9,13})$")

#: Werte von ``time_source`` in der Datenbank.
SOURCE_UTC = "utc"
SOURCE_LOCAL = "local_berlin"

#: Toleranz fuer die Gegenprobe ``timestamp`` gegen ``createdAt``.
_CROSSCHECK_TOLERANCE_S = 120

#: Zeitreihe: links die Archivspalte, rechts der Pfad in ``measurements``.
MACHINE_FIELDS = {
    "pressure": "pressure",
    "flow_in": "flow",                          # Pumpenfluss
    "temp_mix": "mixTemperature",
    "temp_basket": "groupTemperature",
    "target_pressure": "targetPressure",        # neu ab M8: Soll je Messpunkt
    "target_flow": "targetFlow",
    "target_temp_mix": "targetMixTemperature",
    "target_temp_basket": "targetGroupTemperature",
    "profile_frame": "profileFrame",
}

SCALE_FIELDS = {
    "weight": "weight",
    "flow_out": "weightFlow",                   # aus der Waage abgeleitet
}


def is_import_era(shot_id: str) -> bool:
    """Stammt der Bezug aus dem de1app-Import statt aus Decaids Aufzeichnung?"""
    return bool(DE1APP_ID.match(str(shot_id or "")))


def time_source_of(shot: dict[str, Any]) -> str:
    """Ob der Zeitstempel eines Bezugs UTC oder Ortszeit ist.

    Entschieden wird an der Kennung, weil die eine stabile Konvention ist.
    ``createdAt`` dient als Gegenprobe: weichen beide voneinander ab, hat Decaid
    sein Verhalten geaendert, und das soll auffallen statt still falsch zu
    laufen.
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
    """``(ISO8601 in UTC, time_source)`` fuer einen Bezug.

    Bei Ortszeit in der mehrdeutigen Stunde der Rueckstellung wird die erste
    Lesart genommen (``fold=0``, also noch Sommerzeit). Eine Entscheidung muss
    fallen; die fruehere ist die, die zum uebrigen Tagesverlauf passt.
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
    """Bewertung, mit der Null-Regel aus dem Modul-Docstring."""
    annotations = shot.get("annotations") or {}
    value = annotations.get("enjoyment")
    if value is None:
        return None
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if rating == 0 and is_import_era(shot.get("id", "")):
        # 75 von 88 importierten Bezuegen stehen so da - das ist der
        # Vorgabewert des Imports, keine Bewertung.
        return None
    return rating


def shot_row_from_decaid(detail: dict[str, Any], synced_at: str) -> dict[str, Any]:
    """Detail-Antwort -> Zeile fuer ``shots``."""
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
        # Der Bezug kennt nur die Charge; welche Bohne das ist, steht an der
        # Charge. bean_id traegt die Ingestion nach.
        "bean_batch_id": _text(context.get("beanBatchId")),
        "bean_id": None,                       # loest die Ingestion ueber die Charge auf
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
        # Ohne die Messreihe: die steht in shot_series, und eine Detailantwort
        # wiegt mit ihr rund 140 kB - ueber alle Bezuege waere das ein
        # Vielfaches des uebrigen Archivs, doppelt abgelegt.
        "raw_json": json.dumps(
            {k: v for k, v in detail.items() if k != "measurements"},
            ensure_ascii=False, separators=(",", ":"),
        ),
        "synced_at": synced_at,
    }


def series_rows_from_decaid(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """``measurements`` -> Zeilen fuer ``shot_series``.

    ``elapsed`` entsteht aus den Zeitstempeln der Messpunkte, weil Decaid kein
    ``time``-Feld liefert (T12). ``state``/``substate`` und ``profile_frame``
    kommen mit - aus ihnen leitet ``metrics`` das Ende der Praeinfusion ab.
    """
    measurements = detail.get("measurements") or []
    times = measurement_times(measurements)
    shot_id = detail.get("id")

    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for elapsed, point in zip(times, measurements, strict=True):
        key = round(elapsed, 3)
        if key in seen:
            # elapsed ist Teil des Primaerschluessels.
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
    """Bohne -> Zeile fuer ``beans``."""
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
    """Charge -> Zeile fuer ``bean_batches``.

    ``unfreezeDate`` fuehrt Decaid nicht als eigenes Feld; es steht, wenn
    ueberhaupt, in den Zusatzangaben. Die Gefrierzeit zaehlt beim Bohnenalter
    nicht mit, deshalb wird beides mitgenommen.
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


# ------------------------------------------------------------------ Hilfsmittel


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
    """``createdAt``/``updatedAt`` tragen ein Z und sind damit bereits UTC."""
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
    """Wahrheitswerte als 0/1 - SQLite kennt kein BOOLEAN."""
    if value is None:
        return None
    return 1 if value else 0


def _date(value: Any) -> str | None:
    """Datumsangaben bleiben Datum.

    Roest- und Gefrierdatum sind Tagesangaben. Sie in eine Zeitzone zu zwingen
    verschoebe sie um einen Tag, ohne dass die Uhrzeit je erfasst worden waere.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:10]
