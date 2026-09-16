"""FastMCP app: streamable HTTP endpoint under the secret path (SPEC §9, §10).

The tool docstrings are not decoration: Claude reads them and has to work out
from them, without asking back, what a number means. Units, the semantics of
`pi_end`, the difference between the two pressure maxima and the meaning of
`warnings` therefore live in the server prompt (always in context) and, in
shortened form, in every tool that returns the affected fields.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from . import __version__
from .config import Config
from .db import Database
from .decaid_client import (
    VERIFIED_DECAID_VERSION,
    DecaidClient,
    DecaidError,
    DecaidUnreachable,
)
from .guards import (
    ALL_RULES,
    as_datetime,
    bean_age_days,
    bean_age_not_checkable,
    run_rules,
)
from .metrics import METRICS_VERSION, curve_shape, downsample_curve, metrics_for_shot
from .stats import _iso as _stats_iso
from .stats import (
    batch_usage,
    compare,
    parse_period,
    summarise,
)
from .sync import (
    QUICK_SYNC_MAX_AGE_S,
    STATE_BACKFILL_DONE,
    STATE_DECAID_VERSION,
    STATE_LAST_REACHABLE,
    STATE_LAST_RESULT,
    STATE_LAST_SYNC,
    SyncCoordinator,
    open_database,
    periodic_sync,
)
from .telemetry import CallMetricsMiddleware
from .writes import BATCH, BEAN, WORKFLOW, Ruleset, ValidationError, validate_fields

log = logging.getLogger(__name__)

SERVER_NAME = "decentespresso"

#: How long a sync may stay absent before status() remarks on it. The tablet
#: is often off; only a longer silence means something is missing.
STALE_SYNC_WARN_DAYS = 7

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: SPEC §9.1: the point arrays are the exception, not the rule - hence lower
#: than the 120 suggested in §9.1.
DEFAULT_MAX_POINTS = 60

#: Points shared between *all* compared shots together. SPEC §9 names a flat
#: 60 per shot - four shots with curves blow the response budget that way
#: (measured 18.2 kB). Split up, even the worst case stays below it.
COMPARE_POINT_BUDGET = 100
COMPARE_MIN_POINTS = 20
NOTES_PREVIEW_CHARS = 160

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

#: Decaid maintains these itself on every write (T15). They move on their own
#: and reporting them as a change would be noise, not information.
SERVER_MANAGED_FIELDS = frozenset({"createdAt", "updatedAt"})

INSTRUCTIONS = """\
Local archive of espresso shots pulled on a Decent DE1. The source is Decaid on
the tablet at the machine, reached over the local network; this server holds the
complete history and is the basis for any analysis.

THE TABLET BEING OFF is not an error. It only runs while coffee is being made.
When a tool reports `waiting_for_tablet`, or the status says so, it means: not
reachable right now, and the archive answers anyway. Say it that way - do not
present it as a fault.

UNITS (used throughout, never sent along): pressure bar, flow ml/s, weight and
dose g, temperature degrees Celsius, time s. Timestamps are ISO8601 in UTC;
`elapsed`/`t` counts from the start of the shot.

TERMS that appear in the metrics:

- `pi_end` - end of preinfusion, in seconds. This is a phase transition the
  machine itself reports, not a threshold and not an estimate.
  `pi_end_source` names where the value actually came from, in this order:
  `substate` = the machine reported the change to `pouring` in plain text -
  read off, not inferred;
  `profile_frame` = no state reported, so the last profile-step boundary before
  the pressure anchor was used;
  `heuristic` = neither was available, so the moment pressure first reaches
  60 % of its maximum was used. With `heuristic` the value is an approximation
  and is not fit for comparisons down to a tenth of a second.

- Two pressure maxima, deliberately kept apart:
  `peak_pressure_infusion` is the maximum up to shortly after preinfusion
  (window `[0, pi_end + 2 s]`). That is the pressure building the puck - the
  number that matters when dialling in.
  `max_pressure_global` is the maximum across the whole shot. With profiles
  that ramp pressure up (D-Flow, for instance) it sits on the last data point
  and says nothing about how the puck was built. Do not confuse the two, and
  never interpret one against the other as a "rise".

- `warnings` - a non-empty list means at least one metric is unreliable for
  this shot. The affected field is then `null`, not 0. The most common cause is
  the scale (not tared, knocked, not connected). Do not guess at fields set to
  `null` and do not skip past them - pass the warning on to the user in plain
  words whenever it bears on the question.

- `null` always means "not recorded", never "the value is 0". That holds for
  `enjoyment` in particular: shots imported from the de1app carry a default of
  0.0 in Decaid, which is not a rating. Ingestion stores those as `null`, so a
  0 that does reach you is a deliberate rating by the user.

CURVE - two representations, and the first one is almost always enough:

- `curve_shape` always comes along. It describes the shot segment by segment
  along the machine's phase markers: per segment `from`/`to` (time), and for
  pressure (`p`) and scale flow (`fo`) the starting value, the ending value,
  the direction (`rising` | `falling` | `steady`) and `linear` (whether the
  path between the ends is straight or curved). `markers` names the notable
  moments; `source` says where the segment boundaries came from (`machine` =
  the machine's phase markers, `markers` = derived from `pi_end` instead,
  `none` = a single segment). That answers questions about rise, fall, plateau,
  the length of a phase and the comparison of two shots without a single raw
  number.

- The point arrays (`t`, `p`, `fi`, `fo`, `w`, `tb`, as parallel lists) are
  only sent when explicitly asked for (`include_curve` or `include_curves`).
  They are roughly ten times the size of `curve_shape`. Ask for them only when
  the question is about shape detail that `curve_shape` cannot give - an
  oscillation inside one segment, say. A missing channel means there was not a
  single reading for it.

GUARDS - `audit_archive` checks four rules. What they mean:

- `grind_not_adjusted` - the batch was changed and the grind setting stayed put.
  Every bean grinds differently; the first shot after such a change is usually
  off.
- `bean_age` - the bean was past the age threshold when the shot was pulled.
  Time spent frozen is subtracted where a thaw date is recorded. When a finding
  says `certain: false`, the batch was frozen and thawed without one - the age
  is then an **upper bound** and must be passed on as one, not as a firm
  number. `not_checked` says how many shots the rule sat out for want of a
  roast date; it never guesses one.
- `missing_rating` - no rating was added once the grace period had passed.
- `dose_outlier` - weights do not match the workflow target. `basis` says what
  was measured against: the workflow target, or the batch median as a fallback.

A finding is a hint, not a verdict. It always names the numbers it rests on -
pass those along instead of merely repeating the message.

WRITING - call the `update_*` and `set_*` tools only on an explicit instruction,
never on your own initiative and never "just to be safe". Set exactly the fields
that were named. Confirm afterwards from the values that come back, not from
what was sent: if a field appears under `unchanged`, Decaid did not take it -
say so instead of reporting success.

