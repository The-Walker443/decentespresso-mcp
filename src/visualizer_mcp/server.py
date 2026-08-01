"""FastMCP-App: Streamable-HTTP-Endpoint unter dem Secret-Pfad (SPEC ss9, ss10).

Die Docstrings der Tools sind kein Beiwerk: Claude liest sie und muss daraus
ohne Rueckfrage ableiten, was eine Zahl bedeutet. Einheiten, die Semantik von
`pi_end`, der Unterschied der beiden Druckmaxima und die Bedeutung von
`warnings` stehen deshalb im Server-Prompt (immer im Kontext) und zusaetzlich
verkuerzt in jedem Tool, das die betroffenen Felder liefert.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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
from .metrics import METRICS_VERSION, downsample_curve, metrics_for_shot
from .sync import (
    QUICK_SYNC_MAX_AGE_S,
    STATE_BACKFILL_DONE,
    STATE_LAST_RESULT,
    STATE_LAST_SYNC,
    SyncCoordinator,
    open_database,
    periodic_sync,
)
from .visualizer_client import VisualizerClient, VisualizerError

log = logging.getLogger(__name__)

SERVER_NAME = "visualizer-espresso"

#: SPEC ss12: status() warnt, wenn die Archivluecke gefaehrlich wird - Visualizer
#: Free haelt nur ein 1-Monats-Fenster vor.
STALE_SYNC_WARN_DAYS = 7

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
DEFAULT_MAX_POINTS = 120
COMPARE_MAX_POINTS = 60
NOTES_PREVIEW_CHARS = 160

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

INSTRUCTIONS = """\
Lokales Archiv der Espresso-Bezuege einer Decent DE1. Quelle ist
visualizer.coffee; dieser Server ist die vollstaendige Historie und die
Grundlage fuer Analysen.

EINHEITEN (durchgaengig, nie mitgeliefert): Druck bar, Fluss ml/s,
Gewicht und Dosis g, Temperatur Grad Celsius, Zeit s. Zeitstempel sind ISO8601
in UTC; `elapsed`/`t` zaehlt ab Shot-Beginn.

BEGRIFFE, die in den Metriken auftauchen:

- `pi_end` - Ende der Praeinfusion in Sekunden. Das ist ein von der Maschine
  selbst gemeldeter Phasenwechsel (Kanal `espresso_state_change`), kein
  Schwellwert und keine Schaetzung. `pi_end_source` sagt, woher der Wert kommt:
  `state_change` = echte Maschinenmarke; `heuristic` = der Shot hatte keine
  Marken, ersatzweise wurde der Zeitpunkt genommen, an dem der Druck erstmals
  60 % des Maximums erreicht. Bei `heuristic` ist der Wert eine Naeherung und
  taugt nicht fuer Vergleiche auf die Zehntelsekunde.

- Zwei Druckmaxima, absichtlich getrennt:
  `peak_pressure_infusion` ist das Maximum bis kurz nach der Praeinfusion
  (Fenster `[0, pi_end + 2 s]`). Das ist der Druck, der den Puck aufbaut - die
  Zahl, die beim Einstellen interessiert.
  `max_pressure_global` ist das Maximum ueber den ganzen Bezug. Bei Profilen
  mit ansteigendem Druck (z. B. D-Flow) liegt es auf dem letzten Messpunkt und
  sagt ueber den Puckaufbau nichts aus. Die beiden Werte nicht verwechseln und
  nicht gegeneinander als "Anstieg" interpretieren.

- `warnings` - eine nicht leere Liste heisst: fuer diesen Shot ist mindestens
  eine Metrik unzuverlaessig. Das betroffene Feld ist dann `null`, nicht etwa
  0. Haeufigste Ursache ist die Waage (nicht tariert, angestossen, nicht
  verbunden). Auf `null` gesetzte Felder nicht raten und nicht ueberlesen -
  die Warnung im Klartext an den Nutzer weitergeben, wenn sie die Frage
  betrifft.

- `null` bedeutet durchgaengig "nicht erfasst", nie "Wert ist 0". Das gilt auch
  fuer `drink_tds`, `drink_ey` und `enjoyment`.

