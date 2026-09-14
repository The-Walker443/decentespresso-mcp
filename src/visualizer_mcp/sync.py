"""Abgleich mit Decaid: Bohnen, Chargen, Bezuege, Profile (SPEC ss20.4).

ABLAUF. Ein Lauf blaettert zuerst die komplette Bezugsliste durch. Die Liste
liefert alles ausser der Messreihe, insbesondere ``updatedAt`` - damit steht
ohne einen einzigen Detailabruf fest, welche Bezuege sich geaendert haben.
Geholt werden dann nur diese.

WARUM DIE GANZE LISTE. Decaid kennt keinen serverseitigen Zeitfilter (T8), und
die Liste ist nach Bezugszeit sortiert, nicht nach Aenderungszeit. Ein im Juni
gezogener Bezug, dessen Notiz heute ergaenzt wurde, steht also weiterhin hinten.
Ein Blaettern, das beim ersten alten Eintrag abbricht, wuerde ihn nie sehen.
Die vollstaendige Liste kostet bei 168 Bezuegen zwei Anfragen im LAN - dafuer
findet der Abgleich auch nachtraegliche Aenderungen.

Backfill und inkrementeller Lauf sind damit derselbe Algorithmus; ``full=True``
erzwingt lediglich, dass jeder Bezug neu geholt wird.

TABLET AUS. Das Tablet ist nicht immer an. Ist es nicht erreichbar, ist das
kein Fehlerzustand, sondern der Normalfall zwischen zwei Kaffees: der Lauf
endet mit ``waiting_for_tablet``, laesst den Bestand unberuehrt und traegt
nichts in die Fehlerliste ein.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .db import Database, utc_now_iso
from .decaid_client import (
    MAX_PAGE_SIZE,
    DecaidClient,
    DecaidError,
    DecaidUnreachable,
)
from .decaid_mapping import (
    batch_row_from_decaid,
    bean_row_from_decaid,
    series_rows_from_decaid,
    shot_row_from_decaid,
)
from .decaid_profile import profile_version
from .metrics import metrics_for_shot, warm_metrics_cache

log = logging.getLogger(__name__)

STATE_LAST_SYNC = "last_sync_at"
STATE_LAST_RESULT = "last_sync_result"
STATE_BACKFILL_DONE = "backfill_completed_at"
STATE_LAST_REACHABLE = "decaid_last_reachable_at"
STATE_DECAID_VERSION = "decaid_version"

#: Obergrenze fuer Detailabrufe pro Lauf. Ein Detail wiegt rund 140 kB; beim
#: ersten Backfill sind das bei 168 Bezuegen gut 23 MB, die sonst in einem Zug
#: ueber das WLAN des Tablets muessten. Der naechste Lauf macht weiter.
MAX_DETAILS_PER_RUN = 60


@dataclass(slots=True)
class SyncResult:
    new_shots: int = 0
    updated: int = 0
    unchanged: int = 0
    series_points: int = 0
    new_profile_versions: int = 0
    profiles_linked: int = 0
    metrics_computed: int = 0
    beans: int = 0
    bean_batches: int = 0
    duration_ms: int = 0
    mode: str = "incremental"
    #: Tablet nicht erreichbar. Kein Fehler - siehe Modul-Docstring.
    waiting_for_tablet: bool = False
    #: Noch offene Detailabrufe, wenn die Obergrenze gegriffen hat.
    pending: int = 0
    #: Blockierend: vorübergehende Probleme, bei denen ein spaeterer Versuch
    #: helfen kann.
    errors: list[str] = field(default_factory=list)
    #: Nicht blockierend: deterministische Befunde. Ein Retry aendert daran
    #: nichts.
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_database(db_path: str) -> Database:
    """Datenbank oeffnen, migrieren und Metrik-Cache waermen.

    Einziger Einstiegspunkt fuer Server wie CLI, damit beide denselben Zustand
    herstellen.
    """
    db = Database(db_path)
    db.migrate()
    warm_metrics_cache(db)
    return db


async def run_sync(
    client: DecaidClient,
    db: Database,
    *,
    full: bool = False,
) -> SyncResult:
    """Ein Abgleichlauf. ``full=True`` holt jeden Bezug neu."""
    started = time.monotonic()
    result = SyncResult(mode="backfill" if full else "incremental")

    try:
        await _sync_beans(client, db, result)
        listed = await _list_all_shots(client)
    except DecaidUnreachable:
        result.waiting_for_tablet = True
        result.duration_ms = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(_persist, db, result)
        log.info("sync postponed", extra={"fields": {"reason": "waiting_for_tablet"}})
        return result
    except DecaidError as exc:
        result.errors.append(f"{exc.code}: {exc}")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(_persist, db, result)
        log.error("sync aborted", extra={"fields": {"error": exc.code}})
        return result

    known = await asyncio.to_thread(db.known_shot_versions)
    stale = [
        item for item in listed
        if full or item["id"] not in known
        or _stamp(item.get("updatedAt")) != known[item["id"]]
    ]
    result.unchanged = len(listed) - len(stale)

    if len(stale) > MAX_DETAILS_PER_RUN:
        # Die aeltesten zuerst: so waechst das Archiv von hinten zusammen und
        # ein abgebrochener Backfill hinterlaesst keine Luecke in der Mitte.
        stale.sort(key=lambda i: str(i.get("timestamp") or ""))
        result.pending = len(stale) - MAX_DETAILS_PER_RUN
        stale = stale[:MAX_DETAILS_PER_RUN]

    log.info(
        "sync scan",
        extra={"fields": {"mode": result.mode, "listed": len(listed),
                          "to_fetch": len(stale), "pending": result.pending}},
    )

    for item in stale:
        if await _ingest_shot(client, db, item["id"], result) is False:
            break

    result.metrics_computed = await asyncio.to_thread(warm_metrics_cache, db)
    await asyncio.to_thread(db.link_shots_to_beans)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(_persist, db, result, complete=not result.pending)

    log.info(
        "sync done",
        extra={"fields": {
            "mode": result.mode, "new": result.new_shots, "updated": result.updated,
            "unchanged": result.unchanged, "points": result.series_points,
            "profiles": result.new_profile_versions, "beans": result.beans,
            "metrics": result.metrics_computed, "pending": result.pending,
            "errors": len(result.errors), "dur_ms": result.duration_ms,
        }},
    )
    return result


async def _ingest_shot(
    client: DecaidClient, db: Database, shot_id: str, result: SyncResult
) -> bool:
    """Einen Bezug holen und ablegen. ``False`` heisst: Tablet weg, Lauf beenden."""
    try:
        detail = await client.get_shot(shot_id)
    except DecaidUnreachable:
        # Mitten im Lauf verschwunden. Was schon da ist, bleibt; der Rest
        # kommt beim naechsten Mal.
        result.waiting_for_tablet = True
        return False
    except DecaidError as exc:
        # Ein kaputter Bezug bricht den Lauf nicht ab (SPEC ss6.5).
        result.errors.append(f"{shot_id}: {exc.code}: {exc}")
        log.warning("shot failed", extra={"fields": {"shot": shot_id, "error": exc.code}})
        return True

    try:
        synced_at = utc_now_iso()
        shot = shot_row_from_decaid(detail, synced_at)
        series = series_rows_from_decaid(detail)
        is_new = await asyncio.to_thread(db.upsert_shot, shot, series)
    except Exception as exc:  # noqa: BLE001 - Einzelfehler darf den Lauf nicht killen
        result.errors.append(f"{shot_id}: store_failed: {type(exc).__name__}: {exc}")
        log.warning("shot store failed", extra={"fields": {"shot": shot_id}})
        return True

    result.series_points += len(series)
    if is_new:
        result.new_shots += 1
    else:
        result.updated += 1

    await _link_profile(db, shot_id, detail, result)
    return True


async def _link_profile(
    db: Database, shot_id: str, detail: dict[str, Any], result: SyncResult
) -> None:
    """Profilversion aus dem eingebetteten Workflow ableiten und verknuepfen.

    Anders als in der Visualizer-Aera braucht es dafuer keinen zweiten Abruf und
    keinen TCL-Parser - das Profil liegt dem Bezug bei.
    """
    profile = ((detail.get("workflow") or {}).get("profile")) or {}
    if not profile.get("steps"):
        result.warnings.append(f"{shot_id}: kein Profil im Workflow")
        return

    version = profile_version(profile)
    profile_id, is_new = await asyncio.to_thread(
        lambda: db.upsert_profile(seen_at=utc_now_iso(), source="decaid", **version)
    )
    if is_new:
        result.new_profile_versions += 1
    await asyncio.to_thread(db.link_shot_profile, shot_id, profile_id)
    result.profiles_linked += 1


async def _sync_beans(client: DecaidClient, db: Database, result: SyncResult) -> None:
    """Bohnen und Chargen. Beide Listen sind klein und kommen unpaginiert."""
    synced_at = utc_now_iso()
    beans = [bean_row_from_decaid(b, synced_at) for b in await client.beans()]
    batches = [batch_row_from_decaid(b, synced_at) for b in await client.bean_batches()]
    if beans:
        result.beans = await asyncio.to_thread(db.upsert_beans, beans)
    if batches:
        result.bean_batches = await asyncio.to_thread(db.upsert_bean_batches, batches)


async def _list_all_shots(client: DecaidClient) -> list[dict[str, Any]]:
    """Die vollstaendige Bezugsliste, ueber alle Seiten. Ohne Messreihen."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await client.list_shots(limit=MAX_PAGE_SIZE, offset=offset)
        if not page.items:
            break
        items.extend(page.items)
        offset += len(page.items)
        if offset >= page.total:
            break
    return items


