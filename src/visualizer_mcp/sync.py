"""Sync-Worker: Backfill und inkrementeller Abgleich (SPEC ss6).

M1 synchronisiert Metadaten und Zeitreihen. Profile (``profiles``, ``profile_id``)
folgen in M2 - der Client kann sie bereits laden, der Worker nutzt das noch nicht.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import Database, utc_now_iso
from .visualizer_client import (
    VisualizerClient,
    VisualizerError,
    series_rows_from_detail,
    shot_row_from_detail,
)

log = logging.getLogger(__name__)

STATE_LAST_SYNC = "last_sync_at"
STATE_LAST_RESULT = "last_sync_result"
STATE_CURSOR = "updated_after_cursor"
STATE_BACKFILL_DONE = "backfill_completed_at"

#: Ueberlappung des Cursors, damit ein Shot nicht zwischen zwei Laeufen
#: durchrutscht, wenn Visualizer und wir uns um Sekunden uneinig sind.
CURSOR_OVERLAP_S = 120


@dataclass(slots=True)
class SyncResult:
    new_shots: int = 0
    updated: int = 0
    unchanged: int = 0
    series_points: int = 0
    duration_ms: int = 0
    mode: str = "incremental"
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_sync(
    client: VisualizerClient,
    db: Database,
    *,
    full: bool = False,
) -> SyncResult:
    """Ein Sync-Lauf.

    ``full=True`` laeuft ueber alle Seiten (Backfill, SPEC ss6.1). Sonst fragt der
    Lauf nur Shots ab, die seit dem letzten Cursor geaendert wurden - das findet
    im Gegensatz zur Seitenheuristik aus SPEC ss6.2 auch nachtraegliche
    Aenderungen an Notizen oder Bewertungen.
    """
    started = time.monotonic()
    mode = "backfill" if full else "incremental"
    result = SyncResult(mode=mode)

    cursor = None if full else await asyncio.to_thread(_read_cursor, db)
    known = await asyncio.to_thread(db.known_shot_versions)

    try:
        rows = await _collect_rows(client, cursor=cursor)
    except VisualizerError as exc:
        result.errors.append(f"{exc.code}: {exc}")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(_persist, db, result, None)
        log.error("sync aborted", extra={"fields": {"mode": mode, "error": exc.code}})
        return result

    stale = [
        row for row in rows
        if row["id"] not in known or known[row["id"]] != int(row.get("updated_at") or 0)
    ]
    result.unchanged = len(rows) - len(stale)

    log.info(
        "sync scan",
        extra={"fields": {"mode": mode, "listed": len(rows), "to_fetch": len(stale)}},
    )

    for row in stale:
        shot_id = row["id"]
        try:
            detail = await client.get_shot(shot_id)
        except VisualizerError as exc:
            # SPEC ss6.5: ein kaputter Shot bricht den Lauf nicht ab.
            result.errors.append(f"{shot_id}: {exc.code}: {exc}")
            log.warning("shot failed", extra={"fields": {"shot": shot_id, "error": exc.code}})
            continue
        if detail is None:
            result.unchanged += 1
            continue

        try:
            synced_at = utc_now_iso()
            shot = shot_row_from_detail(detail, synced_at)
            series = series_rows_from_detail(detail)
            is_new = await asyncio.to_thread(db.upsert_shot, shot, series)
        except Exception as exc:  # noqa: BLE001 - Einzelfehler darf den Lauf nicht killen
            result.errors.append(f"{shot_id}: store_failed: {type(exc).__name__}: {exc}")
            log.warning("shot store failed", extra={"fields": {"shot": shot_id}})
            continue

        result.series_points += len(series)
        if is_new:
            result.new_shots += 1
        else:
            result.updated += 1

    newest = max((int(r.get("updated_at") or 0) for r in rows), default=None)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(_persist, db, result, newest)

    log.info(
        "sync done",
        extra={"fields": {
            "mode": mode, "new": result.new_shots, "updated": result.updated,
            "unchanged": result.unchanged, "points": result.series_points,
            "errors": len(result.errors), "dur_ms": result.duration_ms,
        }},
    )
    return result


async def _collect_rows(
    client: VisualizerClient, *, cursor: int | None
) -> list[dict[str, Any]]:
    if cursor is None:
        return await client.iter_all_shot_rows()
    page = await client.list_shots(page=1, items=100, updated_after=cursor)
    rows = list(page.rows)
    for page_no in range(2, page.pages + 1):
        rows.extend(
            (await client.list_shots(page=page_no, items=100, updated_after=cursor)).rows
        )
    return rows


def _read_cursor(db: Database) -> int | None:
    """Cursor aus dem Zustand, sonst aus der DB abgeleitet, sonst None (= Backfill)."""
    stored = db.get_state(STATE_CURSOR)
    if stored is not None:
        try:
            return int(stored)
        except ValueError:
            log.warning("cursor unlesbar, faelle auf Backfill zurueck")
            return None
    if not db.get_state(STATE_BACKFILL_DONE):
        return None
    newest = db.max_updated_at()
    return max(0, newest - CURSOR_OVERLAP_S) if newest else None


def _persist(db: Database, result: SyncResult, newest_updated_at: int | None) -> None:
    db.set_state(STATE_LAST_SYNC, utc_now_iso())
    db.set_json_state(STATE_LAST_RESULT, result.as_dict())
    db.record_errors(result.errors)
    if result.errors:
        # Cursor nur weiterschieben, wenn wirklich alles durchlief - sonst
        # bliebe ein fehlgeschlagener Shot fuer immer ungeholt.
        return
    if newest_updated_at:
        db.set_state(STATE_CURSOR, str(max(0, newest_updated_at - CURSOR_OVERLAP_S)))
    if result.mode == "backfill":
        db.set_state(STATE_BACKFILL_DONE, utc_now_iso())


async def periodic_sync(
    client: VisualizerClient,
    db: Database,
    interval_min: int,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Hintergrundschleife (SPEC ss3: asyncio-Loop statt apscheduler).

    Der erste Lauf startet sofort; er ist ein Backfill, solange noch keiner
    erfolgreich war.
    """
    stop = stop or asyncio.Event()
    while not stop.is_set():
        full = await asyncio.to_thread(lambda: not db.get_state(STATE_BACKFILL_DONE))
        try:
            await run_sync(client, db, full=full)
        except Exception:  # noqa: BLE001 - die Schleife muss ueberleben
            log.exception("sync loop iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_min * 60)
        except TimeoutError:
            continue