Kurven kommen als parallele Arrays (`t`, `p`, `fi`, `fo`, `w`, `tb`), nicht als
Objektliste, und sind ausgeduennt. Fehlt ein Kanal, gab es dafuer keinen
einzigen Messwert.\
"""


# --------------------------------------------------------------------- Tools


def build_mcp(
    config: Config, db: Database, coordinator: SyncCoordinator | None = None
) -> FastMCP:
    """Baut die FastMCP-Instanz.

    ``coordinator`` ist optional: ohne ihn arbeiten alle lesenden Tools normal
    weiter, nur ``sync_now`` und der Frische-Check von ``get_shot("latest")``
    entfallen. Tests nutzen das, um ohne Netz auszukommen.
    """
    mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations=READ_ONLY)
    async def list_beans() -> dict[str, Any]:
        """Alle Bohnen im Archiv mit Bezugszahl, Zeitraum und Muehleneinstellung.

        Einstieg fuer jede Frage der Form "was habe ich zuletzt getrunken" oder
        "welche Bohnen gibt es". Einheiten: Zeitstempel ISO8601 UTC.
        `grinder_setting` ist Freitext der Muehle (z. B. "4,2") und laesst sich
        nur innerhalb derselben Muehle vergleichen.
        """
        rows = await asyncio.to_thread(db.list_beans)
        return {
            "beans": [
                {
                    "brand": r["bean_brand"],
                    "type": r["bean_type"],
                    "shot_count": r["shot_count"],
                    "first_shot": r["first_shot"],
                    "last_shot": r["last_shot"],
                    "last_grinder_model": r["last_grinder_model"],
                    "last_grinder_setting": r["last_grinder_setting"],
                    "grinder_settings_used": r["grinder_settings"],
                }
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
        """Kompakte Liste der Bezuege, neueste zuerst.

        Filter sind Teilstrings, Gross-/Kleinschreibung egal: `bean` trifft
        Marke oder Sorte, `roaster` nur die Marke, `profile` den Profilnamen.
        `since`/`until` nehmen ISO8601 (`2026-07-31` oder
        `2026-07-31T19:00:00Z`) oder relative Kuerzel: `7d`, `12h`, `2w`, `1m`,
        `1y` - jeweils "innerhalb der letzten ...".

        Einheiten: `dose`/`yield` in g, `duration` in s, Druck in bar.
        `peak_pressure_infusion` ist das Druckmaximum bis kurz nach der
        Praeinfusion, nicht das Maximum des ganzen Bezugs (dafuer `get_shot`).
        `null` heisst nicht erfasst. Ist `warnings` > 0, sind fuer diesen Shot
        Metriken unzuverlaessig - Details liefert `get_shot_metrics`.

        Bei mehr Treffern als `limit` kommt `next_cursor` zurueck; diesen Wert
        unveraendert als `cursor` erneut schicken.

        Auf der ersten Seite (ohne `cursor`) prueft der Server vorher auf
        Frische und gleicht ab, wenn der letzte Sync mehr als zwei Minuten her
        ist; das Ergebnis steht in `freshness`. Beim Blaettern unterbleibt das
        bewusst - ein Abgleich mitten in der Paginierung koennte die
        Treffermenge unter dem Cursor verschieben.
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
        include_curve: bool = True,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> dict[str, Any]:
        """Ein Bezug vollstaendig: Metadaten, Metriken, Kurve, Profil-Kurzfassung.

        `id` ist eine Shot-UUID oder `"latest"` fuer den neuesten Bezug;
        zusammen mit `bean` der neueste Bezug dieser Bohne. Bei `"latest"`
        prueft der Server vorher auf Frische und synchronisiert, wenn der letzte
        Abgleich mehr als zwei Minuten her ist - ein eben gezogener Bezug ist
        damit sofort da. Ob das passiert ist, steht in `freshness`.

        Einheiten: Druck bar, Fluss ml/s, Gewicht g, Temperatur Grad Celsius,
        Zeit s.

        Zu den Metriken:
        - `pi_end` ist das Ende der Praeinfusion und stammt aus einer
          Phasenmarke der Maschine; `pi_end_source` unterscheidet
          `state_change` (echte Marke) von `heuristic` (Naeherung ueber 60 %
          des Druckmaximums, nur wenn der Shot keine Marken hatte).
        - `peak_pressure_infusion` ist das Druckmaximum im Fenster
          `[0, pi_end + 2 s]` und beschreibt den Puckaufbau.
          `max_pressure_global` ist das Maximum des ganzen Bezugs; bei
          ansteigenden Profilen liegt es am Schluss und sagt ueber den
          Puckaufbau nichts aus.
        - Eine nicht leere `warnings`-Liste heisst: die dort genannten Felder
          sind `null`, weil die Grundlage fehlte (meist die Waage). Nicht als 0
          lesen und nicht ueberlesen.

        Die Kurve kommt als parallele Arrays: `t` Zeit, `p` Druck, `fi`
        Pumpenfluss, `fo` Fluss aus der Waage, `w` Gewicht, `tb`
        Korbtemperatur. Sie ist auf `max_points` ausgeduennt (Standard 120,
        Maximum 400); erster und letzter Punkt sowie beide Druckmaxima sind
        immer enthalten. `include_curve=false` spart Platz, wenn nur die Zahlen
        gebraucht werden.
        """
        freshness = None
        if id == "latest" and coordinator is not None:
            freshness = await _refresh(coordinator)

        shot_id = id
        if id == "latest":
            shot_id = await asyncio.to_thread(db.latest_shot_id, bean)
            if shot_id is None:
                raise ToolError(
                    "shot_not_found: Es gibt keinen archivierten Bezug"
                    + (f" fuer die Bohne {bean!r}." if bean else ".")
                )

        row = await asyncio.to_thread(db.get_shot_row, shot_id)
        if row is None:
            raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {shot_id!r}.")

        metrics = await asyncio.to_thread(metrics_for_shot, db, shot_id)
        payload: dict[str, Any] = {
            "shot": _full_shot(row),
            "metrics": _public_metrics(metrics),
            "profile": await asyncio.to_thread(_profile_summary, db, row["profile_id"]),
        }
        if freshness is not None:
            payload["freshness"] = freshness
        if include_curve:
            payload["curve"] = await asyncio.to_thread(
                _curve, db, shot_id, metrics, max_points
            )
        return payload

    @mcp.tool(annotations=READ_ONLY)
    async def get_shot_metrics(id: str) -> dict[str, Any]:
        """Nur die abgeleiteten Metriken eines Bezugs, ohne Kurve und Profil.

        Einheiten: Druck bar, Fluss ml/s, Temperatur Grad Celsius, Zeit s.

        `pi_end` ist ein von der Maschine gemeldeter Phasenwechsel (Ende der
        Praeinfusion); `pi_end_source` = `state_change` bedeutet echte Marke,
        `heuristic` eine Naeherung ueber 60 % des Druckmaximums fuer Shots ohne
        Marken. `peak_pressure_infusion` betrifft nur den Puckaufbau bis kurz
        nach der Praeinfusion, `max_pressure_global` den ganzen Bezug - beide
        sind getrennt, weil sie bei ansteigenden Profilen weit auseinander
        liegen. Steht etwas in `warnings`, ist das jeweilige Feld `null`, weil
        die Messgrundlage fehlte; das ist kein Nullwert.
        """
        metrics = await asyncio.to_thread(metrics_for_shot, db, id)
        if metrics is None:
            raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {id!r}.")
        return {"id": id, **_public_metrics(metrics)}

    @mcp.tool(annotations=READ_ONLY)
    async def compare_shots(
        ids: list[str], include_curves: bool = False
    ) -> dict[str, Any]:
        """Zwei bis vier Bezuege nebeneinander, mit Differenz zum ersten.

        Der erste Eintrag in `ids` ist die Bezugsgroesse; `deltas` enthaelt je
        weiterem Shot die Differenz zu ihm (positiv = groesser als der erste).
        Einheiten wie ueberall: Druck bar, Fluss ml/s, Zeit s, Gewicht g.

        Vorsicht bei zwei Stellen: `pi_end` ist nur dann auf die
        Zehntelsekunde vergleichbar, wenn bei allen Shots
        `pi_end_source == "state_change"` steht - sonst mischen sich
        Maschinenmarke und Naeherung. Und `peak_pressure_infusion` ist die
        Groesse fuer den Puckaufbau; `max_pressure_global` kann bei
        ansteigenden Profilen allein vom Profilverlauf abweichen. Felder, die
        bei einem Shot `null` sind, tauchen in `deltas` nicht auf - der Grund
        steht in dessen `warnings`.

        `include_curves=true` haengt je Shot eine auf 60 Punkte ausgeduennte
        Kurve an.
        """
        if not 2 <= len(ids) <= 4:
            raise ToolError(
                f"invalid_argument: compare_shots braucht 2 bis 4 Kennungen, "
                f"bekommen hat es {len(ids)}."
            )

        entries: list[dict[str, Any]] = []
        for shot_id in ids:
            row = await asyncio.to_thread(db.get_shot_row, shot_id)
            if row is None:
                raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {shot_id!r}.")
            metrics = await asyncio.to_thread(metrics_for_shot, db, shot_id)
            entry: dict[str, Any] = {
                "id": shot_id,
                "started_at": row["started_at"],
                "bean": _bean_label(row),
                "profile": row["profile_name"],
                "grinder_setting": row["grinder_setting"],
                "dose_g": row["dose_g"],
                "yield_g": row["yield_g"],
                **_public_metrics(metrics),
            }
            if include_curves:
                entry["curve"] = await asyncio.to_thread(
                    _curve, db, shot_id, metrics, COMPARE_MAX_POINTS
                )
            entries.append(entry)

        return {
            "reference": ids[0],
            "shots": entries,
            "deltas": [_delta(entries[0], other) for other in entries[1:]],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_profiles() -> dict[str, Any]:
        """Alle Profile mit ihren Versionen.

        Ein Profil bekommt eine neue Version, sobald sich seine Datei auf der
        Maschine aendert. `version_hash` (hier auf 8 Zeichen gekuerzt) ist die
        Identitaet einer Version - daran haengt jeder Bezug, der mit ihr lief.
        `semantic_hash` gruppiert daneben Versionen, die identisch bruehen und
        sich nur kosmetisch unterscheiden (Notiztext, Formatierung): gleicher
        `semantic_hash` heisst gleiche Sollwerte, auch bei verschiedenem
        `version_hash`. Zum Vergleichen von Bezuegen ist `semantic_hash` der
        richtige Schluessel.
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
        """Die Sollwerte eines Profils - genau eine Angabe machen.

        `shot_id` liefert die Version, mit der dieser Bezug tatsaechlich lief
        (auch wenn das Profil spaeter geaendert wurde). `version_hash` (Praefix
        genuegt) trifft eine bestimmte Version, `name` die zuletzt gesehene
        Version dieses Namens.

        Einheiten in den Schritten: `target` ist Druck in bar, wenn `mode` auf
        `pressure` steht, sonst Fluss in ml/s; `temp_c` Grad Celsius,
        `duration_s` Sekunden. `exit` ist die Abbruchbedingung des Schrittes
        und `null`, wenn keine aktiv ist - im TCL stehen dann zwar Werte, die
        Maschine wertet sie aber nicht aus.

        Bei Legacy-Profilen (`type` = `pressure` oder `flow`) ist `steps` leer:
        solche Profile beschreiben ihren Verlauf ueber Kopf-Sollwerte statt
        ueber Schritte. `parse_ok: false` heisst, die Profildatei war nicht
        lesbar; dann taugen nur `title` und `notes`.
        """
        given = [bool(shot_id), bool(name), bool(version_hash)]
        if sum(given) != 1:
            raise ToolError(
                "invalid_argument: genau eine Angabe erwartet - shot_id, name "
                "oder version_hash."
            )

        if shot_id:
            row = await asyncio.to_thread(db.get_shot_row, shot_id)
            if row is None:
                raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {shot_id!r}.")
            if row["profile_id"] is None:
                raise ToolError(
                    f"profile_not_found: Fuer den Bezug {shot_id!r} ist kein Profil "
                    "archiviert."
                )
            profile = await asyncio.to_thread(db.get_profile_row, row["profile_id"])
        else:
            profile = await asyncio.to_thread(
                db.find_profile, version_hash=version_hash, name=name
            )
        if profile is None:
            raise ToolError(
                "profile_not_found: Kein Profil zu "
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
        """Holt neue und geaenderte Bezuege sofort von visualizer.coffee.

        Die einzige Operation, die nach aussen geht; sie schreibt nichts zu
        Visualizer und ist beliebig wiederholbar. Normalerweise unnoetig - der
        Server synchronisiert selbst, und `get_shot("latest")` prueft ohnehin
        auf Frische.

        `errors` sind voruebergehende Probleme (Netz, Schreibfehler); solange
        welche auftreten, wiederholt der naechste Lauf dieselben Bezuege.
        `warnings` sind endgueltige Befunde (Bezug ohne Profil, unlesbare
        Profildatei) - die aendern sich durch Wiederholen nicht.
        """
        if coordinator is None:
            raise ToolError(
                "sync_unavailable: Dieser Server laeuft ohne Visualizer-Verbindung."
            )
        try:
            result = await coordinator.run(full=False)
        except VisualizerError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
        return result.as_dict()

    @mcp.tool(annotations=READ_ONLY)
    async def status() -> dict[str, Any]:
        """Zustand des Archivs: Bestand, letzter Sync, offene Warnungen.

        Zeiten sind ISO8601 in UTC. `shots` zaehlt die lokal archivierten
        Bezuege, `series_points` die gespeicherten Messpunkte. `warnings` meldet
        unter anderem, wenn der letzte Sync so lange her ist, dass Bezuege aus
        dem 1-Monats-Fenster des Visualizer-Free-Tiers gefallen sein koennten.
        """
        return await asyncio.to_thread(_status_payload, config, db)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        # Bewusst ohne Secret-Pfad: der Docker-Healthcheck kennt es nicht. Gibt
        # nichts preis, darf aber laut SPEC ss10.1 nicht ins Tunnel-Ingress.
        return PlainTextResponse("ok")

    return mcp


# ------------------------------------------------------------------ Aufbereitung


async def _refresh(coordinator: SyncCoordinator) -> dict[str, Any]:
    """Frische-Check fuer ``get_shot("latest")`` und die erste ``list_shots``-Seite.

    Ein Fehler bricht nichts ab: der Bestand ist da, nur vielleicht nicht
    taufrisch - das ist eine bessere Antwort als gar keine.
    """
    try:
        result = await coordinator.ensure_fresh(QUICK_SYNC_MAX_AGE_S)
    except VisualizerError as exc:
        # Der Bestand ist da, nur vielleicht nicht ganz aktuell - das ist eine
        # bessere Antwort als gar keine.
        return {"synced": False, "note": f"Sync fehlgeschlagen ({exc.code}), "
                                         "Antwort stammt aus dem Archiv."}
    if result is None:
        return {"synced": False, "note": "Bestand war aktuell, kein Abgleich noetig."}
    return {"synced": True, "new_shots": result.new_shots, "updated": result.updated}


def _bean_label(row: Any) -> str | None:
    parts = [row["bean_brand"], row["bean_type"]]
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
        "notes": _short(row["notes"] or row["private_notes"]),
        "warnings": len(metrics.get("warnings") or []),
    }


def _full_shot(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "bean_brand": row["bean_brand"],
        "bean_type": row["bean_type"],
        "bean_notes": _short(row["bean_notes"]),
        "grinder_model": row["grinder_model"],
        "grinder_setting": row["grinder_setting"],
        "dose_g": row["dose_g"],
        "yield_g": row["yield_g"],
        "ratio": row["ratio"],
        "duration_s": row["duration_s"],
        "drink_tds": row["drink_tds"],
        "drink_ey": row["drink_ey"],
        "enjoyment": row["enjoyment"],
        "notes": row["notes"],
        "private_notes": row["private_notes"],
    }


def _public_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Cache-Interna (``metrics_version``, ``n_points``) gehoeren nicht in die Antwort."""
    if not metrics:
        return {"warnings": ["Keine Metriken berechnet."]}
    return {k: v for k, v in metrics.items() if k not in ("metrics_version", "n_points")}


def _curve(
    db: Database, shot_id: str, metrics: dict[str, Any] | None, max_points: int
) -> dict[str, Any]:
    rows = [dict(r) for r in db.series_for_shot(shot_id)]
    metrics = metrics or {}
    return downsample_curve(
        rows,
        max_points=max_points,
        keep_times=(metrics.get("t_peak"), metrics.get("t_max_pressure_global")),
    )


def _profile_summary(db: Database, profile_id: int | None) -> dict[str, Any] | None:
    """Kurzfassung fuers Shot-Detail - das volle Profil liefert ``get_profile``."""
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


#: Felder, deren Differenz sich zu vergleichen lohnt.
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
    """ISO8601 oder relatives Kuerzel (``7d``, ``12h``, ``2w``, ``1m``, ``1y``)."""
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
            f"invalid_argument: {label}={value!r} ist weder ISO8601 "
            "(2026-07-31 oder 2026-07-31T19:00:00Z) noch ein Kuerzel wie 7d, 12h, 2w."
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
            f"invalid_argument: cursor={cursor!r} stammt nicht aus einer frueheren "
            "Antwort. Ohne cursor beginnt die Liste von vorn."
        ) from exc


