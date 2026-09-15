"""Sync with Decaid: beans, batches, shots, profiles (SPEC §5).

HOW A RUN WORKS. It first pages through the complete shot list. That list
carries everything except the measurements, ``updatedAt`` in particular - so
without a single detail request it is known which shots have changed. Only
those are then fetched.

WHY THE WHOLE LIST. Decaid has no server-side time filter (T8), and the list
is sorted by shot time, not by modification time. A shot pulled in June whose
note was added today therefore still sits at the back. Paging that stops at
the first old entry would never see it. The full list costs two requests on
the local network for 168 shots - and in exchange the sync also finds changes
made after the fact.

Backfill and incremental run are thus the same algorithm; ``full=True`` merely
forces every shot to be fetched again.

TABLET OFF. The tablet is not always on. If it cannot be reached that is not
an error state but the normal case between two coffees: the run ends with
``waiting_for_tablet``, leaves the archive untouched and writes nothing to the
error list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .db import Database, utc_now_iso
from .decaid_client import (
    MAX_PAGE_SIZE,
    DecaidClient,
    DecaidError,
    DecaidUnreachable,
    ShotNotFound,
)
from .decaid_mapping import (
    batch_row_from_decaid,
    bean_row_from_decaid,
    series_rows_from_decaid,
    shot_row_from_decaid,
)
from .decaid_profile import profile_version
from .guards import run_rules
from .metrics import metrics_for_shot, warm_metrics_cache
from .notify import send as notify

log = logging.getLogger(__name__)

STATE_LAST_SYNC = "last_sync_at"
STATE_LAST_RESULT = "last_sync_result"
STATE_BACKFILL_DONE = "backfill_completed_at"
STATE_LAST_REACHABLE = "decaid_last_reachable_at"
STATE_DECAID_VERSION = "decaid_version"

#: Cap on detail requests per run. A detail weighs about 140 kB; for the first
#: backfill of 168 shots that is a good 23 MB which would otherwise have to go
#: over the tablet's Wi-Fi in one go. The next run carries on.
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
    #: Guard findings (SPEC §10) and the messages sent for them.
    findings: int = 0
    notified: int = 0
    duration_ms: int = 0
    mode: str = "incremental"
    #: Tablet not reachable. Not an error - see the module docstring.
    waiting_for_tablet: bool = False
    #: Detail requests still outstanding when the cap kicked in.
    pending: int = 0
    #: Blocking: transient problems where a later attempt can help.
    errors: list[str] = field(default_factory=list)
    #: Non-blocking: deterministic findings. A retry changes nothing about
    #: them.
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_database(db_path: str) -> Database:
    """Open the database, migrate it and warm the metrics cache.

    The single entry point for both server and CLI, so both establish the same
    state.
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
    config: Config | None = None,
) -> SyncResult:
    """One sync run. ``full=True`` fetches every shot again.

    ``config`` enables the guards; without it only the sync runs. That keeps
    the ingestion tests free of guard logic and vice versa.
    """
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
        # Oldest first: that way the archive fills in from the back and an
        # aborted backfill leaves no gap in the middle.
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
    if config is not None:
        await _run_guards(db, config, result)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(_persist, db, result, complete=not result.pending)

    log.info(
        "sync done",
        extra={"fields": {
            "mode": result.mode, "new": result.new_shots, "updated": result.updated,
            "unchanged": result.unchanged, "points": result.series_points,
            "profiles": result.new_profile_versions, "beans": result.beans,
            "metrics": result.metrics_computed, "pending": result.pending,
            "findings": result.findings, "notified": result.notified,
            "errors": len(result.errors), "dur_ms": result.duration_ms,
        }},
    )
    return result


async def _run_guards(db: Database, config: Config, result: SyncResult) -> None:
    """Run the guards and report what is new (SPEC §10).

    A failure here must not topple the sync: the shots are already archived by
    then, and an unreported finding is not data loss.
    """
    if not config.guard_rules:
        return
    try:
        shots = await asyncio.to_thread(db.shots_for_guards)
        batches = await asyncio.to_thread(db.batches_by_id)
        findings = run_rules(
            shots, batches, at=datetime.now(UTC),
            enabled=config.guard_rules,
            warn_days=config.bean_age_warn_days,
            grace_hours=config.rating_grace_hours,
            tolerance_g=config.dose_tolerance_g,
        )
        result.findings = len(findings)
        result.notified = await notify(config, findings, db)
    except Exception as exc:  # noqa: BLE001 - guards do not topple the run
        result.warnings.append(f"guards_failed: {type(exc).__name__}: {exc}")
        log.warning("guards failed", extra={"fields": {"error": type(exc).__name__}})


