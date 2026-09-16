from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest
from fastmcp import Client
from helpers import decaid_detail, store_shot
from starlette.testclient import TestClient

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.server import SERVER_NAME, build_app, build_mcp

from .conftest import TEST_SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "server.db")
    database.migrate()
    yield database
    database.close()


def app_for(config: Config, db: Database):
    # enable_sync=False: no background worker, no network traffic in the test.
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


async def test_all_spec_tools_are_exposed(config: Config, db: Database) -> None:
    async with Client(build_mcp(config, db)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    # SPEC §9 plus audit_archive from §10. get_workflow and the write tools
    # depend on the coordinator and on WRITE_ENABLED, so they are absent here.
    assert set(tools) == {
        "list_beans", "list_shots", "get_shot", "get_shot_metrics",
        "compare_shots", "list_profiles", "get_profile", "sync_now", "status",
        "audit_archive", "stats", "list_batches", "get_batch",
    }
    # Everything except sync_now is read-only (SPEC §9).
    for name, tool in tools.items():
        expected = name != "sync_now"
        assert tool.annotations.readOnlyHint is expected, name


async def test_status_reports_empty_archive(config: Config, db: Database) -> None:
    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["server"] == SERVER_NAME
    assert payload["shots"] == 0
    assert payload["last_sync"] is None
    assert any("backfill" in w for w in payload["warnings"])
    assert any("not been a sync run" in w for w in payload["warnings"])


async def test_status_reports_real_counts(config: Config, db: Database) -> None:
    store_shot(db, decaid_detail("de1app-1785525360",
                                 timestamp="2026-07-31T19:16:00"))
    db.set_state("last_sync_at", "2026-08-01T10:00:00Z")
    db.set_state("backfill_completed_at", "2026-08-01T10:00:00Z")

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["shots"] == 1
    assert payload["oldest_shot"] == "2026-07-31T19:16:00Z"
    assert payload["last_sync"] == "2026-08-01T10:00:00Z"
    assert not [w for w in payload["warnings"] if "backfill" in w]


async def test_status_warns_about_stale_sync(config: Config, db: Database) -> None:
    db.set_state("backfill_completed_at", "2020-01-01T00:00:00Z")
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert any("has not been reachable for that long" in w
               for w in payload["warnings"])


async def test_no_secret_leaks_into_mcp_metadata(config: Config, db: Database) -> None:
    async with Client(build_mcp(config, db)) as client:
        tools = await client.list_tools()
        result = await client.call_tool("status", {})
    blob = json.dumps([t.model_dump(mode="json") for t in tools]) + json.dumps(result.data)
    assert TEST_SECRET not in blob
    assert config.decaid_url in json.dumps(result.data), (
        "the LAN address is no secret and helps when looking things up")
