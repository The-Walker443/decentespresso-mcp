from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.server import SERVER_NAME, build_app, build_mcp
from visualizer_mcp.visualizer_client import shot_row_from_detail

from .conftest import TEST_SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "server.db")
    database.migrate()
    yield database
    database.close()


def app_for(config: Config, db: Database):
    # enable_sync=False: kein Hintergrund-Worker, kein Netzverkehr im Test.
    return build_app(config, db=db, enable_sync=False)


def test_healthz_is_reachable_without_secret(config: Config, db: Database) -> None:
    with TestClient(app_for(config, db)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_mcp_endpoint_exists_under_secret_path(config: Config, db: Database) -> None:
    with TestClient(app_for(config, db)) as client:
        response = client.post(
            f"/{TEST_SECRET}/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert response.status_code != 404


def test_unknown_paths_return_bare_404(config: Config, db: Database) -> None:
    with TestClient(app_for(config, db)) as client:
        for path in ["/", "/mcp", "/wrong-secret/mcp", f"/{TEST_SECRET}", "/.env"]:
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.content == b"", path


async def test_status_reports_empty_archive(config: Config, db: Database) -> None:
    async with Client(build_mcp(config, db)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(tools) == {"status"}
        assert tools["status"].annotations.readOnlyHint is True
        payload = (await client.call_tool("status", {})).data

    assert payload["server"] == SERVER_NAME
    assert payload["shots"] == 0
    assert payload["last_sync"] is None
    assert any("Backfill" in w for w in payload["warnings"])
    assert any("noch keinen Sync" in w for w in payload["warnings"])


async def test_status_reports_real_counts(config: Config, db: Database) -> None:
    detail = json.loads((FIXTURES / "shot_reference.json").read_text(encoding="utf-8"))
    db.upsert_shot(shot_row_from_detail(detail, "2026-08-01T10:00:00Z"), [])
    db.set_state("last_sync_at", "2026-08-01T10:00:00Z")
    db.set_state("backfill_completed_at", "2026-08-01T10:00:00Z")

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["shots"] == 1
    assert payload["oldest_shot"] == "2026-07-31T19:16:00Z"
    assert payload["last_sync"] == "2026-08-01T10:00:00Z"
    assert not [w for w in payload["warnings"] if "Backfill" in w]


async def test_status_warns_about_stale_sync(config: Config, db: Database) -> None:
    db.set_state("backfill_completed_at", "2020-01-01T00:00:00Z")
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert any("1-Monats-Fenster" in w for w in payload["warnings"])


async def test_no_secret_leaks_into_mcp_metadata(config: Config, db: Database) -> None:
    async with Client(build_mcp(config, db)) as client:
        tools = await client.list_tools()
        result = await client.call_tool("status", {})
    blob = json.dumps([t.model_dump(mode="json") for t in tools]) + json.dumps(result.data)
    assert TEST_SECRET not in blob
    assert config.visualizer_password not in blob
