"""Gemeinsame Testdaten fuer die Decaid-Aera (SPEC ss20).

Eigenes Modul statt conftest: die Tests importieren die Helfer per Namen, und
conftest laedt pytest gesondert - von dort laesst sich nichts importieren.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta
from typing import Any

from visualizer_mcp.decaid_mapping import (
    batch_row_from_decaid,
    bean_row_from_decaid,
    series_rows_from_decaid,
    shot_row_from_decaid,
)
from visualizer_mcp.decaid_profile import profile_version

#: Unterscheidet "nicht angegeben" von "ausdruecklich None".
_KEEP = object()


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"

#: Der Bezug, gegen den die Kette geprueft wird - eine anonymisierte echte
#: Antwort von Decaid 0.8.5 mit 184 Messpunkten.
DETAIL_FILE = "shot_detail.json"


def decaid_detail(
    shot_id: str | None = None,
    *,
    timestamp: str | None = None,
    updated_at: str | None = None,
    enjoyment: Any = _KEEP,
    notes: Any = _KEEP,
) -> dict[str, Any]:
    """Ein Bezugsdetail, wahlweise mit abgewandelten Kopfdaten.

    Tiefe Kopie: die Aufrufer aendern daran herum, und die Fixture wird von
    allen Tests geteilt.
    """
    detail = json.loads((FIXTURES / DETAIL_FILE).read_text(encoding="utf-8"))
    if shot_id is not None:
        detail["id"] = shot_id
    if timestamp is not None:
        detail["timestamp"] = timestamp
        # Wie in den Echtdaten: bei importierten Bezuegen deckt sich createdAt
        # mit dem Zeitstempel, bei nativen liegt es zwei Stunden davor. Sonst
        # schlaegt die Gegenprobe in time_source_of an.
        detail["createdAt"] = (
            f"{timestamp}Z" if str(detail["id"]).startswith("de1app-")
            else _two_hours_earlier(timestamp)
        )
    if updated_at is not None:
        detail["updatedAt"] = updated_at
    if enjoyment is not _KEEP:
        detail.setdefault("annotations", {})["enjoyment"] = enjoyment
    if notes is not _KEEP:
        detail.setdefault("annotations", {})["espressoNotes"] = notes
    return detail


def store_shot(db, detail: dict[str, Any], synced_at: str = "2026-09-14T12:00:00Z") -> str:
    """Legt einen Bezug samt Messreihe ins Archiv und gibt die Kennung zurueck."""
    db.upsert_shot(
        shot_row_from_decaid(detail, synced_at),
        series_rows_from_decaid(detail),
    )
    return detail["id"]


def store_shot_with_profile(
    db, detail: dict[str, Any], synced_at: str = "2026-09-14T12:00:00Z"
) -> str:
    """Wie ``store_shot``, aber mit Profilversion und Verknuepfung.

    Das Profil kommt aus dem Workflow des Bezugs - denselben Weg geht der
    Abgleich auch.
    """
    shot_id = store_shot(db, detail, synced_at)
    profile = ((detail.get("workflow") or {}).get("profile")) or {}
    if profile.get("steps"):
        profile_id, _ = db.upsert_profile(
            seen_at=synced_at, source="decaid", **profile_version(profile)
        )
        db.link_shot_profile(shot_id, profile_id)
    return shot_id


def store_beans(db, synced_at: str = "2026-09-14T12:00:00Z") -> None:
    """Bohnen und Chargen aus den Fixtures, damit bean_id aufloesbar wird."""
    beans = json.loads((FIXTURES / "beans.json").read_text(encoding="utf-8"))
    batches = json.loads((FIXTURES / "bean_batches.json").read_text(encoding="utf-8"))
    db.upsert_beans([bean_row_from_decaid(b, synced_at) for b in beans])
    db.upsert_bean_batches([batch_row_from_decaid(b, synced_at) for b in batches])
    db.link_shots_to_beans()


#: Ein kleiner, aber unterscheidbarer Bestand: zwei Bohnen, zwei Profile, eine
#: kaputte Waage. Aus einer echten Antwort abgewandelt, damit die Zahlen
#: plausibel bleiben.
REFERENCE_ID = "de1app-1785525360"
RECENT_ID = "aaaa1111-0000-4000-8000-000000000001"
BROKEN_ID = "aaaa1111-0000-4000-8000-000000000002"


def corpus() -> list[dict[str, Any]]:
    reference = decaid_detail(REFERENCE_ID, timestamp="2026-07-31T19:16:00",
                              updated_at="2026-09-01T10:00:00Z", enjoyment=80.0,
                              notes="klassisch, schmeckt")
    _set_bean(reference, "Tchibo", "Testsorte", "batch-tchibo")

    recent = decaid_detail(RECENT_ID, timestamp="2026-09-13T07:50:12",
                           updated_at="2026-09-13T08:00:00Z", enjoyment=60.0,
                           notes=None)
    _set_bean(recent, "Bogatz", "Espresso Brasil", "batch-bogatz")
    recent["workflow"]["profile"]["title"] = "Default"
    recent["workflow"]["profile"]["steps"][0]["temperature"] = 93.0

    # Aelter als RECENT, damit "neuester Bezug" eindeutig bleibt.
    broken = decaid_detail(BROKEN_ID, timestamp="2026-09-12T07:50:12",
                           updated_at="2026-09-12T08:00:00Z", enjoyment=None,
                           notes=None)
    _set_bean(broken, "Bogatz", "Espresso Brasil", "batch-bogatz")
    broken["workflow"]["profile"]["title"] = "Default"
    broken["workflow"]["profile"]["steps"][0]["temperature"] = 93.0
    break_the_scale(broken)
    return [reference, recent, broken]


def _set_bean(detail: dict[str, Any], roaster: str, name: str, batch: str) -> None:
    context = detail["workflow"]["context"]
    context["coffeeRoaster"] = roaster
    context["coffeeName"] = name
    context["beanBatchId"] = batch


def break_the_scale(detail: dict[str, Any]) -> dict[str, Any]:
    """Waage nicht tariert: sie startet weit ueber null und bleibt stehen.

    Das ist der haeufigste Datenfehler in der Praxis, und die Metriken muessen
    ihn als Warnung melden statt eine Zahl zu erfinden.
    """
    for point in detail["measurements"]:
        scale = point.setdefault("scale", {})
        scale["weight"] = 120.0
        scale["weightFlow"] = 0.0
    return detail


def _two_hours_earlier(stamp: str) -> str:
    moment = datetime.fromisoformat(stamp) - timedelta(hours=2)
    return moment.isoformat(timespec="microseconds") + "Z"