async def _ingest_shot(
    client: DecaidClient, db: Database, shot_id: str, result: SyncResult
) -> bool:
    """Fetch and store one shot. ``False`` means: tablet gone, end the run."""
    try:
        detail = await client.get_shot(shot_id)
    except DecaidUnreachable:
        # Vanished mid-run. What is already there stays; the rest follows next
        # time.
        result.waiting_for_tablet = True
        return False
    except DecaidError as exc:
        # One broken shot does not abort the run (SPEC §6).
        result.errors.append(f"{shot_id}: {exc.code}: {exc}")
        log.warning("shot failed", extra={"fields": {"shot": shot_id, "error": exc.code}})
        return True

    try:
        synced_at = utc_now_iso()
        shot = shot_row_from_decaid(detail, synced_at)
        series = series_rows_from_decaid(detail)
        is_new = await asyncio.to_thread(db.upsert_shot, shot, series)
    except Exception as exc:  # noqa: BLE001 - a single failure must not kill the run
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
    """Derive the profile version from the embedded workflow and link it.

    No second request and no foreign format to parse - the profile ships with
    the shot.
    """
    profile = ((detail.get("workflow") or {}).get("profile")) or {}
    if not profile.get("steps"):
        result.warnings.append(f"{shot_id}: no profile in the workflow")
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
    """Beans and batches. Both lists are small and come unpaginated.

    Decaid's version is noted along the way: ``status()`` works on the database
    only and cannot ask for itself.
    """
    info = await client.info()
    await asyncio.to_thread(
        db.set_state, STATE_DECAID_VERSION, str(info.get("version") or "")
    )
    synced_at = utc_now_iso()
    beans = [bean_row_from_decaid(b, synced_at) for b in await client.beans()]
    batches = [batch_row_from_decaid(b, synced_at) for b in await client.bean_batches()]
    if beans:
        result.beans = await asyncio.to_thread(db.upsert_beans, beans)
    if batches:
        result.bean_batches = await asyncio.to_thread(db.upsert_bean_batches, batches)


async def _list_all_shots(client: DecaidClient) -> list[dict[str, Any]]:
    """The complete shot list, across all pages. Without measurements."""
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
    """Normalise ``updatedAt`` the same way it is stored in the database."""
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
    # The backfill only counts as done once nothing is outstanding and nothing
    # went wrong - otherwise the next run would start incrementally and leave
    # the gap standing.
    if complete and not result.errors and not result.waiting_for_tablet:
        if result.mode == "backfill" or not db.get_state(STATE_BACKFILL_DONE):
            db.set_state(STATE_BACKFILL_DONE, utc_now_iso())


