"""FastMCP-App: Streamable-HTTP-Endpoint unter dem Secret-Pfad (SPEC ss9, ss10).

M1 liefert Datenbank, Sync-Worker und ein aussagefaehiges ``status``-Tool. Die
uebrigen Tools aus SPEC ss9.2 (list_shots, get_shot, ...) folgen in M4.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastmcp import FastMCP
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from . import __version__
from .config import Config
from .db import Database
from .sync import STATE_BACKFILL_DONE, STATE_LAST_RESULT, STATE_LAST_SYNC, periodic_sync
from .visualizer_client import VisualizerClient

log = logging.getLogger(__name__)

SERVER_NAME = "visualizer-espresso"

#: SPEC ss12: status() warnt, wenn die Archivluecke gefaehrlich wird - Visualizer
#: Free haelt nur ein 1-Monats-Fenster vor.
STALE_SYNC_WARN_DAYS = 7

INSTRUCTIONS = """\
Zugriff auf das lokale Archiv von Espresso-Bezuegen einer Decent DE1 \
(Quelle: visualizer.coffee).

Einheiten durchgaengig: Druck in bar, Fluss in ml/s, Gewicht/Dosis in g, \
Temperatur in Grad Celsius, Zeit in Sekunden.

Stand dieses Servers: Milestone M1. Shots und Zeitreihen werden synchronisiert; \
abrufbar ist bisher nur `status`. Profilversionen und die Analyse-Tools folgen.\
"""


def build_mcp(config: Config, db: Database) -> FastMCP:
    """Baut die FastMCP-Instanz inkl. Tools und Healthcheck-Route."""
    mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def status() -> dict[str, object]:
        """Zustand des Archivs: Anzahl Shots, Zeitraum, letzter Sync, Warnungen.

        Zeiten sind ISO8601 in UTC. `shots` zaehlt die lokal archivierten Bezuege,
        `series_points` die gespeicherten Messpunkte. `warnings` meldet unter
        anderem, wenn der letzte Sync so lange her ist, dass Bezuege aus dem
        1-Monats-Fenster des Visualizer-Free-Tiers gefallen sein koennten.
        """
        return await asyncio.to_thread(_status_payload, config, db)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        # Bewusst ohne Secret-Pfad: der Docker-Healthcheck kennt es nicht. Gibt
        # nichts preis, darf aber laut SPEC ss10.1 nicht ins Tunnel-Ingress.
        return PlainTextResponse("ok")

    return mcp


def _status_payload(config: Config, db: Database) -> dict[str, object]:
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

    errors = db.get_json_state("last_errors", []) or []
    return {
        "server": SERVER_NAME,
        "version": __version__,
        "milestone": "M1",
        "shots": db.count_shots(),
        "series_points": db.count_series_points(),
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

    ``enable_sync`` steuert die Hintergrundschleife; ohne Angabe laeuft sie, wenn
    ``SYNC_INTERVAL_MIN > 0`` ist. Tests setzen sie explizit auf False.
    """
    database = db or Database(config.db_path)
    database.migrate()
    sync_on = config.sync_interval_min > 0 if enable_sync is None else enable_sync

    mcp = build_mcp(config, database)
    app = mcp.http_app(path=config.mcp_path, transport="http")
    app.add_exception_handler(HTTPException, _empty_404)
    app.add_exception_handler(404, _empty_404)

    if sync_on:
        _attach_sync_lifespan(app, config, database)
    return app


def _attach_sync_lifespan(app, config: Config, db: Database) -> None:
    """Haengt den Sync-Worker an den Lebenszyklus der bereits gebauten App."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope) -> AsyncIterator[None]:
        client = VisualizerClient(
            config.visualizer_email,
            config.visualizer_password,
            user_agent=config.user_agent,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            periodic_sync(client, db, config.sync_interval_min, stop=stop),
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
            await client.aclose()
            log.info("sync worker stopped")

    app.router.lifespan_context = lifespan
