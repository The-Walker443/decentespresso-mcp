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

Backfill and incremental run are thus the same algorithm; ``full`` only
chooses the label. It once forced every shot to be fetched again, which made a
first backfill of more than one run's worth impossible to finish (SPEC §6).

TABLET OFF. The tablet is not always on. If it cannot be reached that is not
an error state but the normal case between two coffees: the run ends with
``waiting_for_tablet``, leaves the archive untouched and writes nothing to the
error list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .db import Database, utc_now_iso
from .decaid_client import (
    ENJOYMENT_0_10_FROM_BUILD,
    MAX_PAGE_SIZE,
    AlreadyExists,
    DecaidClient,
    DecaidError,
    DecaidUnreachable,
    Protected,
    ShotNotFound,
)
from .decaid_mapping import (
    SCALE_10,
    SCALE_100,
    annotation_fields,
    batch_row_from_decaid,
    bean_row_from_decaid,
    series_rows_from_decaid,
    shot_row_from_decaid,
)
from .decaid_profile import profile_version
from .guards import run_rules
from .metrics import metrics_for_shot, warm_metrics_cache
from .notify import send as notify
from .profile_forge import (
    MARKER,
    MARKER_KEY,
    OVERRIDES,
    TITLE_SEPARATOR,
    apply_overrides,
    brew_hash,
    changes_brewing,
    check_title,
    default_title,
    is_default,
    made_here,
    resolve_profile,
    running_profile,
    same_brew,
)
from .recipes import (
    DYE2_FAVOURITES,
    DYE2_NAMESPACE,
    DYE2_RECIPES,
    LEGACY_NOTE,
    Catalogue,
    context_patch,
    default_name,
    dye2_name,
    favourite_name,
    favourite_workflow,
    profile_kind,
    row_from_workflow,
)
from .upload_watch import POLL_SECONDS, WATCH_SECONDS, newest, outcome
from .writes import ValidationError

log = logging.getLogger(__name__)

#: Key under which a workflow write hands the profile upload's outcome to the
#: tool (T47). Popped there; never part of a diff.
MACHINE_UPLOAD = "_machine_upload"

UPLOAD_NOTES = {
    "failed_retrying": (
        "Decaid could not upload the profile to the DE1 and retries on its own "
        "after 3, 10 and 30 s. Do not start the shot until the tablet preview "
        "shows the new curve."
    ),
    "pending_connection": (
        "The DE1 is not connected. Decaid uploads the profile once it connects; "
        "until then the machine keeps the profile it had."
    ),
    "unconfirmed": (
        "The upload runs over Bluetooth for several seconds and nothing "
        "confirmed it yet. Do not start the shot until the tablet preview shows "
        "the new curve."
    ),
}

