#!/usr/bin/env python
"""Einmalige Uebertragung der Visualizer-Annotationen nach Decaid (SPEC ss20.1).

Vor dem Historien-Reset: Notizen und Bewertungen, die waehrend der
Visualizer-Aera entstanden sind (viele davon per Chat gesetzt), sollen nicht
verloren gehen. Sie liegen in der alten SQLite-Datei; Decaid hat dieselben
Bezuege, aber ohne diese Felder.

Bewusst ein Skript und kein MCP-Tool: es laeuft genau einmal, schreibt in
fremde Datensaetze und soll nicht versehentlich aus einem Gespraech heraus
ausloesbar sein.

    python scripts/migrate_visualizer_annotations.py --db /data/shots-visualizer-era.db
    python scripts/migrate_visualizer_annotations.py --db ... --apply

Ohne ``--apply`` wird nichts geschrieben, nur berichtet.

ZUORDNUNG - drei Wege, in dieser Reihenfolge (am 2026-09-14 gegen die echten
Daten geprueft, 29 von 29 eindeutig):

1. ``annotations.extras.visualizerId`` - eindeutig, aber nur dort vorhanden, wo
   **Decaid selbst** nach Visualizer hochgeladen hat. Das begann erst am
   2026-08-30; fuer die davor liegenden Bezuege gibt es das Feld nicht, weil
   damals die de1app hochgeladen hat. Allein damit waere 1 von 29 Annotationen
   uebertragbar gewesen.
2. ``de1app-<unix>``-Kennungen - aus der de1app importierte Bezuege tragen den
   Startzeitpunkt als Unixzeit in der ID. Deterministisch und exakt.
3. Zeitstempel mit Toleranz, und nur wenn genau ein Kandidat passt. Decaid legt
   importierte Bezuege in UTC ab, selbst aufgezeichnete in Ortszeit - deshalb
   werden beide Lesarten geprueft.

GESCHRIEBEN WIRD NUR IN LEERE FELDER. Was in Decaid schon steht, gilt; die
alte Datei ist die Ergaenzung, nicht die Wahrheit.
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

#: Decaid legt aus der de1app importierte Bezuege in UTC ab, selbst
#: aufgezeichnete in Ortszeit. Beide Lesarten werden geprueft.
LOCAL_OFFSETS_H = (0, 1, 2)

#: Toleranz fuer die Zeitzuordnung. Bezuege liegen Minuten auseinander; 90 s
#: sind eng genug fuer Eindeutigkeit und weit genug fuer Rundungen.
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
    """Visualizer hat Notizen als HTML abgelegt, Decaid nimmt Klartext.

    ``<p>Cappuccino</p>`` wuerde in Decaid woertlich mit den Klammern
    erscheinen - deshalb Tags entfernen und Entities aufloesen.
    """
    text = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = _TAG.sub("", text)
    return html.unescape(text).strip()


def _unrated(value: Any) -> bool:
    """Gilt eine Bewertung als leer?

    Decaid legt importierte Bezuege mit enjoyment: 0.0 an, selbst
    aufgezeichnete mit null. Ueber alle 168 Bezuege gemessen: 88-mal 0.0,
    69-mal null, und echte Bewertungen liegen bei 40 bis 100. Eine Null ist
    also "nicht bewertet", kein Urteil - dieselbe Lesart wie bei Visualizer
    (SPEC ss5). Ohne diese Regel bliebe jede uebertragene Bewertung liegen.
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
    """Die drei Wege aus dem Modul-Docstring, in ihrer Reihenfolge."""
    for shot in shots:
        extras = ((shot.get("annotations") or {}).get("extras") or {})
        if extras.get("visualizerId") == row["id"]:
            return shot, "visualizerId"

    started = _naive(row["started_at"])
    if started is None:
        return None, "unlesbarer Zeitstempel"

    # UTC ausdruecklich setzen: .timestamp() deutet einen naiven Zeitstempel
    # sonst als Ortszeit, und der Versatz sprengt jede Toleranz.
    epoch = int(started.replace(tzinfo=UTC).timestamp())
    for shot in shots:
        hit = _DE1APP_ID.match(str(shot.get("id", "")))
        if hit and abs(int(hit.group(1)) - epoch) <= MATCH_TOLERANCE.total_seconds():
            return shot, "de1app-Kennung"

    candidates = []
    for shot in shots:
        stamp = _naive(str(shot.get("timestamp", "")))
        if stamp is None:
            continue
        if any(abs(stamp - (started + timedelta(hours=h))) <= MATCH_TOLERANCE
               for h in LOCAL_OFFSETS_H):
            candidates.append(shot)
    if len(candidates) == 1:
        return candidates[0], "Zeitstempel"
    if candidates:
        return None, f"mehrdeutig ({len(candidates)} Kandidaten)"
    return None, "kein Kandidat"


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
                plan.skipped["espressoNotes"] = "in Decaid bereits belegt"
            else:
                plan.fields["espressoNotes"] = note

        if row["enjoyment"] is not None:
            if not _unrated(ann.get("enjoyment")):
                plan.skipped["enjoyment"] = "in Decaid bereits belegt"
            else:
                # T18: Decaid nutzt dieselbe Skala 0-100, kein Mapping noetig.
                plan.fields["enjoyment"] = float(row["enjoyment"])

        plans.append(plan)
    return plans, unmatched