def _stamp(value: Any) -> str | None:
    """``updatedAt`` so normalisieren, wie es auch in der DB steht."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _persist(db: Database, result: SyncResult, *, complete: bool = False) -> None:
    db.set_state(STATE_LAST_SYNC, utc_now_iso())
    db.set_json_state(STATE_LAST_RESULT, result.as_dict())
    db.record_errors(result.errors + [f"warn: {w}" for w in result.warnings])
    if not result.waiting_for_tablet:
        db.set_state(STATE_LAST_REACHABLE, utc_now_iso())
    # Der Backfill gilt erst als fertig, wenn nichts mehr offen ist und nichts
    # schiefging - sonst begaenne der naechste Lauf inkrementell und liesse die
    # Luecke stehen.
    if complete and not result.errors and not result.waiting_for_tablet:
        if result.mode == "backfill" or not db.get_state(STATE_BACKFILL_DONE):
            db.set_state(STATE_BACKFILL_DONE, utc_now_iso())


async def refresh_shot(client: DecaidClient, db: Database, shot_id: str) -> dict:
    """Laedt einen einzelnen Bezug neu und schreibt ihn lokal fort (SPEC ss18.3).

    Derselbe Weg wie im Abgleich - Detail holen, upserten, Metriken neu rechnen.
    Der Upsert verwirft den Metrik-Cache des Bezugs ohnehin; der Warmlauf danach
    fuellt ihn wieder. Wichtig nach einer Dosisaenderung: die Ratio haengt daran.
    """
    detail = await client.get_shot(shot_id)
    synced_at = utc_now_iso()
    await asyncio.to_thread(
        db.upsert_shot,
        shot_row_from_decaid(detail, synced_at),
        series_rows_from_decaid(detail),
    )
    await asyncio.to_thread(metrics_for_shot, db, shot_id, refresh=True)
    return detail


#: SPEC ss9.2: get_shot("latest") prueft vorher auf Frische. Zwei Minuten sind
#: kurz genug, dass ein eben gezogener Bezug auftaucht, und lang genug, dass
#: eine Gespraechsfolge nicht bei jeder Frage synchronisiert.
QUICK_SYNC_MAX_AGE_S = 120


class SyncCoordinator:
    """Serialisiert Abgleichlaeufe und haelt den Client.

    Ein Schloss statt eines Schedulers: der Dienst hat einen Nutzer, und zwei
    gleichzeitige Laeufe wuerden sich nur gegenseitig die Detailabrufe
    wegnehmen.
    """

    def __init__(self, client: DecaidClient, db: Database) -> None:
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

    async def ensure_fresh(self, max_age_s: int = QUICK_SYNC_MAX_AGE_S) -> SyncResult | None:
        """Gleicht ab, wenn der letzte Lauf zu lange her ist. Sonst ``None``."""
        age = await asyncio.to_thread(self._age_seconds)
        if age is not None and age < max_age_s:
            return None
        return await self.run()

    def _age_seconds(self) -> float | None:
        stamp = self._db.get_state(STATE_LAST_SYNC)
        if not stamp:
            return None
        try:
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (datetime.now(UTC) - last).total_seconds()

    async def write_shot(
        self, shot_id: str, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Schreibt Annotationen und liefert ``(vorher, nachher)`` (SPEC ss18.3).

        Beide Staende kommen aus einem echten Abruf, nicht aus dem, was
        gesendet wurde - nur so faellt auf, wenn Decaid ein Feld verwirft.
        """
        async with self._lock:
            before = await self._client.get_shot(shot_id)
            await self._client.update_shot(shot_id, {"annotations": dict(fields)})
            after = await refresh_shot(self._client, self._db, shot_id)
        return (before.get("annotations") or {}), (after.get("annotations") or {})

    async def aclose(self) -> None:
        await self._client.aclose()