DIAGNOSIS - what the machine measured about the coffee bed itself:

- `puck_resistance` - pressure divided by flow squared, over the stretch where
  the machine was holding its target. A simplified Darcy analogue: it stays
  near-constant while the bed does. `band` places it against this archive;
  `trend` is what matters more - a bed that loses resistance while the pressure
  is held is opening up, and `steep_decline` is the clearest sign of channeling
  in this data. It is not an absolute physical quantity: compare shots on this
  machine, and above all watch one shot change within itself.

- `channeling` - five independent signs, each with its own signature. `risk` is
  `low`, `elevated` or `high`; `fired` names the ones that tripped, `based_on`
  the ones that could be computed at all. An indicator missing from `based_on`
  was **not** checked - most often because the scale readings were unreliable -
  and that is not the same as passing. What each one means:

  `pressure_dip`      pressure fell away after the infusion peak: the bed gave
                      way and the pump briefly lost against it.
  `flow_instability`  flow would not settle while the machine held a constant
                      target. Measured only inside target-constant stretches,
                      so a profile that ramps on purpose is not blamed.
  `flow_divergence`   the pump delivered more than the scale received. Kept up,
                      that is liquid going somewhere other than the cup.
  `early_drops`       liquid reached the cup before preinfusion was over.
                      Nothing should come through a properly wetted bed then.
  `resistance_trend`  the bed lost resistance while the pressure was held.

  Two or more fired means `high`, one means `elevated`. Report the raw values,
  not just the band: a single indicator is a hint, not a diagnosis.

- `profile_compliance` / `temperature_vs_target` - what the machine was asked
  for against what it did, per data point. Each channel is judged only where it
  is the one being held. `temperature` is judged throughout, because the group
  is meant to hold its target whatever else is happening; `direction` says
  whether it ran above or below. A shot can follow its profile perfectly and
  still taste wrong - compliance tells you whether to look at the machine or at
  the coffee.

DETAIL LEVELS - `get_shot`, `get_shot_metrics` and `compare_shots` take
`detail`:

- `summary` (default) for triage: every metric, the curve shape, the resistance
  band and the channeling risk. Enough to say whether a shot is worth a closer
  look.
- `per_phase` to locate a cause: the raw value behind every indicator, and the
  compliance per phase of the profile. This is the level for "why did this taste
  wrong".
- `detailed` for shape detail: adds the point arrays, as `include_curve` does.

Start at `summary`. Going deeper costs roughly twice the size each step, and on
a comparison of four shots `detailed` is near the response budget.

