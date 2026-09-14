#!/usr/bin/env python
"""One-off transfer of the Visualizer annotations into Decaid (SPEC §20.1).

Ahead of the history reset: notes and ratings that came about during the
Visualizer era (many of them set through chat) must not be lost. They sit in the
old SQLite file; Decaid holds the same shots but without these fields.

Deliberately a script and not an MCP tool: it runs exactly once, writes into
someone else's records and must not be triggerable by accident from a
conversation.

    python scripts/migrate_visualizer_annotations.py --db /data/shots-visualizer-era.db
    python scripts/migrate_visualizer_annotations.py --db ... --apply

Without ``--apply`` nothing is written, only reported.

MATCHING - three routes, in this order (checked against the real data on
2026-09-14, 29 out of 29 unambiguous):

1. ``annotations.extras.visualizerId`` - unambiguous, but only present where
   **Decaid itself** uploaded to Visualizer. That only started on 2026-08-30;
   for the shots before it the field does not exist, because back then the
   de1app did the uploading. On its own that would have carried 1 of 29
   annotations.
2. ``de1app-<unix>`` identifiers - shots imported from the de1app carry their
   start time as Unix time in the ID. Deterministic and exact.
3. Timestamps with a tolerance, and only when exactly one candidate fits. Decaid
   stores imported shots in UTC and natively recorded ones in local time, so
   both readings are tried.

ONLY EMPTY FIELDS ARE WRITTEN. Whatever already stands in Decaid holds; the old
file is the supplement, not the truth.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

#: Decaid stores shots imported from the de1app in UTC and natively recorded
#: ones in local time. Both readings are tried.
LOCAL_OFFSETS_H = (0, 1, 2)

#: Tolerance for the time-based matching. Shots are minutes apart; 90 s is
#: tight enough to stay unambiguous and loose enough for rounding.
MATCH_TOLERANCE = timedelta(seconds=90)

_DE1APP_ID = re.compile(r"^de1app-(\d{9,13})$")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Plan:
    old_id: str
    started_at: str
    decaid_id: str
    how: str
    fields: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


def clean_note(raw: str) -> str:
    """Visualizer stored notes as HTML, Decaid takes plain text.

    ``<p>Cappuccino</p>`` would show up in Decaid with the brackets intact -
    hence strip the tags and resolve the entities.
    """
    text = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = _TAG.sub("", text)
    return html.unescape(text).strip()


def _unrated(value: Any) -> bool:
    """Does a rating count as empty?

    Decaid creates imported shots with enjoyment: 0.0 and natively recorded
    ones with null. Measured across all 168 shots: 88 times 0.0, 69 times null,
    and real ratings run from 40 to 100. A zero there means "not rated", not a
    judgement - the same reading as with Visualizer (SPEC §5). Without this
    rule every transferred rating would stay behind.
    """
    return value is None or value == 0


def _naive(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def load_old(db_path: str) -> list[sqlite3.Row]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        return list(db.execute(
            "SELECT id, started_at, notes, enjoyment FROM shots "
            "WHERE (notes IS NOT NULL AND trim(notes) <> '') OR enjoyment IS NOT NULL "
            "ORDER BY started_at"
        ))
    finally:
        db.close()


def load_decaid(client: httpx.Client) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    offset, total = 0, None
    while total is None or offset < total:
        page = client.get("/api/v1/shots",
                          params={"limit": 100, "offset": offset}).json()
        total = page["total"]
        batch = page.get("items") or []
        if not batch:
            break
        shots.extend(batch)
        offset += len(batch)
    return shots


def find_match(row: sqlite3.Row, shots: list[dict[str, Any]]) -> tuple[dict | None, str]:
    """The three routes from the module docstring, in that order."""
    for shot in shots:
        extras = ((shot.get("annotations") or {}).get("extras") or {})
        if extras.get("visualizerId") == row["id"]:
            return shot, "visualizerId"

    started = _naive(row["started_at"])
    if started is None:
        return None, "unreadable timestamp"

    # Set UTC explicitly: .timestamp() otherwise reads a naive timestamp as
    # local time, and the offset blows past any tolerance.
    epoch = int(started.replace(tzinfo=UTC).timestamp())
    for shot in shots:
        hit = _DE1APP_ID.match(str(shot.get("id", "")))
        if hit and abs(int(hit.group(1)) - epoch) <= MATCH_TOLERANCE.total_seconds():
            return shot, "de1app identifier"

    candidates = []
    for shot in shots:
        stamp = _naive(str(shot.get("timestamp", "")))
        if stamp is None:
            continue
        if any(abs(stamp - (started + timedelta(hours=h))) <= MATCH_TOLERANCE
               for h in LOCAL_OFFSETS_H):
            candidates.append(shot)
    if len(candidates) == 1:
        return candidates[0], "timestamp"
    if candidates:
        return None, f"ambiguous ({len(candidates)} candidates)"
    return None, "no candidate"


def build_plan(
    rows: list[sqlite3.Row], shots: list[dict[str, Any]]
) -> tuple[list[Plan], list[str]]:
    plans, unmatched = [], []
    for row in rows:
        shot, how = find_match(row, shots)
        if shot is None:
            unmatched.append(f"{row['id'][:8]} {row['started_at'][:19]}: {how}")
            continue

        ann = shot.get("annotations") or {}
        plan = Plan(row["id"], row["started_at"], shot["id"], how)

        note = clean_note(row["notes"] or "")
        if note:
            if str(ann.get("espressoNotes") or "").strip():
                plan.skipped["espressoNotes"] = "already set in Decaid"
            else:
                plan.fields["espressoNotes"] = note

        if row["enjoyment"] is not None:
            if not _unrated(ann.get("enjoyment")):
                plan.skipped["enjoyment"] = "already set in Decaid"
            else:
                # T18: Decaid uses the same 0-100 scale, no mapping needed.
                plan.fields["enjoyment"] = float(row["enjoyment"])

        plans.append(plan)
    return plans, unmatched


def apply_plan(client: httpx.Client, plan: Plan, pause: float) -> tuple[bool, str]:
    """Write, then read back fresh - a 200 on its own proves nothing."""
    client.put(f"/api/v1/shots/{plan.decaid_id}",
               json={"annotations": dict(plan.fields)})
    time.sleep(pause)
    after = client.get(f"/api/v1/shots/{plan.decaid_id}").json()
    ann = after.get("annotations") or {}

    wrong = {
        name: (value, ann.get(name))
        for name, value in plan.fields.items()
        if ann.get(name) != value
    }
    if wrong:
        return False, "; ".join(f"{k}: expected {v[0]!r}, read {v[1]!r}"
                                for k, v in wrong.items())
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True,
                        help="the old SQLite file, e.g. /data/shots-visualizer-era.db")
    parser.add_argument("--decaid", default="http://10.100.100.171:8080")
    parser.add_argument("--apply", action="store_true",
                        help="without this nothing is written")
    parser.add_argument("--pause", type=float, default=0.4,
                        help="wait between writing and reading back")
    args = parser.parse_args(argv)

    rows = load_old(args.db)
    print(f"Old file:   {len(rows)} shots carrying annotations")

    with httpx.Client(base_url=args.decaid.rstrip("/"), timeout=90,
                      headers={"Accept": "application/json"}) as client:
        shots = load_decaid(client)
        print(f"Decaid:     {len(shots)} shots\n")

        plans, unmatched = build_plan(rows, shots)

        by_how: dict[str, int] = {}
        to_write = 0
        for plan in plans:
            by_how[plan.how] = by_how.get(plan.how, 0) + 1
            to_write += len(plan.fields)

        print("Matched by:", ", ".join(f"{k}: {v}" for k, v in sorted(by_how.items()))
          or "nothing")
        if unmatched:
            print(f"Unmatched ({len(unmatched)}):")
            for line in unmatched:
                print(f"  {line}")

        print(f"\nFields to transfer: {to_write}")
        for plan in plans:
            if not plan.fields and not plan.skipped:
                continue
            marks = " ".join(f"{k}={_short(v)}" for k, v in plan.fields.items())
            skips = " ".join(f"{k}({v})" for k, v in plan.skipped.items())
            print(f"  {plan.old_id[:8]} -> {plan.decaid_id[:16]:16} [{plan.how}]"
                  f"  {marks}{'  skipped: ' + skips if skips else ''}")

        if not args.apply:
            print("\nDry run - nothing written. Use --apply to do it.")
            return 0

        print("\nWriting:")
        ok = failed = 0
        for plan in plans:
            if not plan.fields:
                continue
            try:
                good, detail = apply_plan(client, plan, args.pause)
            except httpx.HTTPError as exc:
                good, detail = False, f"{type(exc).__name__}"
            if good:
                ok += 1
                print(f"  [ok ] {plan.decaid_id[:16]:16} {', '.join(plan.fields)}")
            else:
                failed += 1
                print(f"  [!! ] {plan.decaid_id[:16]:16} {detail}")

        print(f"\nTransferred: {ok}, failed: {failed}")
        return 1 if failed else 0


def _short(value: Any, limit: int = 42) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
