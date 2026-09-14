"""Entrypoint: Config laden, Logging aufsetzen, Server oder Sync starten."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import uvicorn

from . import __version__
from .config import Config, ConfigError
from .decaid_client import DecaidClient, DecaidError, DecaidUnreachable
from .logging_setup import setup_logging
from .server import build_app
from .sync import open_database, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="visualizer-mcp")
    parser.add_argument(
        "--print-connector-url",
        action="store_true",
        help=(
            "Gibt die vollstaendige Connector-URL inkl. Secret auf stdout aus und "
            "beendet sich. Nur manuell aufrufen - die URL landet sonst dauerhaft "
            "in 'docker logs'."
        ),
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Einmaliger vollstaendiger Sync ueber alle Seiten, dann Ende.",
    )
    parser.add_argument(
        "--sync-once",
        action="store_true",
        help="Einmaliger inkrementeller Sync, dann Ende.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        # Vor setup_logging: bewusst auf stderr, damit auch ein kaputtes
        # LOG_LEVEL die Meldung nicht verschluckt.
        print(exc, file=sys.stderr)
        return 2

    if args.print_connector_url:
        url = config.connector_url
        if url is None:
            print(
                "PUBLIC_BASE_URL ist nicht gesetzt - Pfad lautet: " + config.mcp_path,
                file=sys.stderr,
            )
            return 1
        print(url)
        return 0

    setup_logging(config.log_level, secrets=config.secret_values())
    log = logging.getLogger("visualizer_mcp")
    for warning in config.startup_warnings():
        log.warning(warning)

    if args.backfill or args.sync_once:
        return asyncio.run(_run_sync_cli(config, full=args.backfill))

    log.info(
        "starting",
        extra={
            "fields": {
                "version": __version__,
                "host": config.host,
                "port": config.port,
                "mcp_path": "/<secret>/mcp",
                "sync_interval_min": config.sync_interval_min,
                "db_path": config.db_path,
                "tz": config.display_tz,
            }
        },
    )
    if config.public_base_url:
        log.info(
            "connector url available - print it with: "
            "docker compose exec visualizer-mcp visualizer-mcp --print-connector-url",
            extra={"fields": {"base_url": config.public_base_url}},
        )

    uvicorn.run(
        build_app(config),
        host=config.host,
        port=config.port,
        log_config=None,  # Logging kommt aus setup_logging (inkl. Redaction)
        access_log=False,  # Access-Logs wuerden den Secret-Pfad protokollieren
    )
    return 0


async def _run_sync_cli(config: Config, *, full: bool) -> int:
    log = logging.getLogger("visualizer_mcp")
    db = open_database(config.db_path)
    client = DecaidClient(config.decaid_url)
    try:
        info = await client.info()
        log.info("decaid reached", extra={"fields": {"version": info.get("version")}})
        result = await run_sync(client, db, full=full)
    except DecaidUnreachable:
        # Tablet aus - kein Fehler, nur nichts zu tun.
        log.info("decaid not reachable", extra={"fields": {"url": config.decaid_url}})
        return 0
    except DecaidError as exc:
        log.error("sync failed", extra={"fields": {"error": exc.code, "detail": str(exc)}})
        return 1
    finally:
        await client.aclose()
        db.close()

    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