STATISTICS - `stats(period)` leaves out cleaning and calibration profiles and
shots that produced under 5 g (aborts, which often run under an ordinary
profile name). `busiest_day` is the exception and counts everything, giving
the split - a day of flushes was still a day at the machine. Top lists carry
`rated` next to `shots`, because a mean over one rating is not the claim a
mean over ten is. Batches report what was used, not what is left: Decaid
stores no weight on a batch, so a remainder would be invented.\
"""


# --------------------------------------------------------------------- Tools


def build_mcp(
    config: Config, db: Database, coordinator: SyncCoordinator | None = None
) -> FastMCP:
    """Builds the FastMCP instance.

    ``coordinator`` is optional: without it every read tool works as usual,
    only ``sync_now`` and the freshness check of ``get_shot("latest")`` fall
    away. Tests use that to get by without a network.
    """
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        middleware=[CallMetricsMiddleware()],
    )

    @mcp.tool(annotations=READ_ONLY)
    async def list_beans() -> dict[str, Any]:
        """Every bean with shot count, date range, origin and its batches.

        The way into "which beans are there". Times are ISO8601 UTC,
        `grinder_setting` is free text ("4,2", for instance). `batches` carries
        the identifiers `update_batch` needs; `origin` is absent when nothing
        is recorded, which means unrecorded, never zero.
        """
        rows, batches = await asyncio.gather(
            asyncio.to_thread(db.list_beans),
            asyncio.to_thread(db.list_batches),
        )
        by_bean: dict[str, list[dict[str, Any]]] = {}
        for batch in batches:
            by_bean.setdefault(str(batch["bean_id"]), []).append({
                "id": batch["id"],
                "roast_date": batch["roast_date"],
                "frozen": bool(batch["frozen"]),
                "shot_count": batch["shot_count"],
                "first_shot": batch["first_shot"],
                "last_shot": batch["last_shot"],
            })
        return {
            "beans": [
                _drop_empty({
                    "id": r["bean_id"],
                    "name": r["bean_name"],
                    "roaster": r["roaster"],
                    "processing": r["processing"],
                    "decaf": bool(r["decaf"]) if r["decaf"] is not None else None,
                    "origin": _origin(r),
                    "shot_count": r["shot_count"],
                    "first_shot": r["first_shot"],
                    "last_shot": r["last_shot"],
                    "last_grinder_model": r["last_grinder_model"],
                    "last_grinder_setting": r["last_grinder_setting"],
                    "grinder_settings_used": r["grinder_settings"],
                    "batches": by_bean.get(str(r["bean_id"]), []),
                })
                for r in rows
            ]
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_shots(
        bean: str | None = None,
        roaster: str | None = None,
        profile: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Compact list of shots, newest first.

        Filters are case-insensitive substrings: `bean` matches the bean name
        or the roastery, `roaster` only the roastery, `profile` the profile
        name. `since`/`until` take ISO8601 (`2026-07-31`) or a relative
        shorthand (`12h`, `7d`, `2w`, `1m`, `1y`). `warnings` is the number
        of unreliable metrics on that shot.

        With more matches than `limit` a `next_cursor` comes back; send it
        again unchanged as `cursor`. The first page syncs with the tablet
        beforehand if needed (`freshness`); later pages do not.

        Units and terms: see the server instructions.
        """
        capped = max(1, min(int(limit), MAX_LIMIT))
        offset = _decode_cursor(cursor)

        freshness = None
        if cursor is None and coordinator is not None:
            freshness = await _refresh(coordinator)
        rows, total = await asyncio.to_thread(
            db.query_shots,
            bean=bean, roaster=roaster, profile=profile,
            since=_parse_time(since, "since"), until=_parse_time(until, "until"),
            limit=capped, offset=offset,
        )
        shots = []
        for row in rows:
            metrics = await asyncio.to_thread(metrics_for_shot, db, row["id"])
            shots.append(_compact_shot(row, metrics))
        following = offset + len(rows)
        payload: dict[str, Any] = {
            "shots": shots,
            "total_matching": total,
            "next_cursor": _encode_cursor(following) if following < total else None,
        }
        if freshness is not None:
            payload["freshness"] = freshness
        return payload

    @mcp.tool(annotations=READ_ONLY)
    async def get_shot(
        id: str = "latest",
        bean: str | None = None,
        detail: str = "summary",
        include_curve: bool = False,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> dict[str, Any]:
        """One shot: metadata, metrics, curve shape, profile summary.

        `id` is a shot UUID or `"latest"` (with `bean`, the newest of that
        bean); for `"latest"` the server syncs beforehand if needed.

        `detail` picks how deep to go - see the server instructions.
        `detailed` attaches the point arrays, thinned to `max_points` (default
        60, maximum 400); `include_curve=true` does the same at any level.
        """
        detail = _check_detail(detail)
        freshness = None
        if id == "latest" and coordinator is not None:
            freshness = await _refresh(coordinator)

        shot_id = id
        if id == "latest":
            shot_id = await asyncio.to_thread(db.latest_shot_id, bean)
            if shot_id is None:
                raise ToolError(
                    "shot_not_found: there is no archived shot"
                    + (f" for the bean {bean!r}." if bean else ".")
                )

        row = await asyncio.to_thread(db.get_shot_row, shot_id)
        if row is None:
            raise ToolError(f"shot_not_found: no shot with the identifier {shot_id!r}.")

        metrics = await asyncio.to_thread(metrics_for_shot, db, shot_id)
        series = await asyncio.to_thread(_series, db, shot_id)
        payload: dict[str, Any] = {
            "shot": _full_shot(row),
            "detail": detail,
            "metrics": _public_metrics(metrics, detail),
            "curve_shape": curve_shape(series, metrics),
            "profile": await asyncio.to_thread(_profile_summary, db, row["profile_id"]),
        }
        batch_row = (
            await asyncio.to_thread(db.batch_row, row["bean_batch_id"])
            if row["bean_batch_id"] else None
        )
        batch = _batch_block(batch_row, row["started_at"])
        if batch is not None:
            payload["bean_batch"] = batch
        if freshness is not None:
            payload["freshness"] = freshness
        if include_curve or detail == "detailed":
            payload["curve"] = _curve(series, metrics, max_points)
        return payload

    @mcp.tool(annotations=READ_ONLY)
    async def get_shot_metrics(id: str, detail: str = "summary") -> dict[str, Any]:
        """Only the derived metrics of a shot - no shape, no curve, no profile.

        The narrowest response when only numbers are needed. `detail` picks how
        deep the diagnostics go; see the server instructions.
        """
        detail = _check_detail(detail)
        metrics = await asyncio.to_thread(metrics_for_shot, db, id)
        if metrics is None:
            raise ToolError(f"shot_not_found: no shot with the identifier {id!r}.")
        return {"id": id, "detail": detail, **_public_metrics(metrics, detail)}

    @mcp.tool(annotations=READ_ONLY)
    async def compare_shots(
        ids: list[str],
        detail: str = "summary",
        include_profile: bool = True,
        include_curves: bool = False,
    ) -> dict[str, Any]:
        """Two to four shots side by side - metrics, shape, profiles, deltas.

        The first entry in `ids` is the reference; `deltas` gives the difference
        to it for every further shot. Fields that are `null` there are absent.

        `include_profile` (default true) returns the profile summary per shot
        and sets `profile_notice` when the shots did not run on the same
        targets - that usually makes a separate `get_profile` call unnecessary.
        `detail` picks how deep the diagnostics go; `detailed` also attaches
        the point arrays, as `include_curves=true` does at any level. See the
        server instructions.
        """
        detail = _check_detail(detail)
        if not 2 <= len(ids) <= 4:
            raise ToolError(
                f"invalid_argument: compare_shots takes 2 to 4 identifiers, "
                f"got {len(ids)}."
            )

        per_shot_points = max(COMPARE_MIN_POINTS, COMPARE_POINT_BUDGET // len(ids))

        entries: list[dict[str, Any]] = []
        profiles: list[dict[str, Any] | None] = []
        for shot_id in ids:
            row = await asyncio.to_thread(db.get_shot_row, shot_id)
            if row is None:
                raise ToolError(f"shot_not_found: no shot with the identifier {shot_id!r}.")
            metrics = await asyncio.to_thread(metrics_for_shot, db, shot_id)
            series = await asyncio.to_thread(_series, db, shot_id)
            entry: dict[str, Any] = {
                "id": shot_id,
                "started_at": row["started_at"],
                "bean": _bean_label(row),
                "profile_name": row["profile_name"],
                "grinder_setting": row["grinder_setting"],
                "dose_g": row["dose_g"],
                "yield_g": row["yield_g"],
                **_compare_metrics(metrics, detail),
                "curve_shape": curve_shape(series, metrics),
            }
            profile = (
                await asyncio.to_thread(_profile_brief, db, row["profile_id"])
                if include_profile
                else None
            )
            profiles.append(profile)
            if profile is not None:
                entry["profile"] = profile
            if include_curves or detail == "detailed":
                entry["curve"] = _curve(series, metrics, per_shot_points)
            entries.append(entry)

        payload: dict[str, Any] = {
            "reference": ids[0],
            "detail": detail,
            "shots": entries,
            "deltas": [_delta(entries[0], other) for other in entries[1:]],
        }
        if include_profile:
            notice = _profile_notice(profiles)
            if notice:
                payload["profile_notice"] = notice
        return payload

    @mcp.tool(annotations=READ_ONLY)
    async def list_batches(bean: str | None = None) -> dict[str, Any]:
        """Bean batches with roast date and how much was pulled from each.

        `bean` filters by name or roastery (substring). Newest roast first.
        Where the identifiers for `update_batch` come from. No age here - an age
        only means something against a shot, and `get_shot` gives it that way.
        """
        rows = await asyncio.to_thread(db.list_batches)
        if bean:
            needle = bean.strip().lower()
            rows = [
                r for r in rows
                if needle in (r["bean_name"] or "").lower()
                or needle in (r["bean_roaster"] or "").lower()
            ]
        return {
            "batches": [_batch_summary(r) for r in rows],
            "total": len(rows),
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_batch(id: str) -> dict[str, Any]:
        """One batch in full: all dates, freezer state, weights, usage.

        `frozen_since`/`thawed_on` carry the bean-age calculation. A `null`
        weight means none was recorded, not that the bag is empty.
        """
        row = await asyncio.to_thread(db.batch_row, id)
        if row is None:
            raise ToolError(
                f"batch_not_found: no batch with the identifier {id!r}. "
                "list_batches shows which exist."
            )
        usage = [b for b in await asyncio.to_thread(db.list_batches) if b["id"] == id]
        return _batch_summary(usage[0] if usage else dict(row), full=True)

    @mcp.tool(annotations=READ_ONLY)
    async def list_profiles() -> dict[str, Any]:
        """Every profile with its versions.

        `version_hash` is the identity of a version, `semantic_hash` groups
        versions with identical targets - more in the
        Server-Anweisungen.
        """
        rows = await asyncio.to_thread(db.profile_overview)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["name"], []).append({
                "version_hash": row["version_hash"][:8],
                "semantic_hash": (row["semantic_hash"] or "")[:8] or None,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "shot_count": row["shot_count"],
            })
        return {
            "profiles": [
                {"name": name, "version_count": len(versions), "versions": versions}
                for name, versions in grouped.items()
            ]
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_profile(
        shot_id: str | None = None,
        name: str | None = None,
        version_hash: str | None = None,
    ) -> dict[str, Any]:
        """The complete targets of a profile - give exactly one argument.

        `shot_id` returns the version this shot ran on; `version_hash` (a prefix
        suffices) a specific one, `name` the most recently seen one of that
        name. Within the steps `target` is bar for `mode: pressure` and ml/s
        otherwise; `exit` is `null` without an active exit condition. Legacy
        profiles have no steps - their targets sit in `legacy_settings`.

        For merely comparing two shots, `compare_shots` is enough.
        """
        given = [bool(shot_id), bool(name), bool(version_hash)]
        if sum(given) != 1:
            raise ToolError(
                "invalid_argument: exactly one argument expected - shot_id, name "
                "or version_hash."
            )

        if shot_id:
            row = await asyncio.to_thread(db.get_shot_row, shot_id)
            if row is None:
                raise ToolError(f"shot_not_found: no shot with the identifier {shot_id!r}.")
            if row["profile_id"] is None:
                raise ToolError(
                    f"profile_not_found: no profile archived for the shot "
                    f"{shot_id!r} "
                    "archiviert."
                )
            profile = await asyncio.to_thread(db.get_profile_row, row["profile_id"])
        else:
            profile = await asyncio.to_thread(
                db.find_profile, version_hash=version_hash, name=name
            )
        if profile is None:
            raise ToolError(
                "profile_not_found: no profile for "
                f"{'version_hash ' + repr(version_hash) if version_hash else 'name ' + repr(name)}."
            )

        parsed = json.loads(profile["parsed_json"])
        return {
            "name": profile["name"],
            "version_hash": profile["version_hash"][:8],
            "semantic_hash": (profile["semantic_hash"] or "")[:8] or None,
            "first_seen": profile["first_seen"],
            "last_seen": profile["last_seen"],
            **parsed,
        }

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True}
    )
    async def sync_now() -> dict[str, Any]:
        """Fetches new and changed shots from the tablet right away.

        Usually unnecessary: the server syncs on its own, and `get_shot`/
        `list_shots` check freshness anyway. Changes nothing in Decaid and
        can be repeated at will. `errors` are transient problems,
        `warnings` are final findings. If `waiting_for_tablet` is set the tablet
        was off - not an error, just nothing to fetch; say it that way.
        """
        if coordinator is None:
            raise ToolError(
                "sync_unavailable: this server runs without a connection to Decaid."
            )
        try:
            result = await coordinator.run(full=False)
        except DecaidError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
        return result.as_dict()

    @mcp.tool(annotations=READ_ONLY)
    async def status() -> dict[str, Any]:
        """State of the archive: contents, last sync, open warnings.

        Times are ISO8601 UTC. `decaid` says when the tablet was last
        reachable; `waiting_for_tablet` does not mean a fault but that the
        tablet is currently off - between two coffees that is the normal case
        and should not be reported as an error.
        """
        return await asyncio.to_thread(_status_payload, config, db)

    @mcp.tool(annotations=READ_ONLY)
    async def audit_archive(
        since: str | None = None, rule: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Checks the archive for inconsistencies (rules: see the instructions).

        `since` as an ISO date or shorthand (`7d`, `2w`, `1m`); `rule` narrows
        to one of the four rules.
        """
        cutoff = _parse_time(since, "since")
        shots = await asyncio.to_thread(db.shots_for_guards, cutoff)
        batches = await asyncio.to_thread(db.batches_by_id)
        findings = run_rules(
            shots, batches, at=datetime.now(UTC),
            enabled=config.guard_rules,
            warn_days=config.bean_age_warn_days,
            grace_hours=config.rating_grace_hours,
            tolerance_g=config.dose_tolerance_g,
        )
        if rule:
            if rule not in ALL_RULES:
                raise ToolError(
                    f"invalid_argument: {rule!r} is not a rule. Allowed: "
                    + ", ".join(ALL_RULES)
                )
            findings = [f for f in findings if f.rule == rule]

        capped = findings[: max(1, min(int(limit), 50))]
        return {
            "checked_shots": len(shots),
            "active_rules": list(config.guard_rules),
            "thresholds": {
                "bean_age_warn_days": config.bean_age_warn_days,
                "rating_grace_hours": config.rating_grace_hours,
                "dose_tolerance_g": config.dose_tolerance_g,
            },
            "total_findings": len(findings),
            "findings": [f.as_dict() for f in capped],
            "by_rule": _count_by_rule(findings),
            "not_checked": _drop_empty(bean_age_not_checkable(shots, batches)),
        }


    @mcp.tool(annotations=READ_ONLY)
    async def stats(period: str = "30d", compare_previous: bool = True) -> dict[str, Any]:
        """What was pulled in a period, from what, and how it tasted.

        `period` is an ISO date ("since then") or a shorthand like `30d`, `12h`,
        `4w`. `compare_previous` adds the same figures for the period of equal
        length before it, plus the deltas.

        Cleaning and calibration profiles are left out, as are shots that
        produced almost nothing - see the server instructions for what counts.
        `busiest_day` is the exception and counts everything, because a day of
        flushes was still a day at the machine.
        """
        try:
            since, until = parse_period(period, now=datetime.now(UTC))
        except ValueError as exc:
            raise ToolError(f"invalid_argument: {exc}") from exc

        rows = await asyncio.to_thread(
            db.shots_for_stats, _stats_iso(since), _stats_iso(until))
        batches = await asyncio.to_thread(db.batches_by_id)

        payload: dict[str, Any] = {
            "period": period,
            **summarise(rows, since=since, until=until),
            "batches": batch_usage(rows, batches, at=datetime.now(UTC)),
        }

        if compare_previous:
            span = until - since
            before_rows = await asyncio.to_thread(
                db.shots_for_stats, _stats_iso(since - span), _stats_iso(since))
            previous = summarise(before_rows, since=since - span, until=since)
            payload["comparison"] = compare(payload, previous)

        return payload

    if coordinator is not None:
        _register_workflow_reader(mcp, coordinator)

    if config.write_enabled and coordinator is not None:
        _register_update_shot(mcp, db, coordinator)
        _register_catalog_writes(mcp, db, coordinator)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        # Deliberately without the secret path: the Docker health check does not
        # know it. Reveals nothing, but per SPEC §12 must not reach the tunnel
        # ingress.
        return PlainTextResponse("ok")

    return mcp


def _register_workflow_reader(mcp: FastMCP, coordinator: SyncCoordinator) -> None:
    """``get_workflow`` reads live from the tablet - the archive does not hold it."""

    @mcp.tool(annotations=READ_ONLY)
    async def get_workflow() -> dict[str, Any]:
        """What the **next** shot is set up to run on - not what the last one did.

        Read live from the tablet, never cached. `grinder_setting` is what the
        grinder is dialled to now; the newest shot may have run on something
        else. On a disagreement say so instead of reconciling it - the change
        has either not been pulled on yet or was made and forgotten.
        """
        try:
            workflow = await coordinator.read_workflow()
        except DecaidUnreachable as exc:
            raise ToolError(f"waiting_for_tablet: {exc}") from exc
        except DecaidError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

        context = workflow.get("context") or {}
        profile = workflow.get("profile") or {}
        return {
            "bean_batch_id": context.get("beanBatchId"),
            "bean_name": context.get("coffeeName"),
            "bean_roaster": context.get("coffeeRoaster"),
            "grinder_model": context.get("grinderModel"),
            "grinder_setting": context.get("grinderSetting"),
            "target_dose_g": context.get("targetDoseWeight"),
            "target_yield_g": context.get("targetYield"),
            "profile": profile.get("title"),
        }


def _register_catalog_writes(
    mcp: FastMCP, db: Database, coordinator: SyncCoordinator
) -> None:
    """Write tools for bean, batch and workflow (SPEC §11).

    The same guard rails as ``update_shot``: whitelist before sending, read-back
    afterwards, and present only when ``WRITE_ENABLED`` is set.
    """

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def update_bean(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Changes the master data of a bean in Decaid.

        Identifier from `list_beans`. Allowed: name, roaster, species,
        processing, notes, decaf. Applies retroactively to every shot of this
        bean - say so beforehand.
        """
        return await _write(coordinator.write_bean, BEAN, id, fields,
                            what="bean")

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def update_batch(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Changes a bean batch (roast date, frozen state).

        Allowed: roastDate, buyDate, freezeDate (each ISO YYYY-MM-DD), frozen.
        Decaid keeps no thaw date - to thaw, set `frozen` to false; bean age is
        an upper bound only from then on.
        """
        return await _write(coordinator.write_batch, BATCH, id, fields,
                            what="batch")

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def set_workflow(fields: dict[str, Any]) -> dict[str, Any]:
        """Sets what the next shot should run on.

        Changes the machine, not the archive. Allowed: grinderSetting,
        grinderModel, targetDoseWeight, targetYield, beanBatchId. A profile
        change is not possible - that belongs at the machine. Afterwards name
        what is now set.

        `beanBatchId` also rewrites the coffee name and roastery shown on the
        machine, resolved from the batch; they come back under `alongside`.
        An unknown batch is refused rather than set.
        """
        return await _write(coordinator.write_workflow, WORKFLOW, None, fields,
                            what="workflow")


async def _write(
    writer: Any, ruleset: Ruleset, target_id: str | None,
    fields: dict[str, Any], *, what: str,
) -> dict[str, Any]:
    """The shared path of every write tool: validate, send, read back.

    One path rather than three, so that validation, read-back comparison and
    log line do not come out three slightly different ways.
    """
    try:
        payload = validate_fields(fields, ruleset)
    except ValidationError as exc:
        raise ToolError("invalid_argument: " + "; ".join(exc.problems)) from exc

    started = time.perf_counter()
    try:
        args = (payload,) if target_id is None else (target_id, payload)
        before, after = await writer(*args)
    except DecaidUnreachable as exc:
        raise ToolError(f"waiting_for_tablet: {exc}") from exc
    except DecaidError as exc:
        raise ToolError(f"{exc.code}: {exc}") from exc

    changes = {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in payload
    }
    ignored = [n for n, pair in changes.items() if pair["before"] == pair["after"]]

    # Report what moved, not what was sent. A field can change without being
    # asked for - `set_workflow` carries the coffee labels along with a batch
    # change (SPEC T28) - and a write that quietly changed something else is
    # exactly what a read-back is for.
    alongside = sorted(
        name for name, value in after.items()
        if name not in payload
        and name not in SERVER_MANAGED_FIELDS
        and before.get(name) != value
    )
    for name in alongside:
        changes[name] = {"before": before.get(name), "after": after.get(name)}

    # Field names yes, values no - notes can hold private things.
    log.info(
        f"{what} updated",
        extra={"fields": {
            "target": target_id or what,
            "wrote": ",".join(sorted(payload)),
            "unchanged": ",".join(sorted(ignored)) or "-",
            "dur_ms": round((time.perf_counter() - started) * 1000, 1),
        }},
    )

    result: dict[str, Any] = {"changes": changes}
    if target_id is not None:
        result["id"] = target_id
    if alongside:
        result["alongside"] = alongside
        result["note_alongside"] = (
            "These changed without being asked for, to keep the record "
            "consistent: " + ", ".join(alongside) + "."
        )
    if ignored:
        result["unchanged"] = ignored
        result["note"] = (
            "Decaid did not take these fields: " + ", ".join(ignored)
            + ". Either the new value was identical to the old one, or the API "
            "discarded it."
        )
    return result


def _count_by_rule(findings: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule] = counts.get(finding.rule, 0) + 1
    return counts


def _register_update_shot(mcp: FastMCP, db: Database, coordinator: SyncCoordinator) -> None:
    """Registers the write tool - only with ``WRITE_ENABLED`` (SPEC §11).

    Deliberately a separate function rather than a flag inside the tool: when
    the switch is off, ``update_shot`` does not appear in the tool list at all.
    A tool that exists and refuses invites asking again; one that does not
    exist does not.
    """

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def update_shot(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Changes the note, rating or weights of a shot in Decaid.

        `fields` maps field name -> new value; `null` clears a field. Allowed:
        espressoNotes, enjoyment (0-100), actualDoseWeight, actualYield (each in
        g). Timestamps, telemetry and profile are not.
        """
        try:
            payload = validate_fields(fields)
        except ValidationError as exc:
            raise ToolError("invalid_argument: " + "; ".join(exc.problems)) from exc

        row = await asyncio.to_thread(db.get_shot_row, id)
        if row is None:
            raise ToolError(f"shot_not_found: no shot with the identifier {id!r}.")

        started = time.perf_counter()
        try:
            before, after = await coordinator.write_shot(id, payload)
        except DecaidError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc

        changes = {
            name: {"before": before.get(name), "after": after.get(name)}
            for name in payload
        }
        ignored = [name for name, pair in changes.items()
                   if pair["before"] == pair["after"]]

        # Field names yes, values no - notes can hold private things.
        log.info(
            "shot updated",
            extra={"fields": {
                "shot": id,
                "wrote": ",".join(sorted(payload)),
                "unchanged": ",".join(sorted(ignored)) or "-",
                "dur_ms": round((time.perf_counter() - started) * 1000, 1),
            }},
        )

        result: dict[str, Any] = {"id": id, "changes": changes}
        if ignored:
            result["unchanged"] = ignored
            result["note"] = (
                "Decaid did not take these fields: "
                + ", ".join(ignored)
                + ". Either the new value was identical to the old one, or the "
                "API discarded it."
            )
        return result


# ------------------------------------------------------------------ Aufbereitung


async def _refresh(coordinator: SyncCoordinator) -> dict[str, Any]:
    """Freshness check for ``get_shot("latest")`` and the first ``list_shots`` page.

    A failure aborts nothing: the archive is there, just perhaps not brand new -
    which is a better answer than none at all.
    """
    try:
        result = await coordinator.ensure_fresh(QUICK_SYNC_MAX_AGE_S)
    except DecaidError as exc:
        # The archive is there, just perhaps not quite current - a better answer
        # than none at all.
        return {"synced": False, "note": f"Sync failed ({exc.code}), the answer "
                                         "comes from the archive."}
    if result is None:
        return {"synced": False, "note": "Archive was current, no sync needed."}
    if result.waiting_for_tablet:
        # Not an error: between two coffees the tablet is simply off.
        return {"synced": False, "waiting_for_tablet": True,
                "note": "Tablet not reachable, the answer comes from the archive."}
    return {"synced": True, "new_shots": result.new_shots, "updated": result.updated}


def _bean_label(row: Any) -> str | None:
    parts = [row["bean_roaster"], row["bean_name"]]
    label = " ".join(p for p in parts if p)
    return label or None


def _short(text: str | None, limit: int = NOTES_PREVIEW_CHARS) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _compact_shot(row: Any, metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = metrics or {}
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "bean": _bean_label(row),
        "profile": row["profile_name"],
        "grinder_setting": row["grinder_setting"],
        "dose_g": row["dose_g"],
        "yield_g": row["yield_g"],
        "ratio": row["ratio"],
        "duration_s": row["duration_s"],
        "peak_pressure_infusion": metrics.get("peak_pressure_infusion"),
        "enjoyment": row["enjoyment"],
        "notes": _short(row["notes"]),
        "warnings": len(metrics.get("warnings") or []),
    }


def _full_shot(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "time_source": row["time_source"],
        "bean_name": row["bean_name"],
        "bean_roaster": row["bean_roaster"],
        "bean_batch_id": row["bean_batch_id"],
        "basket": row["basket_name"],
        "grinder_model": row["grinder_model"],
        "grinder_setting": row["grinder_setting"],
        "dose_g": row["dose_g"],
        "yield_g": row["yield_g"],
        "ratio": row["ratio"],
        "duration_s": row["duration_s"],
        "target_dose_g": row["target_dose_g"],
        "target_yield_g": row["target_yield_g"],
        "stop_reason": row["stop_reason"],
        "enjoyment": row["enjoyment"],
        "notes": row["notes"],
    }


def _batch_summary(row: Any, *, full: bool = False) -> dict[str, Any]:
    """A batch as a list entry, or in full for ``get_batch``."""
    data = dict(row)
    summary: dict[str, Any] = {
        "id": data.get("id"),
        "bean_id": data.get("bean_id"),
        "bean_name": data.get("bean_name"),
        "bean_roaster": data.get("bean_roaster"),
        "roast_date": data.get("roast_date"),
        "frozen": bool(data.get("frozen")),
        "shot_count": data.get("shot_count"),
        "first_shot": data.get("first_shot"),
        "last_shot": data.get("last_shot"),
    }
    if full:
        summary |= {
            "buy_date": data.get("buy_date"),
            "open_date": data.get("open_date"),
            "best_before_date": data.get("best_before_date"),
            "frozen_since": data.get("freeze_date"),
            "thawed_on": data.get("unfreeze_date"),
            "weight_g": data.get("weight_g"),
            "weight_remaining_g": data.get("weight_remaining_g"),
            "archived": bool(data.get("archived")),
        }
        if not data.get("roast_date"):
            summary["age_unknown_reason"] = "This batch carries no roast date."
    return _drop_empty(summary) | {"frozen": bool(data.get("frozen"))}


def _drop_empty(block: dict[str, Any]) -> dict[str, Any]:
    """Leaves out keys whose value is None or an empty container.

    Only for optional blocks that are absent rather than null when nothing is
    recorded. Never for a measured field: there `null` is the statement.
    """
    return {k: v for k, v in block.items() if v not in (None, {}, [])}


def _origin(row: Any) -> dict[str, Any] | None:
    """Country, region, producer, variety, altitude - or nothing at all.

    Decaid's UI shows grey placeholder text in the empty fields ("washed,
    natural, honey…"); the API sends the key not at all. So an absent key here
    means nobody typed it, and inventing a value from the placeholder would
    turn a UI hint into a recorded fact.
    """
    keys = ("country", "region", "producer")
    block: dict[str, Any] = {k: row[k] for k in keys if _has(row, k)}
    for key in ("variety", "altitude"):
        if _has(row, key):
            with contextlib.suppress(ValueError, TypeError):
                block[key] = json.loads(row[key])
    return _drop_empty(block) or None


def _has(row: Any, key: str) -> bool:
    try:
        return row[key] is not None
    except (IndexError, KeyError):
        return False


def _batch_block(row: Any, started_at: str | None) -> dict[str, Any] | None:
    """The batch a shot was pulled from, aged **against that shot**.

    `days_off_roast` counts to `started_at`, never to now. An age measured
    against the clock would keep growing after the fact, so the same shot would
    answer "how old was the bean" differently every week and any comparison
    across a month would quietly drift.

    A missing roast date stays `null` and says why. There is no fallback to the
    purchase date, the open date or zero: those are different facts, and a
    plausible number in place of a missing one is worse than the gap, because
    nothing downstream can tell them apart.
    """
    if row is None:
        return None
    batch = dict(row)
    started = as_datetime(started_at)
    age, certain = bean_age_days(batch, datetime.now(UTC), started=started)

    block: dict[str, Any] = {
        "id": batch.get("id"),
        "roast_date": batch.get("roast_date"),
        "days_off_roast": age,
        "frozen": bool(batch.get("frozen")),
        "freeze_date": batch.get("freeze_date"),
        "unfreeze_date": batch.get("unfreeze_date"),
        "buy_date": batch.get("buy_date"),
        "open_date": batch.get("open_date"),
    }
    if age is None:
        block["age_unknown_reason"] = (
            "This batch carries no roast date."
            if not batch.get("roast_date")
            else "The roast date is after the shot - one of the two is wrong."
        )
    elif not certain:
        block["age_is_upper_bound"] = True
        block["age_upper_bound_reason"] = (
            "The batch was frozen and thawed, and no thaw date is recorded, so "
            "the time in the freezer cannot be subtracted. The bean is at most "
            "this old."
        )
    return block


DETAIL_LEVELS = ("summary", "per_phase", "detailed")

#: Fields that only earn their size once someone is looking for a cause.
_DIAGNOSTIC_KEYS = ("puck_resistance", "channeling", "profile_compliance")


def _public_metrics(
    metrics: dict[str, Any] | None, detail: str = "summary"
) -> dict[str, Any]:
    """Project the cached metrics onto a detail level.

    One cache entry serves all three levels - computing the diagnostics is
    cheap next to fetching the series, so they are always computed and only the
    answer is trimmed.

    ``summary`` keeps every scalar metric and reduces the diagnostics to their
    verdicts: a band for the resistance, a risk with the names of the
    indicators that fired. That is enough to triage a shot and costs a few
    hundred bytes. ``per_phase`` adds the raw value behind every indicator and
    the per-phase compliance - what one needs to locate a cause rather than
    notice one.
    """
    if not metrics:
        return {"warnings": ["No metrics computed."]}

    public = {
        k: v for k, v in metrics.items()
        if k not in ("metrics_version", "n_points")
    }
    if detail != "summary":
        return public

    resistance = public.get("puck_resistance")
    if resistance:
        public["puck_resistance"] = {
            "median": resistance["median"],
            "band": resistance["band"],
            "trend": resistance["trend"],
        }
    risk = public.get("channeling")
    if risk:
        public["channeling"] = {
            "risk": risk["risk"],
            "fired": risk["fired"],
            "based_on": risk["based_on"],
            **({"note": risk["note"]} if "note" in risk else {}),
        }
    compliance = public.pop("profile_compliance", None)
    if compliance:
        # The one compliance number worth carrying at triage: a group that sits
        # off its target explains a taste on its own.
        temperature = compliance.get("temperature")
        if temperature:
            public["temperature_vs_target"] = {
                "mean_deviation": temperature["mean_deviation"],
                "band": temperature["band"],
                "direction": temperature["direction"],
            }
    return public


def _compare_metrics(metrics: dict[str, Any] | None, detail: str) -> dict[str, Any]:
    """Like ``_public_metrics``, minus the per-phase table.

    Four phase tables side by side is not what a comparison is for - and it is
    what pushed compare_shots(4, per_phase) to 19 kB, well over budget. Anyone
    who needs the phases of one shot asks for that shot.
    """
    public = _public_metrics(metrics, detail)
    compliance = public.get("profile_compliance")
    if isinstance(compliance, dict) and "phases" in compliance:
        public["profile_compliance"] = {
            k: v for k, v in compliance.items() if k != "phases"
        }
        public["profile_compliance"]["phases_omitted"] = len(compliance["phases"])
    return public


def _check_detail(detail: str) -> str:
    if detail not in DETAIL_LEVELS:
        raise ToolError(
            f"invalid_argument: detail={detail!r} is not a level. "
            f"Allowed: {', '.join(DETAIL_LEVELS)}"
        )
    return detail


def _series(db: Database, shot_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.series_for_shot(shot_id)]


def _curve(
    rows: list[dict[str, Any]], metrics: dict[str, Any] | None, max_points: int
) -> dict[str, Any]:
    metrics = metrics or {}
    return downsample_curve(
        rows,
        max_points=max_points,
        keep_times=(metrics.get("t_peak"), metrics.get("t_max_pressure_global")),
    )


def _profile_summary(db: Database, profile_id: int | None) -> dict[str, Any] | None:
    """Summary for the shot detail - ``get_profile`` returns the full profile."""
    if profile_id is None:
        return None
    row = db.get_profile_row(profile_id)
    if row is None:
        return None
    parsed = json.loads(row["parsed_json"])
    return {
        "title": parsed.get("title") or row["name"],
        "type": parsed.get("type"),
        "version_hash": row["version_hash"][:8],
        "semantic_hash": (row["semantic_hash"] or "")[:8] or None,
        "target_weight_g": parsed.get("target_weight_g"),
        "target_temp_c": parsed.get("target_temp_c"),
        "parse_ok": parsed.get("parse_ok"),
        "steps": [
            {
                "name": s.get("name"),
                "mode": s.get("mode"),
                "target": s.get("target"),
                "temp_c": s.get("temp_c"),
                "duration_s": s.get("duration_s"),
            }
            for s in parsed.get("steps") or []
        ],
    }


def _profile_brief(db: Database, profile_id: int | None) -> dict[str, Any] | None:
    """Summary for ``compare_shots`` - headline targets, no step list.

    Enough for the question "did these shots run on the same profile"; the
    complete steps come from ``get_profile``.
    """
    if profile_id is None:
        return None
    row = db.get_profile_row(profile_id)
    if row is None:
        return None
    parsed = json.loads(row["parsed_json"])
    brief: dict[str, Any] = {
        "title": parsed.get("title") or row["name"],
        "type": parsed.get("type"),
        "version_hash": row["version_hash"][:8],
        "semantic_hash": (row["semantic_hash"] or "")[:8] or None,
        "target_weight_g": parsed.get("target_weight_g"),
        "target_temp_c": parsed.get("target_temp_c"),
        "step_count": len(parsed.get("steps") or []),
    }
    if parsed.get("legacy_settings"):
        brief["legacy_settings"] = parsed["legacy_settings"]
    return brief


def _profile_notice(profiles: list[dict[str, Any] | None]) -> str | None:
    """Warns when the compared shots did not share the same targets.

    Without this note one easily reads differences as a consequence of the
    settings when they come from the profile.
    """
    known = [p for p in profiles if p]
    if not known:
        return None
    # Missing profiles first: otherwise the two-profile guard below swallows
    # exactly the case where only one of them is archived.
    if len(known) != len(profiles):
        return ("At least one shot has no profile archived - the comparison of "
                "targets is incomplete.")
    if len(known) < 2:
        return None

    versions = {p["version_hash"] for p in known}
    if len(versions) == 1:
        return None

    semantics = {p["semantic_hash"] for p in known}
    if len(semantics) == 1:
        return ("The shots ran on different profile versions that brew "
                "identically (same semantic_hash) - the difference is purely "
                "cosmetic.")
    return ("Careful: the shots ran on profiles with different targets "
            "(differing semantic_hash). Differences in the metrics may come "
            "from the profile, not from grind setting or dose.")


#: Fields whose difference is worth comparing.
_DELTA_FIELDS = (
    "dose_g", "yield_g", "duration_s", "ratio", "pi_end", "t_first_drops",
    "peak_pressure_infusion", "max_pressure_global", "end_pressure",
    "pressure_dip_after_peak", "avg_flow_pour", "flow_stability",
    "pressure_trend_pour", "temp_basket_mean",
)


def _delta(reference: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _DELTA_FIELDS:
        left, right = reference.get(key), other.get(key)
        if isinstance(left, int | float) and isinstance(right, int | float):
            values[key] = round(right - left, 3)
    return {"id": other["id"], "vs_reference": values}


# ------------------------------------------------------------------ Parameter

_RELATIVE = re.compile(r"^\s*(\d+)\s*([hdwmy])\s*$", re.IGNORECASE)
_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30, "y": 24 * 365}


def _parse_time(value: str | None, label: str) -> str | None:
    """ISO8601 or a relative shorthand (``7d``, ``12h``, ``2w``, ``1m``, ``1y``)."""
    if not value:
        return None
    text = value.strip()

    match = _RELATIVE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        moment = datetime.now(UTC) - timedelta(hours=amount * _UNIT_HOURS[unit])
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            f"invalid_argument: {label}={value!r} is neither ISO8601 "
            "(2026-07-31 or 2026-07-31T19:00:00Z) nor a shorthand such as 7d, "
            "12h, 2w."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _encode_cursor(offset: int) -> str:
    return f"o{offset}"


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor.lstrip("o")))
    except ValueError as exc:
        raise ToolError(
            f"invalid_argument: cursor={cursor!r} did not come from an earlier "
            "response. Without a cursor the list starts from the beginning."
        ) from exc


# --------------------------------------------------------------------- status


def _status_payload(config: Config, db: Database) -> dict[str, Any]:
    oldest, newest = db.shot_span()
    last_sync = db.get_state(STATE_LAST_SYNC)
    warnings: list[str] = []

    if not db.get_state(STATE_BACKFILL_DONE):
        warnings.append("The backfill has not completed in full yet.")

    age_days = _age_days(last_sync)
    if age_days is None:
        warnings.append("There has not been a sync run yet.")
    elif age_days > STALE_SYNC_WARN_DAYS:
        warnings.append(
            f"Last sync {age_days:.0f} days ago - the tablet has not been "
            "reachable for that long. Newer shots are missing from the archive."
        )
    if config.sync_interval_min == 0:
        warnings.append("Automatic sync is switched off (SYNC_INTERVAL_MIN=0).")

    missing_profiles = len(db.shot_ids_without_profile())
    if missing_profiles:
        warnings.append(f"{missing_profiles} shots without a profile version.")

    last_reachable = db.get_state(STATE_LAST_REACHABLE)
    decaid_version = db.get_state(STATE_DECAID_VERSION) or None
    if decaid_version and decaid_version != VERIFIED_DECAID_VERSION:
        # Not an error, but the reason an assumption about the API could
        # suddenly stop holding.
        warnings.append(
            f"Decaid is running version {decaid_version}, verified is "
            f"{VERIFIED_DECAID_VERSION} - deviations in API behaviour are "
            "possible."
        )

    last_result = db.get_json_state(STATE_LAST_RESULT, {}) or {}
    if last_result.get("waiting_for_tablet"):
        # Not an error, just a fact - the tablet only runs while coffee is
        # being made.
        warnings.append(
            "Tablet was not reachable on the last attempt"
            + (f" (last reached: {last_reachable})" if last_reachable else "")
            + "."
        )

    errors = db.get_json_state("last_errors", []) or []
    return {
        "server": SERVER_NAME,
        "decaid": {
            "url": config.decaid_url,
            "version": decaid_version,
            "verified_version": VERIFIED_DECAID_VERSION,
            "last_reachable": last_reachable,
            "waiting_for_tablet": bool(last_result.get("waiting_for_tablet")),
        },
        "guards": {
            "active_rules": list(config.guard_rules),
            "findings": last_result.get("findings", 0),
            "notifications": "on" if config.ntfy_url else "off",
        },
        "version": __version__,
        "build_ref": os.environ.get("BUILD_REF") or None,
        "shots": db.count_shots(),
        "series_points": db.count_series_points(),
        "profile_versions": db.count_profiles(),
        "shots_without_profile": missing_profiles,
        "shots_with_metrics": db.count_metrics(METRICS_VERSION),
        "metrics_version": METRICS_VERSION,
        "oldest_shot": oldest,
        "newest_shot": newest,
        "last_sync": last_sync,
        "last_sync_result": db.get_json_state(STATE_LAST_RESULT),
        "sync_interval_min": config.sync_interval_min,
        "display_timezone": config.display_tz,
        "recent_errors": errors[-5:],
        "warnings": warnings,
    }


def _age_days(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        parsed = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - parsed).total_seconds() / 86400


# ------------------------------------------------------------------- ASGI-App


async def _empty_404(request: Request, exc: Exception) -> Response:
    """404 without a body (SPEC §12): a scanner should not even recognise Starlette."""
    status_code = getattr(exc, "status_code", 404)
    if status_code == 404:
        return Response(status_code=404)
    detail = getattr(exc, "detail", None)
    return PlainTextResponse(str(detail or ""), status_code=status_code)


def build_app(
    config: Config,
    *,
    db: Database | None = None,
    enable_sync: bool | None = None,
):
    """The finished ASGI app: MCP under ``/<secret>/mcp``, ``/healthz``, else 404.

    ``enable_sync`` controls the background loop *and* the Decaid connection;
    without it both run when ``SYNC_INTERVAL_MIN > 0``. Tests set it to False
    and thereby get by without a network.
    """
    database = db or open_database(config.db_path)
    sync_on = config.sync_interval_min > 0 if enable_sync is None else enable_sync

    coordinator = None
    if sync_on:
        coordinator = SyncCoordinator(DecaidClient(config.decaid_url), database)

    mcp = build_mcp(config, database, coordinator)
    app = mcp.http_app(path=config.mcp_path, transport="http")
    app.add_exception_handler(HTTPException, _empty_404)
    app.add_exception_handler(404, _empty_404)

    if coordinator is not None:
        _attach_sync_lifespan(app, config, coordinator)
    return app


def _attach_sync_lifespan(app, config: Config, coordinator: SyncCoordinator) -> None:
    """Attaches the sync worker to the lifecycle of the already built app."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope) -> AsyncGenerator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            periodic_sync(coordinator, config.sync_interval_min, stop=stop),
            name="decaid-sync",
        )
        log.info("sync worker started",
                 extra={"fields": {"interval_min": config.sync_interval_min}})
        try:
            async with inner(scope):
                yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await coordinator.aclose()
            log.info("sync worker stopped")

    app.router.lifespan_context = lifespan
