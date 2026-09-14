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

from . import __version__, milestone
from .config import Config
from .db import Database
from .decaid_client import (
    VERIFIED_DECAID_VERSION,
    DecaidClient,
    DecaidError,
    DecaidUnreachable,
)
from .guards import ALL_RULES, run_rules
from .metrics import METRICS_VERSION, curve_shape, downsample_curve, metrics_for_shot
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

SERVER_NAME = "visualizer-espresso"

#: SPEC ss12: status() warnt, wenn die Archivluecke gefaehrlich wird - Visualizer
#: So lange darf ein Abgleich ausbleiben, bevor status() das anmerkt. Das
#: Tablet ist oft aus; erst eine laengere Stille heisst, dass etwas fehlt.
STALE_SYNC_WARN_DAYS = 7

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: SPEC ss17.1: die Punktarrays sind die Ausnahme, nicht die Regel - deshalb
#: niedriger als der Vorschlag aus ss9.1 (120).
DEFAULT_MAX_POINTS = 60

#: Punkte, die sich *alle* verglichenen Shots zusammen teilen. SPEC ss9.2 nennt
#: pauschal 60 je Shot - damit sprengen vier Shots mit Kurven das Antwortbudget
#: (gemessen 18.2 kB). Aufgeteilt bleibt auch der schlimmste Fall darunter.
COMPARE_POINT_BUDGET = 100
COMPARE_MIN_POINTS = 20
NOTES_PREVIEW_CHARS = 160

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

