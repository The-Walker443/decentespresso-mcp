"""Shared test data for the Decaid era (SPEC §4).

A module of its own rather than conftest: the tests import the helpers by
name, and pytest loads conftest separately - nothing can be imported from
there.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta
from typing import Any

from decentespresso_mcp.decaid_mapping import (
    batch_row_from_decaid,
    bean_row_from_decaid,
    series_rows_from_decaid,
    shot_row_from_decaid,
)
from decentespresso_mcp.decaid_profile import profile_version

#: Distinguishes "not given" from "explicitly None".
_KEEP = object()


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"

#: The shot the whole chain is checked against - an anonymised real response
#: from Decaid 0.8.5 with 184 data points.
DETAIL_FILE = "shot_detail.json"


def decaid_detail(
    shot_id: str | None = None,
    *,
    timestamp: str | None = None,
    updated_at: str | None = None,
    enjoyment: Any = _KEEP,
    notes: Any = _KEEP,
) -> dict[str, Any]:
    """One shot detail, optionally with altered header fields.

    A deep copy: callers modify it, and the fixture is shared by every test.
    """
    detail = json.loads((FIXTURES / DETAIL_FILE).read_text(encoding="utf-8"))
    if shot_id is not None:
        detail["id"] = shot_id
    if timestamp is not None:
        detail["timestamp"] = timestamp
        # As in the real data: for imported shots createdAt matches the
        # timestamp, for native ones it sits two hours earlier. Otherwise the
        # cross-check in time_source_of fires.
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
    """Puts a shot and its series into the archive and returns the identifier."""
    db.upsert_shot(
        shot_row_from_decaid(detail, synced_at),
        series_rows_from_decaid(detail),
    )
    return detail["id"]


def store_shot_with_profile(
    db, detail: dict[str, Any], synced_at: str = "2026-09-14T12:00:00Z"
) -> str:
    """Like ``store_shot``, but with a profile version and the link.

    The profile comes from the shot's workflow - the same route the sync
    takes.
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
    """Beans and batches from the fixtures, so bean_id becomes resolvable."""
    beans = json.loads((FIXTURES / "beans.json").read_text(encoding="utf-8"))
    batches = json.loads((FIXTURES / "bean_batches.json").read_text(encoding="utf-8"))
    db.upsert_beans([bean_row_from_decaid(b, synced_at) for b in beans])
    db.upsert_bean_batches([batch_row_from_decaid(b, synced_at) for b in batches])
    db.link_shots_to_beans()


#: A small but distinguishable archive: two beans, two profiles, one broken
#: scale. Derived from a real response so the numbers stay plausible.
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

    # Older than RECENT, so "newest shot" stays unambiguous.
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
    """Scale not tared: it starts well above zero and stays there.

    That is the most common data fault in practice, and the metrics must
    report it as a warning rather than invent a number.
    """
    for point in detail["measurements"]:
        scale = point.setdefault("scale", {})
        scale["weight"] = 120.0
        scale["weightFlow"] = 0.0
    return detail


def _two_hours_earlier(stamp: str) -> str:
    moment = datetime.fromisoformat(stamp) - timedelta(hours=2)
    return moment.isoformat(timespec="microseconds") + "Z"