#: Rueckzug, wenn das Tablet aus ist. Es laeuft nur, waehrend Kaffee gemacht
#: wird; im Minutentakt dagegen anzurennen brachte nichts und fuellte das Log.
IDLE_BACKOFF_MULTIPLIER = 4


async def periodic_sync(
    coordinator: SyncCoordinator, interval_min: int, *, stop: asyncio.Event
) -> None:
    """Hintergrundschleife. Endet, sobald ``stop`` gesetzt wird.

    Ist das Tablet aus, wird das Intervall gestreckt statt eine Fehlerlawine zu
    erzeugen - das ist der erwartete Zustand zwischen zwei Kaffees.
    """
    if interval_min <= 0:
        return
    while not stop.is_set():
        delay = interval_min * 60
        try:
            result = await coordinator.run()
            if result.waiting_for_tablet:
                delay *= IDLE_BACKOFF_MULTIPLIER
            elif result.pending:
                # Backfill laeuft noch - zuegig weitermachen.
                delay = min(delay, 30)
        except Exception:  # noqa: BLE001 - die Schleife darf nie sterben
            log.exception("sync loop failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            continue


__all__ = [
    "IDLE_BACKOFF_MULTIPLIER",
    "MAX_DETAILS_PER_RUN",
    "QUICK_SYNC_MAX_AGE_S",
    "STATE_BACKFILL_DONE",
    "STATE_DECAID_VERSION",
    "STATE_LAST_REACHABLE",
    "STATE_LAST_RESULT",
    "STATE_LAST_SYNC",
    "SyncCoordinator",
    "SyncResult",
    "open_database",
    "periodic_sync",
    "refresh_shot",
    "run_sync",
]
