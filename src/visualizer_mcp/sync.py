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
from datetime import UTC, datetime
from typing import Any

from .db import Database, utc_now_iso
from .metrics import metrics_for_shot, warm_metrics_cache
from .tcl_profile import PARSER_VERSION, parse_profile, semantic_hash, version_hash
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
    metrics_computed: int = 0
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


def open_database(db_path: str) -> Database:
    """Datenbank oeffnen, migrieren und Nachzuegler-Felder befuellen.

    Einziger Einstiegspunkt fuer Server wie CLI, damit beide denselben Zustand
    herstellen.
    """
    db = Database(db_path)
    db.migrate()
    reparse_profiles(db)
    warm_metrics_cache(db)
    return db


def reparse_profiles(db: Database) -> int:
    """Parst Profile neu, deren ``parsed_json`` von einem aelteren Parser stammt.

    Ausgangspunkt ist das gespeicherte ``raw_tcl``, nicht das alte
    ``parsed_json`` - ein neuer Parser liefert per Definition andere Felder, die
    im alten JSON gar nicht stehen. ``version_hash`` bleibt unberuehrt: die
    Identitaet einer Version haengt an der Datei, nicht an unserer Deutung.

    Idempotent; laeuft bei jedem Start und tut nichts, wenn alles aktuell ist.
    """
    changed = 0
    for row in db.all_profiles_for_reparse():
        try:
            previous = json.loads(row["parsed_json"])
        except json.JSONDecodeError:
            previous = {}
        stale = (
            previous.get("parser_version") != PARSER_VERSION
            or (row["semantic_hash"] is None and previous.get("parse_ok"))
        )
        if not stale:
            continue

        parsed = parse_profile(row["raw_tcl"])
        db.update_profile_parse(
            row["id"],
            name=parsed.get("title") or "(ohne Titel)",
            parsed_json=json.dumps(parsed, ensure_ascii=False),
            # parse_ok=false liefert None - ohne Parse gibt es keine
            # semantische Sicht, NULL ist die richtige Aussage.
            semantic_hash=semantic_hash(parsed),
            profile_notes=parsed.get("notes"),
        )
        changed += 1

    if changed:
        log.info(
            "profiles reparsed",
            extra={"fields": {"profiles": changed, "parser_version": PARSER_VERSION}},
        )
    return changed


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
    result.metrics_computed = await asyncio.to_thread(warm_metrics_cache, db)

    newest = max((int(r.get("updated_at") or 0) for r in rows), default=None)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(_persist, db, result, newest)

    log.info(
        "sync done",
        extra={"fields": {
            "mode": mode, "new": result.new_shots, "updated": result.updated,
            "unchanged": result.unchanged, "points": result.series_points,
            "profiles": result.new_profile_versions, "linked": result.profiles_linked,
            "metrics": result.metrics_computed,
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
                semantic_hash=semantic_hash(parsed),
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


async def refresh_shot(client: VisualizerClient, db: Database, shot_id: str) -> dict:
    """Laedt einen einzelnen Shot neu und schreibt ihn lokal fort (SPEC ss18.3).

    Derselbe Weg wie im Sync-Lauf - Detail holen, upserten, Metriken neu
    rechnen. Der Upsert verwirft den Metrik-Cache des Shots ohnehin; der
    Warmlauf danach fuellt ihn wieder, damit die naechste Frage nicht auf die
    Neuberechnung wartet. Wichtig nach einer Dosisaenderung: die Ratio haengt
    daran.
    """
    detail = await client.get_shot(shot_id)
    if detail is None:  # pragma: no cover - nur bei ETag, das hier keiner setzt
        raise ShotNotFound(f"Kein Detail fuer {shot_id}")

    synced_at = utc_now_iso()
    await asyncio.to_thread(
        db.upsert_shot,
        shot_row_from_detail(detail, synced_at),
        series_rows_from_detail(detail),
    )
    await asyncio.to_thread(metrics_for_shot, db, shot_id, refresh=True)
    return detail


#: SPEC ss9.2: get_shot("latest") prueft vorher auf Frische. Zwei Minuten sind
#: kurz genug, dass ein eben gezogener Shot auftaucht, und lang genug, dass eine
#: Folge von Fragen nicht jedes Mal Visualizer anfasst.
QUICK_SYNC_MAX_AGE_S = 120


class SyncCoordinator:
    """Buendelt alle Sync-Ausloeser hinter einem Lock.

    Hintergrundschleife, ``sync_now`` und der Frische-Check von
    ``get_shot("latest")`` teilen sich denselben Client. Ohne das Lock koennten
    zwei Laeufe denselben Shot gleichzeitig schreiben und den Cursor
    widerspruechlich setzen.
    """

    def __init__(self, client: VisualizerClient, db: Database) -> None:
        self._client = client
        self._db = db
        self._lock = asyncio.Lock()

    async def run(self, *, full: bool | None = None) -> SyncResult:
        async with self._lock:
            if full is None:
                full = await asyncio.to_thread(
                    lambda: not self._db.get_state(STATE_BACKFILL_DONE)
                )
            return await run_sync(self._client, self._db, full=full)

    async def write_shot(
        self, shot_id: str, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Write-through: erst Visualizer, dann lokal nachziehen (SPEC ss18.3).

        Gibt ``(vorher, nachher)`` zurueck - beides vollstaendige Details, beide
        frisch von der API. Der Vorher-Stand kommt aus einem eigenen Abruf und
        nicht aus der lokalen Kopie: die koennte veraltet sein, und dann waere
        das gemeldete "vorher" eine Behauptung statt einer Messung.

        Unter demselben Lock wie der Sync - sonst koennte die
        Hintergrundschleife zwischen Schreiben und Nachlesen denselben Shot
        anfassen.
        """
        async with self._lock:
            before = await self._client.get_shot(shot_id)
            if before is None:  # pragma: no cover - nur mit ETag moeglich
                raise ShotNotFound(f"Kein Detail fuer {shot_id}")

            await self._client.update_shot(shot_id, fields)
            after = await refresh_shot(self._client, self._db, shot_id)
            return before, after

    async def ensure_fresh(self, max_age_s: int = QUICK_SYNC_MAX_AGE_S) -> SyncResult | None:
        """Synchronisiert nur, wenn der letzte Lauf zu lange her ist.

        ``None`` heisst: der Bestand galt bereits als frisch, es gab keinen
        Netzzugriff.
        """
        age = await asyncio.to_thread(self._age_seconds)
        if age is not None and age < max_age_s:
            return None
        return await self.run()

    def _age_seconds(self) -> float | None:
        stamp = self._db.get_state(STATE_LAST_SYNC)
        if not stamp:
            return None
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (datetime.now(UTC) - parsed).total_seconds()

    async def aclose(self) -> None:
        await self._client.aclose()


async def periodic_sync(
    coordinator: SyncCoordinator,
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
        try:
            await coordinator.run()
        except Exception:  # noqa: BLE001 - die Schleife muss ueberleben
            log.exception("sync loop iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_min * 60)
        except TimeoutError:
            continue
