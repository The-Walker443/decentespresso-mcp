"""Sync-Worker: Backfill und inkrementeller Abgleich (SPEC ss6).

M1 synchronisiert Metadaten und Zeitreihen. Profile (``profiles``, ``profile_id``)
folgen in M2 - der Client kann sie bereits laden, der Worker nutzt das noch nicht.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import Database, utc_now_iso
from .tcl_profile import parse_profile, version_hash
from .visualizer_client import (
    ShotNotFound,
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
#: Shots, fuer die Visualizer kein Profil hat (422/404). Ohne diese Liste wuerde
#: jeder Lauf dieselbe aussichtslose Anfrage wiederholen.
STATE_NO_PROFILE = "shots_without_profile"

#: Ueberlappung des Cursors, damit ein Shot nicht zwischen zwei Laeufen
#: durchrutscht, wenn Visualizer und wir uns um Sekunden uneinig sind.
CURSOR_OVERLAP_S = 120


@dataclass(slots=True)
class SyncResult:
    new_shots: int = 0
    updated: int = 0
    unchanged: int = 0
    series_points: int = 0
    new_profile_versions: int = 0
    profiles_linked: int = 0
    duration_ms: int = 0
    mode: str = "incremental"
    #: Blockierend: vorübergehende Probleme, bei denen ein spaeterer Versuch
    #: helfen kann. Solange welche auftreten, bleibt der Cursor stehen.
    errors: list[str] = field(default_factory=list)
    #: Nicht blockierend: deterministische Befunde (kaputtes TCL, Shot ohne
    #: Profil). Ein Retry wuerde daran nichts aendern.
    warnings: list[str] = field(default_factory=list)

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

    await _sync_profiles(client, db, result)

    newest = max((int(r.get("updated_at") or 0) for r in rows), default=None)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(_persist, db, result, newest)

    log.info(
        "sync done",
        extra={"fields": {
            "mode": mode, "new": result.new_shots, "updated": result.updated,
            "unchanged": result.unchanged, "points": result.series_points,
            "profiles": result.new_profile_versions, "linked": result.profiles_linked,
            "errors": len(result.errors), "dur_ms": result.duration_ms,
        }},
    )
    return result


async def _sync_profiles(
    client: VisualizerClient, db: Database, result: SyncResult
) -> None:
    """Holt fuer jeden Shot ohne Profilverknuepfung das TCL (SPEC ss6.4).

    Eigener Durchgang statt im Shot-Loop: so bekommen auch Shots eine
    Profilversion, die schon vor M2 archiviert wurden - ohne ihr Detail erneut
    zu laden.
    """
    pending = await asyncio.to_thread(db.shot_ids_without_profile)
    skip = set(await asyncio.to_thread(db.get_json_state, STATE_NO_PROFILE, []) or [])
    todo = [shot_id for shot_id in pending if shot_id not in skip]
    if not todo:
        return

    log.info("profile scan", extra={"fields": {"pending": len(todo), "skipped": len(skip)}})

    for shot_id in todo:
        try:
            raw_tcl = await client.get_profile_tcl(shot_id)
        except ShotNotFound:
            # 404/422 = dieser Shot hat kein Profil. Einmal merken, nicht ewig retryen.
            skip.add(shot_id)
            await asyncio.to_thread(db.set_json_state, STATE_NO_PROFILE, sorted(skip))
            result.warnings.append(f"{shot_id}: kein Profil auf Visualizer hinterlegt")
            log.info("shot has no profile", extra={"fields": {"shot": shot_id}})
            continue
        except VisualizerError as exc:
            result.errors.append(f"{shot_id}: profile_{exc.code}: {exc}")
            continue

        if not raw_tcl.strip():
            skip.add(shot_id)
            await asyncio.to_thread(db.set_json_state, STATE_NO_PROFILE, sorted(skip))
            continue

        try:
            # Bewusst nicht in to_thread: der Tcl-Interpreter bleibt so im
            # Hauptthread, und das Parsen dauert nur Millisekunden.
            parsed = parse_profile(raw_tcl)
            digest = version_hash(raw_tcl)
            profile_id, is_new = await asyncio.to_thread(
                db.upsert_profile,
                name=parsed.get("title") or "(ohne Titel)",
                version_hash=digest,
                raw_tcl=raw_tcl,
                parsed_json=json.dumps(parsed, ensure_ascii=False),
                profile_notes=parsed.get("notes"),
                seen_at=utc_now_iso(),
            )
            await asyncio.to_thread(db.link_shot_profile, shot_id, profile_id)
        except Exception as exc:  # noqa: BLE001 - ein Profil darf den Lauf nicht killen
            result.errors.append(f"{shot_id}: profile_store_failed: {type(exc).__name__}: {exc}")
            log.warning("profile store failed", extra={"fields": {"shot": shot_id}})
            continue

        result.profiles_linked += 1
        if is_new:
            result.new_profile_versions += 1
            if not parsed.get("parse_ok"):
                # Warnung, kein Fehler: das Rohprofil liegt in der DB und ein
                # erneuter Versuch wuerde denselben Parse-Fehler erzeugen.
                result.warnings.append(
                    f"{shot_id}: profile_parse_ok=false ({parsed.get('parse_error')})"
                )


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
    db.record_errors(result.errors + [f"warn: {w}" for w in result.warnings])
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