# --------------------------------------------------------------------- status


def _status_payload(config: Config, db: Database) -> dict[str, Any]:
    oldest, newest = db.shot_span()
    last_sync = db.get_state(STATE_LAST_SYNC)
    warnings: list[str] = []

    if not db.get_state(STATE_BACKFILL_DONE):
        warnings.append("Backfill wurde noch nicht vollstaendig abgeschlossen.")

    age_days = _age_days(last_sync)
    if age_days is None:
        warnings.append("Es gab noch keinen Sync-Lauf.")
    elif age_days > STALE_SYNC_WARN_DAYS:
        warnings.append(
            f"Letzter Sync vor {age_days:.0f} Tagen - Visualizer Free haelt nur "
            "ein 1-Monats-Fenster vor, aeltere Bezuege koennen verloren sein."
        )
    if config.sync_interval_min == 0:
        warnings.append("Automatischer Sync ist abgeschaltet (SYNC_INTERVAL_MIN=0).")

    missing_profiles = len(db.shot_ids_without_profile())
    if missing_profiles:
        warnings.append(f"{missing_profiles} Shots ohne Profilversion.")

    errors = db.get_json_state("last_errors", []) or []
    return {
        "server": SERVER_NAME,
        "version": __version__,
        "milestone": "M4",
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
    """404 ohne Body (SPEC ss10.1): ein Scanner soll nicht mal Starlette erkennen."""
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
    """Fertige ASGI-App: MCP unter ``/<secret>/mcp``, ``/healthz``, sonst 404.

    ``enable_sync`` steuert Hintergrundschleife *und* Visualizer-Verbindung;
    ohne Angabe laeuft beides, wenn ``SYNC_INTERVAL_MIN > 0`` ist. Tests setzen
    es auf False und kommen damit ohne Netz aus.
    """
    database = db or open_database(config.db_path)
    sync_on = config.sync_interval_min > 0 if enable_sync is None else enable_sync

    coordinator = None
    if sync_on:
        coordinator = SyncCoordinator(
            VisualizerClient(
                config.visualizer_email,
                config.visualizer_password,
                user_agent=config.user_agent,
            ),
            database,
        )

    mcp = build_mcp(config, database, coordinator)
    app = mcp.http_app(path=config.mcp_path, transport="http")
    app.add_exception_handler(HTTPException, _empty_404)
    app.add_exception_handler(404, _empty_404)

    if coordinator is not None:
        _attach_sync_lifespan(app, config, coordinator)
    return app


def _attach_sync_lifespan(app, config: Config, coordinator: SyncCoordinator) -> None:
    """Haengt den Sync-Worker an den Lebenszyklus der bereits gebauten App."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope) -> AsyncGenerator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            periodic_sync(coordinator, config.sync_interval_min, stop=stop),
            name="visualizer-sync",
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