async def refresh_shot(client: DecaidClient, db: Database, shot_id: str) -> dict:
    """Reload one shot and carry it forward locally (SPEC §11.1).

    The same path as in a sync run - fetch the detail, upsert, recompute the
    metrics. The upsert discards the shot's metrics cache anyway; the warm-up
    afterwards fills it again. Important after a dose change: the ratio depends
    on it.
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


#: SPEC §9: get_shot("latest") checks freshness first. Two minutes is short
#: enough for a just-pulled shot to appear, and long enough that a run of
#: questions does not sync on every one of them.
QUICK_SYNC_MAX_AGE_S = 120


class SyncCoordinator:
    """Serialises sync runs and holds the client.

    A lock rather than a scheduler: the service has one user, and two
    concurrent runs would only take detail requests away from each other.
    """

    def __init__(self, client: DecaidClient, db: Database,
                 config: Config | None = None) -> None:
        self._client = client
        self._db = db
        self._config = config
        self._lock = asyncio.Lock()

    async def run(self, *, full: bool | None = None) -> SyncResult:
        async with self._lock:
            if full is None:
                full = await asyncio.to_thread(
                    lambda: not self._db.get_state(STATE_BACKFILL_DONE)
                )
            return await run_sync(self._client, self._db, full=full,
                                  config=self._config)

    async def ensure_fresh(self, max_age_s: int = QUICK_SYNC_MAX_AGE_S) -> SyncResult | None:
        """Syncs if the last run is too long ago. Otherwise ``None``."""
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
        """Writes annotations and returns ``(before, after)`` (SPEC §11.1).

        Both states come from a real request rather than from what was sent -
        that is the only way a field discarded by Decaid becomes visible.
        """
        async with self._lock:
            before = await self._client.get_shot(shot_id)
            await self._client.update_shot(shot_id, {"annotations": dict(fields)})
            after = await refresh_shot(self._client, self._db, shot_id)
        return (before.get("annotations") or {}), (after.get("annotations") or {})

    async def write_bean(
        self, bean_id: str, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Change a bean, with read-back. The archive is updated afterwards."""
        async with self._lock:
            before = await self._find(self._client.beans(), bean_id)
            await self._client.update_bean(bean_id, dict(fields))
            after = await self._find(self._client.beans(), bean_id)
            await asyncio.to_thread(
                self._db.upsert_beans, [bean_row_from_decaid(after, utc_now_iso())]
            )
        return before, after

    async def write_batch(
        self, batch_id: str, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Change a batch, with read-back."""
        async with self._lock:
            before = await self._find(self._client.bean_batches(), batch_id)
            await self._client.update_bean_batch(batch_id, dict(fields))
            after = await self._find(self._client.bean_batches(), batch_id)
            await asyncio.to_thread(
                self._db.upsert_bean_batches,
                [batch_row_from_decaid(after, utc_now_iso())],
            )
            await asyncio.to_thread(self._db.link_shots_to_beans)
        return before, after

    async def write_workflow(
        self, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Change the workflow context, with read-back.

        The workflow is the setting for the *next* shot; it only reaches the
        archive once a shot has actually been pulled with it. So there is
        nothing to update locally here.

        A batch change carries the coffee labels with it - see
        ``_coffee_labels`` for why that is our job rather than Decaid's.
        """
        patch = dict(fields)
        if patch.get("beanBatchId"):
            patch.update(await self._coffee_labels(str(patch["beanBatchId"])))

        async with self._lock:
            before = (await self._client.workflow()).get("context") or {}
            await self._client.update_workflow({"context": patch})
            after = (await self._client.workflow()).get("context") or {}
        return before, after

    async def _coffee_labels(self, batch_id: str) -> dict[str, Any]:
        """``coffeeName``/``coffeeRoaster`` for a batch, resolved through its bean.

        The workflow context keeps the managed reference (``beanBatchId``) and
        the two display strings side by side, and Decaid derives neither from
        the other (T28). Setting only the batch therefore leaves the machine
        showing the previous coffee's name - measured on the live instance, and
        the reason this method exists. Decaid's own API examples write the id
        and the labels together, which is the behaviour reproduced here.

        An unresolvable batch aborts the write. Decaid accepts any string as a
        ``beanBatchId`` without checking it (T29), so refusing here is the only
        thing standing between a typo and a workflow pointing at nothing.
        """
        try:
            batch = await self._find(self._client.bean_batches(), batch_id)
        except ShotNotFound:
            raise ShotNotFound(
                f"Decaid has no batch {batch_id!r}. Nothing was written - the "
                "workflow still points at the batch it did before."
            ) from None
        bean_id = batch.get("beanId")
        if not bean_id:
            raise ShotNotFound(f"The batch {batch_id!r} names no bean")
        bean = await self._find(self._client.beans(), str(bean_id))
        return {"coffeeName": bean.get("name"), "coffeeRoaster": bean.get("roaster")}

    async def read_workflow(self) -> dict[str, Any]:
        return await self._client.workflow()

    @staticmethod
    async def _find(pending: Any, wanted: str) -> dict[str, Any]:
        """Pick one entry out of a list response.

        Both catalogues are a handful of rows, so one request serves the lookup
        and the read-back alike. ``GET /api/v1/beans/<id>`` and
        ``/api/v1/bean-batches/<id>`` do exist (T16) - an earlier comment here
        claimed they did not, which was an assumption nobody had checked.
        """
        for item in await pending:
            if str(item.get("id")) == wanted:
                return item
        raise ShotNotFound(f"No entry with the identifier {wanted!r}")

    async def aclose(self) -> None:
        await self._client.aclose()


#: Back off when the tablet is off. It only runs while coffee is being made;
#: hammering at it every minute achieved nothing and filled the log.
IDLE_BACKOFF_MULTIPLIER = 4


async def periodic_sync(
    coordinator: SyncCoordinator, interval_min: int, *, stop: asyncio.Event
) -> None:
    """Background loop. Ends as soon as ``stop`` is set.

    When the tablet is off the interval is stretched rather than producing an
    avalanche of errors - that is the expected state between two coffees.
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
                # Backfill still running - keep going promptly.
                delay = min(delay, 30)
        except Exception:  # noqa: BLE001 - the loop must never die
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