INSTRUCTIONS = """\
Lokales Archiv der Espresso-Bezuege einer Decent DE1. Quelle ist Decaid auf
dem Tablet an der Maschine, im eigenen Netz; dieser Server ist die
vollstaendige Historie und die Grundlage fuer Analysen.

TABLET AUS ist kein Fehler. Es laeuft nur, waehrend Kaffee gemacht wird.
Meldet ein Tool `waiting_for_tablet` oder steht es im Status, heisst das:
gerade nicht erreichbar, das Archiv antwortet trotzdem. Das so sagen und
nicht als Stoerung darstellen.

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

VERLAUF - zwei Darstellungen, und die erste reicht fast immer:

- `curve_shape` kommt immer mit. Es beschreibt den Bezug abschnittsweise
  entlang der Phasenmarken der Maschine: je Abschnitt `from`/`to` (Zeit) und
  fuer Druck (`p`) und Waagenfluss (`fo`) jeweils Anfangswert, Endwert,
  Richtung (`rising` | `falling` | `flat`) und `linear` (ob der Verlauf
  zwischen den Enden gerade ist oder gekruemmt). `markers` nennt die markanten
  Zeitpunkte, `source` sagt, woher die Abschnittsgrenzen stammen
  (`machine` = Phasenmarken der Maschine, `markers` = ersatzweise aus
  `pi_end`, `none` = nur ein Abschnitt). Damit lassen sich Fragen nach Anstieg, Abfall,
  Plateau, Dauer einer Phase und Vergleich zweier Bezuege beantworten, ohne
  eine einzige Rohzahl.

- Die Punktarrays (`t`, `p`, `fi`, `fo`, `w`, `tb`, parallele Listen) kommen
  nur auf ausdrueckliche Anforderung (`include_curve` bzw. `include_curves`).
  Sie sind rund zehnmal so gross wie `curve_shape`. Nur anfordern, wenn es um
  Formdetails geht, die `curve_shape` nicht hergibt - etwa Schwingungen
  innerhalb eines Abschnitts. Fehlt ein Kanal, gab es dafuer keinen einzigen
  Messwert.

WAECHTER - `audit_archive` prueft vier Regeln. Was sie bedeuten:

- `grind_not_adjusted` - die Charge wurde gewechselt, der Mahlgrad blieb
  stehen. Jede Bohne mahlt anders; der erste Bezug danach geht meist daneben.
- `bean_age` - die Bohne war beim Bezug ueber der Altersschwelle. Gefrierzeit
  ist herausgerechnet. Steht im Befund `certain: false`, war die Charge
  eingefroren und Decaid fuehrt kein Auftaudatum - das Alter ist dann eine
  **Obergrenze** und als solche weiterzugeben, nicht als feste Zahl.
- `missing_rating` - nach Ablauf der Frist keine Bewertung nachgetragen.
- `dose_outlier` - Dosis weit weg vom Soll. `basis` sagt, woran gemessen
  wurde: am Soll des Workflows oder ersatzweise am Median der Charge.

Ein Befund ist ein Hinweis, kein Urteil. Er nennt immer die Zahlen, auf die
er sich stuetzt - die mitliefern, statt nur die Meldung zu wiederholen.

SCHREIBEN - alle `update_*`- und `set_*`-Tools nur auf ausdrueckliche
Anweisung aufrufen, nie von sich aus und nie "zur Sicherheit". Genau die
Felder setzen, die genannt wurden. Danach anhand der zurueckgelieferten
Werte bestaetigen, nicht anhand dessen, was gesendet wurde: steht ein Feld
unter `unchanged`, hat Decaid es nicht uebernommen - das sagen, statt Erfolg
zu melden.\
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
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        middleware=[CallMetricsMiddleware()],
    )

    @mcp.tool(annotations=READ_ONLY)
    async def list_beans() -> dict[str, Any]:
        """Alle Bohnen im Archiv mit Bezugszahl, Zeitraum und Muehleneinstellungen.

        Einstieg fuer "welche Bohnen gibt es". Zeiten ISO8601 UTC,
        `grinder_setting` ist Freitext der Muehle (z. B. "4,2").
        """
        rows = await asyncio.to_thread(db.list_beans)
        return {
            "beans": [
                {
                    "id": r["bean_id"],
                    "name": r["bean_name"],
                    "roaster": r["roaster"],
                    "processing": r["processing"],
                    "decaf": bool(r["decaf"]) if r["decaf"] is not None else None,
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

        Filter sind Teilstrings ohne Beachtung der Gross-/Kleinschreibung:
        `bean` trifft Marke oder Sorte, `roaster` nur die Marke, `profile` den
        Profilnamen. `since`/`until` nehmen ISO8601 (`2026-07-31`) oder relative
        Kuerzel (`12h`, `7d`, `2w`, `1m`, `1y`). `warnings` ist die Anzahl
        unzuverlaessiger Metriken dieses Shots.

        Bei mehr Treffern als `limit` kommt `next_cursor`; unveraendert als
        `cursor` zurueckschicken. Die erste Seite gleicht vorher mit Visualizer
        ab, wenn noetig (`freshness`); Folgeseiten nicht.

        Einheiten und Begriffe: siehe Server-Anweisungen.
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
        include_curve: bool = False,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> dict[str, Any]:
        """Ein Bezug: Metadaten, Metriken, Kurvenform, Profil-Kurzfassung.

        `id` ist eine Shot-UUID oder `"latest"` (mit `bean` der neueste dieser
        Bohne); bei `"latest"` gleicht der Server vorher ab, wenn noetig.

        Das mitgelieferte `curve_shape` reicht fuer die allermeisten Fragen.
        `include_curve=true` haengt zusaetzlich die Punktarrays an, ausgeduennt
        auf `max_points` (Standard 60, Maximum 400) - nur fuer Formdetails, die
        die Form nicht hergibt.

        Verlauf, Einheiten und Begriffe: siehe Server-Anweisungen.
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
        series = await asyncio.to_thread(_series, db, shot_id)
        payload: dict[str, Any] = {
            "shot": _full_shot(row),
            "metrics": _public_metrics(metrics),
            "curve_shape": curve_shape(series, metrics),
            "profile": await asyncio.to_thread(_profile_summary, db, row["profile_id"]),
        }
        if freshness is not None:
            payload["freshness"] = freshness
        if include_curve:
            payload["curve"] = _curve(series, metrics, max_points)
        return payload

    @mcp.tool(annotations=READ_ONLY)
    async def get_shot_metrics(id: str) -> dict[str, Any]:
        """Nur die abgeleiteten Metriken eines Bezugs, ohne Form, Kurve, Profil.

        Schmalste Antwort, wenn nur Zahlen gebraucht werden. Einheiten und
        Begriffe: siehe Server-Anweisungen.
        """
        metrics = await asyncio.to_thread(metrics_for_shot, db, id)
        if metrics is None:
            raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {id!r}.")
        return {"id": id, **_public_metrics(metrics)}

    @mcp.tool(annotations=READ_ONLY)
    async def compare_shots(
        ids: list[str],
        include_profile: bool = True,
        include_curves: bool = False,
    ) -> dict[str, Any]:
        """Zwei bis vier Bezuege nebeneinander - Metriken, Form, Profile, Deltas.

        Der erste Eintrag in `ids` ist die Bezugsgroesse; `deltas` nennt je
        weiterem Shot die Differenz zu ihm. Felder, die dort `null` sind, fehlen.

        `include_profile` (Standard true) liefert je Shot die Profil-Kurzfassung
        und setzt `profile_notice`, wenn die Bezuege nicht auf denselben
        Sollwerten liefen - ein separater `get_profile`-Aufruf eruebrigt sich
        damit meist. `include_curves=true` haengt Punktarrays an; ueblicherweise
        genuegt das mitgelieferte `curve_shape`.

        Verlauf, Einheiten und Begriffe: siehe Server-Anweisungen.
        """
        if not 2 <= len(ids) <= 4:
            raise ToolError(
                f"invalid_argument: compare_shots braucht 2 bis 4 Kennungen, "
                f"bekommen hat es {len(ids)}."
            )

        per_shot_points = max(COMPARE_MIN_POINTS, COMPARE_POINT_BUDGET // len(ids))

        entries: list[dict[str, Any]] = []
        profiles: list[dict[str, Any] | None] = []
        for shot_id in ids:
            row = await asyncio.to_thread(db.get_shot_row, shot_id)
            if row is None:
                raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {shot_id!r}.")
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
                **_public_metrics(metrics),
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
            if include_curves:
                entry["curve"] = _curve(series, metrics, per_shot_points)
            entries.append(entry)

        payload: dict[str, Any] = {
            "reference": ids[0],
            "shots": entries,
            "deltas": [_delta(entries[0], other) for other in entries[1:]],
        }
        if include_profile:
            notice = _profile_notice(profiles)
            if notice:
                payload["profile_notice"] = notice
        return payload

    @mcp.tool(annotations=READ_ONLY)
    async def list_profiles() -> dict[str, Any]:
        """Alle Profile mit ihren Versionen.

        `version_hash` ist die Identitaet einer Version, `semantic_hash`
        gruppiert Versionen mit gleichen Sollwerten - Naeheres in den
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
        """Die vollstaendigen Sollwerte eines Profils - genau eine Angabe machen.

        `shot_id` liefert die Version, mit der dieser Bezug lief; `version_hash`
        (Praefix genuegt) eine bestimmte, `name` die zuletzt gesehene dieses
        Namens. In den Schritten ist `target` bar bei `mode: pressure`, sonst
        ml/s; `exit` ist `null` ohne aktive Abbruchbedingung. Legacy-Profile
        haben keine Schritte, ihre Sollwerte stehen in `legacy_settings`.

        Zum blossen Vergleich zweier Bezuege genuegt `compare_shots`.
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
        """Holt neue und geaenderte Bezuege sofort vom Tablet.

        Meist unnoetig: der Server synchronisiert selbst, und `get_shot`/
        `list_shots` pruefen ohnehin auf Frische. Aendert in Decaid nichts,
        beliebig wiederholbar. `errors` sind voruebergehende Probleme,
        `warnings` endgueltige Befunde. Ist `waiting_for_tablet` gesetzt, war
        das Tablet aus - kein Fehler, nur nichts zu holen; das so sagen.
        """
        if coordinator is None:
            raise ToolError(
                "sync_unavailable: Dieser Server laeuft ohne Decaid-Verbindung."
            )
        try:
            result = await coordinator.run(full=False)
        except DecaidError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
        return result.as_dict()

    @mcp.tool(annotations=READ_ONLY)
    async def status() -> dict[str, Any]:
        """Zustand des Archivs: Bestand, letzter Abgleich, offene Warnungen.

        Zeiten ISO8601 UTC. `decaid` sagt, wann das Tablet zuletzt erreichbar
        war; `waiting_for_tablet` heisst nicht Stoerung, sondern dass das
        Tablet gerade aus ist - das ist zwischen zwei Kaffees der Normalfall
        und sollte nicht als Fehler gemeldet werden.
        """
        return await asyncio.to_thread(_status_payload, config, db)

    @mcp.tool(annotations=READ_ONLY)
    async def audit_archive(
        since: str | None = None, rule: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Prueft das Archiv auf Unstimmigkeiten (Regeln siehe Anleitung).

        `since` als ISO-Datum oder Kurzform (`7d`, `2w`, `1m`), `rule`
        filtert auf eine der vier Regeln.
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
                    f"invalid_argument: {rule!r} ist keine Regel. Erlaubt: "
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
        }

    if coordinator is not None:
        _register_workflow_reader(mcp, coordinator)

    if config.write_enabled and coordinator is not None:
        _register_update_shot(mcp, db, coordinator)
        _register_catalog_writes(mcp, db, coordinator)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        # Bewusst ohne Secret-Pfad: der Docker-Healthcheck kennt es nicht. Gibt
        # nichts preis, darf aber laut SPEC ss10.1 nicht ins Tunnel-Ingress.
        return PlainTextResponse("ok")

    return mcp


def _register_workflow_reader(mcp: FastMCP, coordinator: SyncCoordinator) -> None:
    """``get_workflow`` liest live vom Tablet - im Archiv steht es nicht."""

    @mcp.tool(annotations=READ_ONLY)
    async def get_workflow() -> dict[str, Any]:
        """Die Einstellung, mit der der naechste Bezug laufen wuerde.

        Kommt live vom Tablet - im Archiv steht nur, womit tatsaechlich
        bezogen wurde.
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
    """Schreibtools fuer Bohne, Charge und Workflow (SPEC ss20.5).

    Dieselben Leitplanken wie ``update_shot``: Whitelist vor dem Senden,
    Read-back danach, und nur vorhanden, wenn ``WRITE_ENABLED`` gesetzt ist.
    """

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def update_bean(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Aendert Stammdaten einer Bohne in Decaid.

        Kennung aus `list_beans`. Erlaubt: name, roaster, species,
        processing, notes, decaf. Gilt rueckwirkend fuer alle Bezuege dieser
        Bohne - das vorher sagen.
        """
        return await _write(coordinator.write_bean, BEAN, id, fields,
                            what="bean")

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def update_batch(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Aendert eine Bohnencharge (Roestdatum, Gefrierzustand).

        Erlaubt: roastDate, buyDate, freezeDate (je ISO YYYY-MM-DD), frozen.
        Decaid fuehrt kein Auftaudatum - beim Auftauen `frozen` auf false
        setzen; das Bohnenalter ist danach nur noch nach oben begrenzt.
        """
        return await _write(coordinator.write_batch, BATCH, id, fields,
                            what="batch")

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def set_workflow(fields: dict[str, Any]) -> dict[str, Any]:
        """Stellt ein, womit der naechste Bezug laufen soll.

        Aendert die Maschine, nicht das Archiv. Erlaubt: grinderSetting,
        grinderModel, targetDoseWeight, targetYield, beanBatchId. Ein
        Profilwechsel ist nicht moeglich - der gehoert an die Maschine.
        Danach nennen, was jetzt eingestellt ist.
        """
        return await _write(coordinator.write_workflow, WORKFLOW, None, fields,
                            what="workflow")


async def _write(
    writer: Any, ruleset: Ruleset, target_id: str | None,
    fields: dict[str, Any], *, what: str,
) -> dict[str, Any]:
    """Gemeinsamer Weg aller Schreibtools: pruefen, senden, nachlesen.

    Ein Weg statt drei, damit Validierung, Read-back-Vergleich und
    Protokollzeile nicht dreimal leicht verschieden ausfallen.
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

    # Feldnamen ja, Werte nein - in Notizen kann Privates stehen.
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
    if ignored:
        result["unchanged"] = ignored
        result["note"] = (
            "Decaid hat diese Felder nicht uebernommen: " + ", ".join(ignored)
            + ". Entweder war der neue Wert mit dem alten identisch, oder "
            "die API hat ihn verworfen."
        )
    return result


def _count_by_rule(findings: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule] = counts.get(finding.rule, 0) + 1
    return counts


def _register_update_shot(mcp: FastMCP, db: Database, coordinator: SyncCoordinator) -> None:
    """Registriert das Schreibtool - nur bei ``WRITE_ENABLED`` (SPEC ss18.4).

    Bewusst als eigene Funktion statt als Flag im Tool: ist der Schalter aus,
    taucht ``update_shot`` in der Tool-Liste gar nicht auf. Ein Tool, das
    existiert und ablehnt, laedt zum Nachfragen ein; eines, das es nicht gibt,
    nicht.
    """

    @mcp.tool(
        annotations={"readOnlyHint": False, "idempotentHint": True,
                     "destructiveHint": False, "openWorldHint": True},
    )
    async def update_shot(id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Aendert Notiz, Bewertung oder Gewichte eines Bezugs in Decaid.

        `fields` ist eine Zuordnung Feldname -> neuer Wert; `null` loescht ein
        Feld. Erlaubt: espressoNotes, enjoyment (0-100), actualDoseWeight,
        actualYield (je g). Zeitstempel, Telemetrie und Profil nicht.
        """
        try:
            payload = validate_fields(fields)
        except ValidationError as exc:
            raise ToolError("invalid_argument: " + "; ".join(exc.problems)) from exc

        row = await asyncio.to_thread(db.get_shot_row, id)
        if row is None:
            raise ToolError(f"shot_not_found: Kein Bezug mit der Kennung {id!r}.")

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

        # Feldnamen ja, Werte nein - in Notizen kann Privates stehen.
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
                "Decaid hat diese Felder nicht uebernommen: "
                + ", ".join(ignored)
                + ". Entweder war der neue Wert mit dem alten identisch, oder "
                "die API hat ihn verworfen."
            )
        return result


# ------------------------------------------------------------------ Aufbereitung


async def _refresh(coordinator: SyncCoordinator) -> dict[str, Any]:
    """Frische-Check fuer ``get_shot("latest")`` und die erste ``list_shots``-Seite.

    Ein Fehler bricht nichts ab: der Bestand ist da, nur vielleicht nicht
    taufrisch - das ist eine bessere Antwort als gar keine.
    """
    try:
        result = await coordinator.ensure_fresh(QUICK_SYNC_MAX_AGE_S)
    except DecaidError as exc:
        # Der Bestand ist da, nur vielleicht nicht ganz aktuell - das ist eine
        # bessere Antwort als gar keine.
        return {"synced": False, "note": f"Sync fehlgeschlagen ({exc.code}), "
                                         "Antwort stammt aus dem Archiv."}
    if result is None:
        return {"synced": False, "note": "Bestand war aktuell, kein Abgleich noetig."}
    if result.waiting_for_tablet:
        # Kein Fehler: das Tablet ist zwischen zwei Kaffees schlicht aus.
        return {"synced": False, "waiting_for_tablet": True,
                "note": "Tablet nicht erreichbar, Antwort stammt aus dem Archiv."}
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


def _public_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Cache-Interna (``metrics_version``, ``n_points``) gehoeren nicht in die Antwort."""
    if not metrics:
        return {"warnings": ["Keine Metriken berechnet."]}
    return {k: v for k, v in metrics.items() if k not in ("metrics_version", "n_points")}


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


def _profile_brief(db: Database, profile_id: int | None) -> dict[str, Any] | None:
    """Kurzfassung fuer ``compare_shots`` - Kopf-Sollwerte, keine Schrittliste.

    Reicht fuer die Frage "liefen die Bezuege auf demselben Profil"; die
    vollstaendigen Schritte liefert ``get_profile``.
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
    """Warnt, wenn die verglichenen Bezuege nicht dieselben Sollwerte hatten.

    Ohne diesen Hinweis liest man Unterschiede leicht als Folge der Einstellung,
    obwohl sie vom Profil kommen.
    """
    known = [p for p in profiles if p]
    if not known:
        return None
    # Fehlende Profile zuerst: sonst verschluckt die Zwei-Profile-Schranke
    # unten genau den Fall, in dem nur eines archiviert ist.
    if len(known) != len(profiles):
        return ("Fuer mindestens einen Bezug ist kein Profil archiviert - der "
                "Vergleich der Sollwerte ist unvollstaendig.")
    if len(known) < 2:
        return None

    versions = {p["version_hash"] for p in known}
    if len(versions) == 1:
        return None

    semantics = {p["semantic_hash"] for p in known}
    if len(semantics) == 1:
        return ("Die Bezuege liefen auf verschiedenen Profilversionen, die aber "
                "identisch bruehen (gleicher semantic_hash) - der Unterschied "
                "ist rein kosmetisch.")
    return ("Achtung: Die Bezuege liefen auf Profilen mit unterschiedlichen "
            "Sollwerten (abweichender semantic_hash). Unterschiede in den "
            "Metriken koennen vom Profil kommen, nicht von Mahlgrad oder Dosis.")


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
            f"Letzter Abgleich vor {age_days:.0f} Tagen - das Tablet war so "
            "lange nicht erreichbar. Neuere Bezuege fehlen im Archiv."
        )
    if config.sync_interval_min == 0:
        warnings.append("Automatischer Sync ist abgeschaltet (SYNC_INTERVAL_MIN=0).")

    missing_profiles = len(db.shot_ids_without_profile())
    if missing_profiles:
        warnings.append(f"{missing_profiles} Shots ohne Profilversion.")

    last_reachable = db.get_state(STATE_LAST_REACHABLE)
    decaid_version = db.get_state(STATE_DECAID_VERSION) or None
    if decaid_version and decaid_version != VERIFIED_DECAID_VERSION:
        # Kein Fehler, aber der Grund, warum eine Annahme ueber die API
        # ploetzlich nicht mehr stimmen koennte.
        warnings.append(
            f"Decaid laeuft in Version {decaid_version}, verifiziert ist "
            f"{VERIFIED_DECAID_VERSION} - Abweichungen im Verhalten der API "
            "sind moeglich."
        )

    last_result = db.get_json_state(STATE_LAST_RESULT, {}) or {}
    if last_result.get("waiting_for_tablet"):
        # Kein Fehler, nur eine Tatsache - das Tablet laeuft nur, waehrend
        # Kaffee gemacht wird.
        warnings.append(
            "Tablet zuletzt nicht erreichbar"
            + (f" (zuletzt erreicht: {last_reachable})" if last_reachable else "")
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
            "notifications": "an" if config.ntfy_url else "aus",
        },
        "version": __version__,
        "milestone": milestone(),
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

    ``enable_sync`` steuert Hintergrundschleife *und* Decaid-Verbindung;
    ohne Angabe laeuft beides, wenn ``SYNC_INTERVAL_MIN > 0`` ist. Tests setzen
    es auf False und kommen damit ohne Netz aus.
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
    """Haengt den Sync-Worker an den Lebenszyklus der bereits gebauten App."""
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
