"""FastMCP-App: Streamable-HTTP-Endpoint unter dem Secret-Pfad (SPEC ss9, ss10).

M0 liefert nur das Geruest: Transport, Secret-Pfad, Healthcheck und ein
``status``-Tool, mit dem sich der Connector schon jetzt in claude.ai einbinden und
pruefen laesst. Die eigentlichen Tools aus SPEC ss9.2 kommen mit M4.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from . import __version__
from .config import Config

log = logging.getLogger(__name__)

SERVER_NAME = "visualizer-espresso"

INSTRUCTIONS = """\
Zugriff auf das lokale Archiv von Espresso-Bezuegen einer Decent DE1 \
(Quelle: visualizer.coffee).

Einheiten durchgaengig: Druck in bar, Fluss in ml/s, Gewicht/Dosis in g, \
Temperatur in Grad Celsius, Zeit in Sekunden.

Stand dieses Servers: Milestone M0 (Geruest). Es sind noch keine Shot-Daten \
abrufbar - nur `status`.\
"""


def build_mcp(config: Config) -> FastMCP:
    """Baut die FastMCP-Instanz inkl. Tools und Healthcheck-Route."""
    mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def status() -> dict[str, object]:
        """Zustand des Servers: Version, Ausbaustufe, Sync-Konfiguration.

        In Milestone M0 gibt es noch keine Datenbank und keinen Sync; die Felder
        `shots`, `last_sync` und `warnings` bleiben daher leer bzw. null. Das Tool
        dient hier dazu, die Connector-Verbindung zu pruefen.
        """
        return {
            "server": SERVER_NAME,
            "version": __version__,
            "milestone": "M0",
            "database_ready": False,
            "sync_enabled": False,
            "sync_interval_min": config.sync_interval_min,
            "display_timezone": config.display_tz,
            "shots": None,
            "last_sync": None,
            "warnings": ["Milestone M0: Sync und Shot-Tools sind noch nicht gebaut."],
        }

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        # Bewusst ohne Secret-Pfad: der Docker-Healthcheck kennt es nicht. Gibt
        # nichts preis, darf aber laut SPEC ss10.1 nicht ins Tunnel-Ingress.
        return PlainTextResponse("ok")

    return mcp


async def _empty_404(request: Request, exc: Exception) -> Response:
    """404 ohne Body (SPEC ss10.1): ein Scanner soll nicht mal Starlette erkennen."""
    status_code = getattr(exc, "status_code", 404)
    if status_code == 404:
        return Response(status_code=404)
    detail = getattr(exc, "detail", None)
    return PlainTextResponse(str(detail or ""), status_code=status_code)


def build_app(config: Config):
    """Fertige ASGI-App: MCP unter ``/<secret>/mcp``, ``/healthz``, sonst 404."""
    mcp = build_mcp(config)
    app = mcp.http_app(path=config.mcp_path, transport="http")
    app.add_exception_handler(HTTPException, _empty_404)
    app.add_exception_handler(404, _empty_404)
    return app