STATE_LAST_SYNC = "last_sync_at"
STATE_LAST_RESULT = "last_sync_result"
STATE_BACKFILL_DONE = "backfill_completed_at"
STATE_LAST_REACHABLE = "decaid_last_reachable_at"
STATE_DECAID_VERSION = "decaid_version"
#: T46: the tablet's rating scale, the Decaid version it was determined for,
#: and when the annotations were re-read after the switch to 0-10.
STATE_ENJOYMENT_SCALE = "enjoyment_scale"
STATE_ENJOYMENT_SCALE_FOR = "enjoyment_scale_version"
STATE_ENJOYMENT_REREAD = "enjoyment_reread_at"

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
    #: T46: the tablet's rating scale, and how many shots the one-off re-read
    #: of annotations after the switch to 0-10 changed.
    enjoyment_scale: str = SCALE_100
    annotations_reread: int | None = None
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
    """One sync run. ``full`` labels it a backfill; what is fetched is the same.

    ``config`` enables the guards; without it only the sync runs. That keeps
    the ingestion tests free of guard logic and vice versa.
    """
    started = time.monotonic()
    result = SyncResult(mode="backfill" if full else "incremental")

    try:
        result.enjoyment_scale = await _sync_beans(client, db, result)
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

    if (result.enjoyment_scale == SCALE_10
            and not await asyncio.to_thread(db.get_state, STATE_ENJOYMENT_REREAD)):
        result.annotations_reread = await asyncio.to_thread(
            _reread_annotations, db, listed)
        await asyncio.to_thread(db.set_state, STATE_ENJOYMENT_REREAD, utc_now_iso())

    known = await asyncio.to_thread(db.known_shot_versions)
    # A shot is fetched when it is new or has changed - in a backfill too.
    #
    # `full` used to force every listed shot through here, which made the first
    # backfill unable to finish: with more shots than MAX_DETAILS_PER_RUN, each
    # run re-selected the same oldest 60, reported `pending` unchanged, and
    # never set the backfill-done marker, so the next run started over. Measured
    # on a fresh archive against 174 shots: 60 fetched, then "+0 shots, 114
    # pending" for as many runs as one cares to make.
    #
    # `full` now only chooses the listing mode and the label. Re-fetching a shot
    # that is already stored identically was never the point of a backfill;
    # making sure everything is there was.
    stale = [
        item for item in listed
        if item["id"] not in known
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
        if await _ingest_shot(client, db, item["id"], result,
                              result.enjoyment_scale) is False:
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
    client: DecaidClient, db: Database, shot_id: str, result: SyncResult,
    scale: str = SCALE_100,
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
        shot = shot_row_from_decaid(detail, synced_at, scale)
        _carry_ambiguity(shot, await asyncio.to_thread(db.get_shot_row, shot_id), scale)
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


async def _sync_beans(client: DecaidClient, db: Database, result: SyncResult) -> str:
    """Beans and batches. Both lists are small and come unpaginated.

    Decaid's version is noted along the way: ``status()`` works on the database
    only and cannot ask for itself. So is its rating scale, which the shots
    are read with; that is what this returns.
    """
    scale = await enjoyment_scale(client, db)
    synced_at = utc_now_iso()
    beans = [bean_row_from_decaid(b, synced_at) for b in await client.beans()]
    batches = [batch_row_from_decaid(b, synced_at) for b in await client.bean_batches()]
    if beans:
        result.beans = await asyncio.to_thread(db.upsert_beans, beans)
    if batches:
        result.bean_batches = await asyncio.to_thread(db.upsert_bean_batches, batches)
    return scale


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


async def enjoyment_scale(client: DecaidClient, db: Database) -> str:
    """The tablet's rating scale, determined once per Decaid version (T46).

    The probe asks the behaviour (``rejects_enjoyment_over_ten``); the build
    number is only the fallback when it gives no clear answer.
    """
    info = await client.info()
    version = str(info.get("fullVersion") or info.get("version") or "")
    await asyncio.to_thread(db.set_state, STATE_DECAID_VERSION, version)
    known = await asyncio.to_thread(db.get_state, STATE_ENJOYMENT_SCALE)
    if known and await asyncio.to_thread(db.get_state, STATE_ENJOYMENT_SCALE_FOR) == version:
        return known
    refused = await client.rejects_enjoyment_over_ten()
    if refused is None:
        refused = _build_number(info) >= ENJOYMENT_0_10_FROM_BUILD
    scale = SCALE_10 if refused else SCALE_100
    await asyncio.to_thread(db.set_state, STATE_ENJOYMENT_SCALE, scale)
    await asyncio.to_thread(db.set_state, STATE_ENJOYMENT_SCALE_FOR, version)
    if scale != known:
        log.info("enjoyment scale", extra={"fields": {"scale": scale, "version": version}})
    return scale


def _build_number(info: dict[str, Any]) -> int:
    raw = info.get("buildNumber") or str(info.get("fullVersion") or "").partition("+")[2]
    try:
        return int(str(raw))
    except ValueError:
        return 0


def _carry_ambiguity(row: dict[str, Any], previous: Any, scale: str) -> None:
    """Keep a rating marked ambiguous across the switch to 0-10 (T46).

    Decaid's migration leaves such a value as it was and does not touch
    ``updatedAt``, so read on 0-10 it would pass for canonical. It still is
    not - until someone rates the shot again, which moves ``updatedAt``.
    """
    if (scale == SCALE_10 and previous is not None and previous["enjoyment_ambiguous"]
            and previous["enjoyment"] == row["enjoyment"]
            and previous["updated_at"] == row["updated_at"]):
        row["enjoyment_ambiguous"] = 1


def _reread_annotations(db: Database, listed: list[dict[str, Any]]) -> int:
    """Re-read every archived shot's annotations from the list, once (T46).

    Decaid's schema-6 migration rescales ratings without touching
    ``updatedAt``, so the cursor in ``run_sync`` never sees it. The list
    carries the annotations, so this costs no detail request. Returns how many
    shots changed.
    """
    stored = db.annotation_rows()
    synced_at = utc_now_iso()
    rows = []
    for item in listed:
        previous = stored.get(item.get("id"))
        if previous is None:
            continue           # not archived yet - the normal path fetches it
        fields = annotation_fields(item, SCALE_10)
        probe = {**fields, "updated_at": _stamp(item.get("updatedAt"))}
        _carry_ambiguity(probe, previous, SCALE_10)
        fields["enjoyment_ambiguous"] = probe["enjoyment_ambiguous"]
        if all(previous[k] == v for k, v in fields.items()):
            continue
        rows.append({
            **fields, "id": item["id"], "synced_at": synced_at,
            "weights_changed": (previous["dose_g"], previous["yield_g"])
                               != (fields["dose_g"], fields["yield_g"]),
            "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        })
    return db.update_annotations(rows)


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
    scale = await asyncio.to_thread(db.get_state, STATE_ENJOYMENT_SCALE) or SCALE_100
    shot = shot_row_from_decaid(detail, synced_at, scale)
    _carry_ambiguity(shot, await asyncio.to_thread(db.get_shot_row, shot_id), scale)
    await asyncio.to_thread(db.upsert_shot, shot, series_rows_from_decaid(detail))
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
        #: Replaced in tests; the upload watch waits between log reads.
        self._sleep = asyncio.sleep

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

    async def enjoyment_scale(self) -> str:
        """The tablet's rating scale, checked against its current version."""
        return await enjoyment_scale(self._client, self._db)

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
        self, fields: dict[str, Any], *, replace_unsaved: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Change the workflow context, with read-back.

        The workflow is the setting for the *next* shot; it only reaches the
        archive once a shot has actually been pulled with it. So there is
        nothing to update locally here.

        A batch change carries the coffee labels with it - see
        ``_coffee_labels`` for why that is our job rather than Decaid's.
        """
        patch = dict(fields)
        reference = patch.pop("profileId", None)
        profile: dict[str, Any] | None = None
        records: list[dict[str, Any]] = []
        if reference:
            # The workflow embeds the profile as an object and holds no id
            # (SPEC T38), so selecting one means copying it in. An unknown
            # reference is refused here: Decaid would take any object at all.
            records = await self._client.profiles(include_hidden=True)
            profile = resolve_profile(records, str(reference))["profile"]
        return await self._apply(patch, profile, records, replace_unsaved=replace_unsaved)

    async def _apply(
        self, patch: dict[str, Any], profile: dict[str, Any] | None,
        records: list[dict[str, Any]], *, replace_unsaved: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The one path that writes the workflow, with every guard rail on it.

        `set_workflow` and `apply_recipe` both end here, so the batch labels
        (T28) and the unsaved-profile guard cannot be had by one and missed by
        the other.
        """
        patch = dict(patch)
        if patch.get("beanBatchId"):
            patch.update(await self._coffee_labels(str(patch["beanBatchId"])))
        if profile is not None:
            current = (await self._client.workflow()).get("profile")
            if (current and not replace_unsaved and not same_brew(current, profile)
                    and await self._would_be_lost(current, records)):
                # Measured on the live instance: the workflow was running a
                # D-Flow tuned on the tablet (pour 1.5 ml/s, limiter 9 bar) that
                # matched none of 85 stored profiles. It existed only here.
                # Selecting another profile would have discarded it with no way
                # back from this server.
                raise Protected(
                    f"The workflow runs {current.get('title')!r} as tuned on the "
                    "tablet, and that version is not saved as a profile anywhere. "
                    "Selecting another one discards it for good. Keep it with "
                    "save_workflow_profile first - or, if the user accepts losing "
                    "it, say replace_unsaved_profile."
                )

        body: dict[str, Any] = {}
        if profile is not None:
            body["profile"] = profile
        if patch:
            body["context"] = patch

        async with self._lock:
            before_wf = await self._client.workflow()
            watch = await self._before_upload(before_wf, profile)
            await self._client.update_workflow(body)
            after_wf = await self._client.workflow()
        upload = await self._after_upload(watch)

        before = dict(before_wf.get("context") or {})
        after = dict(after_wf.get("context") or {})
        if upload is not None:
            after[MACHINE_UPLOAD] = upload
        if profile is not None:
            for side, workflow in ((before, before_wf), (after, after_wf)):
                running = running_profile(workflow.get("profile"), records)
                side["profileId"] = running.get("id") if running else None
                side["profileTitle"] = (workflow.get("profile") or {}).get("title")
        return before, after

    # ------------------------------------------------ Profile upload (T47)

    async def _before_upload(
        self, before_wf: dict[str, Any], profile: dict[str, Any] | None,
    ) -> tuple[str, Any] | None:
        """What to watch from, if this write makes Decaid upload a profile.

        Decaid uploads whenever the workflow's profile object changes, a new
        title included - so only an identical profile is left alone. Before
        the PUT: whether the DE1 is connected (Decaid skips the upload
        silently otherwise), and the tablet's clock as the log shows it.
        """
        current = before_wf.get("profile")
        if profile is None or (current and same_brew(current, profile)
                               and current.get("title") == profile.get("title")):
            return None
        link = await self._machine_link()
        if link == "disconnected":
            return link, None
        try:
            mark = newest(await self._client.log_tail())
        except DecaidError:
            mark = None
        return link, mark

    async def _after_upload(self, watch: tuple[str, Any] | None) -> dict[str, Any] | None:
        """Watch Decaid's log for the upload to end, a few seconds at most."""
        if watch is None:
            return None
        link, mark = watch
        if link == "disconnected":
            return self._upload_result("pending_connection", link)
        if mark is not None:
            for _ in range(int(WATCH_SECONDS / POLL_SECONDS)):
                await self._sleep(POLL_SECONDS)
                try:
                    found = outcome(await self._client.log_tail(), mark)
                except DecaidError:
                    break
                if found is not None:
                    return self._upload_result(found.pop("profile_upload"), link, found)
        return self._upload_result("unconfirmed", link)

    @staticmethod
    def _upload_result(state: str, link: str, extra: dict | None = None) -> dict[str, Any]:
        result = {"profile_upload": state, "de1": link, **(extra or {})}
        if state in UPLOAD_NOTES:
            result["note"] = UPLOAD_NOTES[state]
        return result

    async def _machine_link(self) -> str:
        """``connected``, ``disconnected`` or ``unknown`` - the DE1 over Bluetooth."""
        try:
            devices = await self._client.devices()
        except DecaidError:
            return "unknown"
        machine = next((d for d in devices if d.get("type") == "machine"), None)
        if machine is None:
            return "disconnected"
        return "connected" if machine.get("state") == "connected" else "disconnected"

    async def _would_be_lost(
        self, current: dict[str, Any], records: list[dict[str, Any]]
    ) -> bool:
        """Would replacing this workflow profile lose it for good?

        Not when it is a stored profile, not when a recipe holds it as its
        snapshot, and not when this server replaced it itself with
        update_profile. The last is the case the first version of this guard
        got wrong: after an update the workflow still runs the previous version,
        which Decaid no longer stores (T37), and the guard then blocked the very
        set_workflow that update_profile offers as the next step.
        """
        if running_profile(current, records) is not None:
            return False
        known = await asyncio.to_thread(self._db.known_brews)
        return brew_hash(current) not in known

    # -------------------------------------------------------------- Recipes

    async def save_workflow_profile(self, title: str | None) -> dict[str, Any]:
        """Keep the profile the workflow is running as a named profile.

        Refused when that content is already stored: Decaid would answer the
        POST with 201 and the existing record, dropping the new title (T39) -
        a success report for a profile that was never made. The existing one is
        named instead.

        Afterwards the workflow is given the stored copy back. Same brewing
        content, so nothing about the next shot changes; but the workflow then
        carries the new title, and machine and tablet list say the same thing.
        """
        async with self._lock:
            workflow = await self._client.workflow()
            embedded = workflow.get("profile")
            if not embedded:
                raise ValidationError(["profile: the workflow carries no profile"])
            records = await self._client.profiles(include_hidden=True)
            existing = running_profile(embedded, records)
            if existing is not None:
                raise AlreadyExists(
                    "The workflow's profile is already stored as "
                    f"{(existing.get('profile') or {}).get('title')!r} "
                    f"({existing.get('id')}, {existing.get('visibility')}). Nothing "
                    "was created."
                )
            context = workflow.get("context") or {}
            title = title or default_title(context.get("coffeeRoaster"),
                                           context.get("coffeeName"))
            if not title:
                raise ValidationError(["title: give one - the workflow names no "
                                       "coffee to call it after"])
            taken = [str((r.get("profile") or {}).get("title") or "")
                     for r in records if r.get("visibility") != "deleted"]
            title = check_title(title, taken)
            profile = dict(embedded, title=title)
            created = await self._client.create_profile(
                profile, parent_id=None,
                metadata={MARKER_KEY: MARKER, "savedFromWorkflow": True},
            )
            after = await self._client.profile(str(created["id"]))
            await self._client.update_workflow({"profile": after["profile"]})
        return {"record": after, "was_titled": embedded.get("title")}

    async def dye2_recipes(self) -> list[dict[str, Any]]:
        """DYE2's recipes, read. There is no method here that writes them."""
        return await self._client.store_array(DYE2_NAMESPACE, DYE2_RECIPES)

    async def capture_recipe(self, name: str | None, *, pin: bool) -> dict[str, Any]:
        """What the machine is set to now, as a recipe row - not yet stored.

        Refused when the profile exists only in the workflow. A recipe pointing
        at such a profile would point at nothing the moment the workflow moves
        on; `save_workflow_profile` keeps it first.
        """
        workflow = await self._client.workflow()
        records = await self._client.profiles(include_hidden=True)
        record = running_profile(workflow.get("profile"), records)
        context = workflow.get("context") or {}
        if record is None:
            raise Protected(
                f"The workflow runs {(workflow.get('profile') or {}).get('title')!r}"
                " as tuned on the tablet, and that version is stored nowhere - a "
                "recipe would point at nothing once the workflow moves on. Keep it "
                "with save_workflow_profile first"
                + (f" (suggested title: {default_name(context)!r})"
                   if default_name(context) else "") + "."
            )
        name = name or default_name(context)
        if not name:
            raise ValidationError(["name: give one - the workflow names no coffee"])
        bean_id = None
        if context.get("beanBatchId"):
            with contextlib.suppress(ShotNotFound):
                bean_id = (await self._client.bean_batch(
                    str(context["beanBatchId"]))).get("beanId")
        return row_from_workflow(" ".join(name.split()), context, record,
                                 bean_id=bean_id, pin=pin)

    async def apply_own_recipe(
        self, row: dict[str, Any], *, replace_unsaved: bool,
    ) -> tuple[tuple[dict[str, Any], dict[str, Any]], dict[str, Any]]:
        """Our recipe onto the machine: current profile version, or the pin.

        Unpinned, the profile is looked up by title and so follows every tuning
        despite the id changing each time (T37). A title that no longer
        resolves is refused rather than quietly replaced by the snapshot - the
        user asked for the profile as it is now, and an old copy is a different
        answer to that question.
        """
        records = await self._client.profiles(include_hidden=True)
        snapshot = json.loads(row["profile_snapshot"])
        if row.get("pin_profile"):
            profile, used = snapshot, {"pinned": True, "title": snapshot.get("title")}
        else:
            try:
                record = resolve_profile(records, str(row["profile_title"]))
            except ValidationError:
                raise ValidationError([
                    f"profile: {row['profile_title']!r} is no longer on the tablet. "
                    "Pin the recipe to its snapshot with update_recipe, or save it "
                    "again from a profile that exists."]) from None
            profile = record["profile"]
            used = {"pinned": False, "id": record.get("id"),
                    "title": profile.get("title"),
                    "differs_from_snapshot": not same_brew(profile, snapshot)}
        result = await self._apply(context_patch(row), profile, records,
                                   replace_unsaved=replace_unsaved)
        return result, used

    async def apply_dye2_recipe(
        self, item: dict[str, Any], *, replace_unsaved: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """A DYE2 recipe's `workflow`, PUT as it is - the contract's apply.

        Nothing is added to it and nothing taken away; the batch labels in its
        context are DYE2's to write. The unsaved-profile guard still runs
        first, because it only ever refuses and changes nothing it sends.
        """
        workflow = item.get("workflow")
        if not isinstance(workflow, dict):
            raise ValidationError([f"recipe: {dye2_name(item)!r} was {LEGACY_NOTE}"])
        records = await self._client.profiles(include_hidden=True)
        profile = workflow.get("profile")
        async with self._lock:
            before_wf = await self._client.workflow()
            current = before_wf.get("profile")
            # A stub without steps (T44) replaces no brewing content - the
            # running profile survives it - so there is nothing to protect.
            if (profile and profile.get("steps") and current and not replace_unsaved
                    and not same_brew(current, profile)
                    and await self._would_be_lost(current, records)):
                raise Protected(
                    f"The workflow runs {current.get('title')!r} as tuned on the "
                    "tablet, stored nowhere. Applying this recipe discards it. Keep "
                    "it with save_workflow_profile first - or say "
                    "replace_unsaved_profile."
                )
            watch = await self._before_upload(before_wf, profile)
            await self._client.update_workflow(workflow)
            after_wf = await self._client.workflow()
        upload = await self._after_upload(watch)
        if upload is not None:
            after_wf = {**after_wf, MACHINE_UPLOAD: upload}
        return before_wf, after_wf, await self._label_mismatch(after_wf)

    async def catalogue(self) -> Catalogue:
        """Batches, beans and profiles in one go, to judge listed entries by."""
        batches, beans, records = await asyncio.gather(
            self._client.bean_batches(), self._client.beans(),
            self._client.profiles(include_hidden=True))
        live = [r for r in records if r.get("visibility") != "deleted"]
        return Catalogue(
            batches={str(b["id"]): str(b.get("beanId")) for b in batches},
            beans={str(b["id"]): (b.get("name"), b.get("roaster")) for b in beans},
            profile_ids=frozenset(str(r.get("id")) for r in live),
            profile_titles=frozenset(str((r.get("profile") or {}).get("title") or "")
                                     .casefold() for r in live),
        )

    async def dye2_favourites(self) -> list[dict[str, Any]]:
        """DYE2's favourites, read. Like its recipes, never written from here."""
        return await self._client.store_array(DYE2_NAMESPACE, DYE2_FAVOURITES)

    async def apply_dye2_favourite(
        self, fav: dict[str, Any], *, replace_unsaved: bool,
    ) -> tuple[tuple[dict[str, Any], dict[str, Any]], dict[str, Any]]:
        """A DYE2 favourite onto the machine: its context, copyMask respected,
        and its profile only where there is one.

        Measured (T44): a saved favourite stores its profile as `{id, title}`
        without steps - DYE2's own builder writes it that way - and Decaid
        neither resolves the id nor keeps it, so PUT as stored the stub only
        renames whatever is running. Here a stub with an id is resolved and the
        full profile sent; a stub with no id is not sent at all, and the
        response says the current profile was kept. That is where DYE2's own
        apply stumbles and this does not.

        The context goes through the same guarded path as everything else, so
        a batch brings its labels along (T28) and an unknown one is refused.
        """
        context, profile, masked = favourite_workflow(fav)
        kind = profile_kind(profile)
        records = await self._client.profiles(include_hidden=True)
        chosen: dict[str, Any] | None = None
        used: dict[str, Any] = {"kind": kind}
        if kind == "full":
            chosen = profile
            used["title"] = profile.get("title")
        elif kind == "reference":
            record = next((r for r in records if r.get("id") == profile["id"]
                           and r.get("visibility") != "deleted"), None)
            if record is not None:
                chosen = record["profile"]
                used |= {"title": chosen.get("title"), "id": record["id"],
                         "note": "DYE2 stores this profile as a reference only; "
                                 "resolved and sent in full, since Decaid would "
                                 "not resolve it (T44)."}
            else:
                used["note"] = (f"favourite refers to profile {profile['id']}, which "
                                "is not on the tablet; keeping the current profile "
                                "untouched")
        elif kind == "name":
            used["note"] = ("favourite carries a name-only profile "
                            f"({profile.get('title')!r}); keeping the current "
                            "profile untouched")
        if masked:
            used["masked_off"] = masked
        if context.get("beanBatchId"):
            await self._refuse_bean_as_batch(str(context["beanBatchId"]), fav)
        result = await self._apply(context, chosen, records, replace_unsaved=replace_unsaved)
        return result, used

    async def _refuse_bean_as_batch(self, batch_id: str, fav: dict[str, Any]) -> None:
        """Refuse a favourite whose `beanBatchId` is really a bean's id.

        Measured (T45): six of eight live favourites store a bean id there - it
        answers 404 as a batch and 200 as a bean. Decaid would take it (T29),
        and every shot afterwards would point at a batch that does not exist.
        Picking one of the bean's batches would be a guess, so the batches are
        named and the choice left to the user.
        """
        try:
            await self._client.bean_batch(batch_id)
            return
        except ShotNotFound:
            pass
        try:
            bean = await self._client.bean(batch_id)
        except ShotNotFound:
            return          # neither: _coffee_labels refuses it with its own reason
        batches = await self._client.bean_batches(batch_id)
        listed = ", ".join(f"{b.get('id')} (roasted {str(b.get('roastDate') or '?')[:10]})"
                           for b in batches) or "none - create_batch first"
        raise ValidationError([
            f"favourite {favourite_name(fav)!r} stores the id of the bean "
            f"{bean.get('name')!r} where a batch belongs, so it would point the "
            f"machine at a batch that does not exist. That bean's batches: "
            f"{listed}. Nothing was written. set_workflow with one of these sets "
            "the machine; correcting the favourite itself is done in DYE2."])

    async def _label_mismatch(self, workflow: dict[str, Any]) -> str | None:
        """Does the machine now name a different coffee than its batch holds?

        Measured (T42): DYE2's live recipe "Decaf" sets coffeeName and neither
        the roaster nor the batch, so applied on another coffee's batch the
        machine labels that batch "Sugar Cane Decaf". Applying unchanged is the
        contract; saying what came of it is the least a read-back owes.
        """
        context = workflow.get("context") or {}
        batch_id = context.get("beanBatchId")
        if not batch_id:
            return None
        try:
            labels = await self._coffee_labels(str(batch_id))
        except ShotNotFound:
            return f"the workflow names batch {batch_id!r}, which Decaid does not have"
        shown = (context.get("coffeeRoaster"), context.get("coffeeName"))
        actual = (labels["coffeeRoaster"], labels["coffeeName"])
        if shown == actual:
            return None
        return (f"the machine now shows {shown[0]} / {shown[1]} on a batch of "
                f"{actual[0]} / {actual[1]}. Shots pulled like this are labelled "
                "with the wrong coffee - set_workflow with the right beanBatchId "
                "puts it right.")

    # ------------------------------------------------------------ Creating

    async def create_bean(self, fields: dict[str, Any]) -> dict[str, Any]:
        """A new bean, read back from Decaid and archived.

        Refused before sending when a bean of the same name and roaster exists -
        compared case- and whitespace-insensitively, because "Tugu Kawisari"
        and "tugu  kawisari" are one roaster to a person and two records to a
        database. A second bean by accident splits a coffee's history in two,
        and nothing afterwards brings the halves back together.
        """
        async with self._lock:
            existing = await self._client.beans()
            twin = same_bean(existing, fields.get("name"), fields.get("roaster"))
            if twin is not None:
                raise AlreadyExists(
                    f"A bean {twin.get('name')!r} by {twin.get('roaster')!r} "
                    f"already exists as {twin.get('id')}. Nothing was created; "
                    "use that one, or give the new bean a distinguishing name."
                )
            created = await self._client.create_bean(dict(fields))
            after = await self._client.bean(str(created["id"]))
            await asyncio.to_thread(
                self._db.upsert_beans, [bean_row_from_decaid(after, utc_now_iso())]
            )
        return after

    async def create_batch(self, bean_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """A new batch of an existing bean, read back and archived.

        The bean is looked up first: Decaid would answer an unknown one with a
        404 anyway, but "no such bean" is a clearer thing to be told than "not
        found" about a path nobody typed.
        """
        async with self._lock:
            try:
                bean = await self._client.bean(bean_id)
            except ShotNotFound:
                raise ShotNotFound(
                    f"Decaid has no bean {bean_id!r}. Nothing was created."
                ) from None
            created = await self._client.create_bean_batch(bean_id, dict(fields))
            after = await self._client.bean_batch(str(created["id"]))
            await asyncio.to_thread(
                self._db.upsert_bean_batches,
                [batch_row_from_decaid(after, utc_now_iso())],
            )
        after["_bean"] = {"name": bean.get("name"), "roaster": bean.get("roaster")}
        return after

    # ------------------------------------------------------------ Profiles

    async def profile_catalogue(self) -> list[dict[str, Any]]:
        return await self._client.profiles(include_hidden=True)

    async def clone_profile(
        self, source: str, overrides: dict[str, Any], *,
        title: str | None, bean_id: str | None,
    ) -> dict[str, Any]:
        """A copy of ``source`` with lineage back to it, and the overrides applied.

        Refused when the result would brew exactly like an existing profile.
        Decaid's id is a hash of the brewing content, so such a copy would not
        be a new profile but the old one under a second name - and whatever
        Decaid then does with it, it is not what the caller asked for.
        """
        async with self._lock:
            records = await self._client.profiles(include_hidden=True)
            original = resolve_profile(records, source)

            if title is None and bean_id:
                bean = await self._client.bean(bean_id)
                title = default_title(bean.get("roaster"), bean.get("name"))
            if title is None:
                raise ValidationError([
                    "title: give a title, or a bean to name the profile after "
                    f"(\"<roaster>{TITLE_SEPARATOR}<bean>\")"
                ])
            taken = [str((r.get("profile") or {}).get("title") or "")
                     for r in records if r.get("visibility") != "deleted"]
            title = check_title(title, taken)

            profile, changes = apply_overrides(original["profile"], overrides,
                                               title=title)
            if not changes_brewing(changes):
                raise ValidationError([
                    "overrides: a copy that brews identically is the same profile "
                    "to Decaid - its id is a hash of the brewing content. Change at "
                    "least one of " + ", ".join(OVERRIDES) + ", or select the "
                    "original directly."
                ])
            twin = next((r for r in records if same_brew(r.get("profile"), profile)),
                        None)
            if twin is not None:
                raise AlreadyExists(
                    f"A profile that brews exactly like this already exists: "
                    f"{(twin.get('profile') or {}).get('title')!r} "
                    f"({twin.get('id')}, {twin.get('visibility')}). Nothing was "
                    "created; select that one instead."
                )
            created = await self._client.create_profile(
                profile, parent_id=original.get("id"),
                metadata={MARKER_KEY: MARKER, "clonedFrom": original.get("id")},
            )
            after = await self._client.profile(str(created["id"]))
        return {"record": after, "source": original, "changes": changes}

    async def update_profile(
        self, profile_id: str, overrides: dict[str, Any], *, allow_foreign: bool,
    ) -> dict[str, Any]:
        """Change one of our profiles; defaults are refused whatever is asked.

        The id changes with the content (T37): Decaid replaces the record under
        a new hash. So the read-back follows the id the PUT answered with, and
        the response says so - a caller holding the old id would otherwise be
        holding a reference to nothing.
        """
        async with self._lock:
            records = await self._client.profiles(include_hidden=True)
            record = resolve_profile(records, profile_id)
            title = (record.get("profile") or {}).get("title")
            if is_default(record):
                raise Protected(
                    f"{title!r} is one of Decaid's bundled defaults. Those are not "
                    "changed from here - clone it and change the copy."
                )
            if not made_here(record) and not allow_foreign:
                raise Protected(
                    f"{title!r} was not made by clone_profile, so it may be a "
                    "profile somebody tuned by hand on the tablet. Only change it "
                    "if the user named it explicitly, and then say allow_foreign."
                )
            profile, changes = apply_overrides(record["profile"], overrides)
            if not changes_brewing(changes):
                raise ValidationError(["overrides: nothing to change"])

            workflow = await self._client.workflow()
            was_running = same_brew(workflow.get("profile"), record["profile"])
            updated = await self._client.update_profile(str(record["id"]), profile)
            after = await self._client.profile(str(updated["id"]))
            await asyncio.to_thread(
                self._db.remember_superseded, brew_hash(record["profile"]),
                (record.get("profile") or {}).get("title"), after.get("id"))
        return {"record": after, "before": record, "changes": changes,
                "workflow_ran_previous_version": was_running}

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


def same_bean(
    beans: list[dict[str, Any]], name: Any, roaster: Any
) -> dict[str, Any] | None:
    """An existing bean with this name and roaster, compared as a person would.

    Case and runs of whitespace do not make a different coffee.
    """
    def key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    wanted = (key(name), key(roaster))
    return next((b for b in beans if (key(b.get("name")), key(b.get("roaster")))
                 == wanted), None)


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
    "MACHINE_UPLOAD",
    "STATE_BACKFILL_DONE",
    "STATE_DECAID_VERSION",
    "STATE_ENJOYMENT_REREAD",
    "STATE_ENJOYMENT_SCALE",
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