def apply_plan(client: httpx.Client, plan: Plan, pause: float) -> tuple[bool, str]:
    """Schreiben und danach frisch nachlesen - der 200 allein belegt nichts."""
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
        return False, "; ".join(f"{k}: erwartet {v[0]!r}, gelesen {v[1]!r}"
                                for k, v in wrong.items())
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True,
                        help="alte SQLite-Datei, z. B. /data/shots-visualizer-era.db")
    parser.add_argument("--decaid", default="http://10.100.100.171:8080")
    parser.add_argument("--apply", action="store_true",
                        help="ohne diese Angabe wird nichts geschrieben")
    parser.add_argument("--pause", type=float, default=0.4,
                        help="Wartezeit zwischen Schreiben und Nachlesen")
    args = parser.parse_args(argv)

    rows = load_old(args.db)
    print(f"Alte Datei: {len(rows)} Bezuege mit Annotationen")

    with httpx.Client(base_url=args.decaid.rstrip("/"), timeout=90,
                      headers={"Accept": "application/json"}) as client:
        shots = load_decaid(client)
        print(f"Decaid:     {len(shots)} Bezuege\n")

        plans, unmatched = build_plan(rows, shots)

        by_how: dict[str, int] = {}
        to_write = 0
        for plan in plans:
            by_how[plan.how] = by_how.get(plan.how, 0) + 1
            to_write += len(plan.fields)

        print("Zuordnung:", ", ".join(f"{k}: {v}" for k, v in sorted(by_how.items())) or "keine")
        if unmatched:
            print(f"Ohne Zuordnung ({len(unmatched)}):")
            for line in unmatched:
                print(f"  {line}")

        print(f"\nZu uebertragende Felder: {to_write}")
        for plan in plans:
            if not plan.fields and not plan.skipped:
                continue
            marks = " ".join(f"{k}={_short(v)}" for k, v in plan.fields.items())
            skips = " ".join(f"{k}({v})" for k, v in plan.skipped.items())
            print(f"  {plan.old_id[:8]} -> {plan.decaid_id[:16]:16} [{plan.how}]"
                  f"  {marks}{'  uebersprungen: ' + skips if skips else ''}")

        if not args.apply:
            print("\nProbelauf - nichts geschrieben. Mit --apply ausfuehren.")
            return 0

        print("\nSchreiben:")
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

        print(f"\nUebertragen: {ok}, fehlgeschlagen: {failed}")
        return 1 if failed else 0


def _short(value: Any, limit: int = 42) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    sys.exit(main())
