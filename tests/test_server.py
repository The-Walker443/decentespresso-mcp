from __future__ import annotations

import json

from fastmcp import Client
from starlette.testclient import TestClient

from visualizer_mcp.config import Config
from visualizer_mcp.server import SERVER_NAME, build_app, build_mcp

from .conftest import TEST_SECRET


def test_healthz_is_reachable_without_secret(config: Config) -> None:
    with TestClient(build_app(config)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_mcp_endpoint_exists_under_secret_path(config: Config) -> None:
    with TestClient(build_app(config)) as client:
        response = client.post(
            f"/{TEST_SECRET}/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    # Inhalt egal - entscheidend ist, dass der Pfad existiert.
    assert response.status_code != 404


def test_unknown_paths_return_bare_404(config: Config) -> None:
    with TestClient(build_app(config)) as client:
        for path in ["/", "/mcp", "/wrong-secret/mcp", f"/{TEST_SECRET}", "/.env"]:
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.content == b"", path


async def test_status_tool_is_exposed_and_readonly(config: Config) -> None:
    async with Client(build_mcp(config)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(tools) == {"status"}
        assert tools["status"].annotations.readOnlyHint is True

        result = await client.call_tool("status", {})

    payload = result.data if result.data is not None else json.loads(result.content[0].text)
    assert payload["server"] == SERVER_NAME
    assert payload["milestone"] == "M0"
    assert payload["database_ready"] is False
    assert payload["sync_interval_min"] == config.sync_interval_min


async def test_no_secret_leaks_into_mcp_metadata(config: Config) -> None:
    # Was Claude vom Server sieht, darf das Secret nicht enthalten.
    async with Client(build_mcp(config)) as client:
        tools = await client.list_tools()
        result = await client.call_tool("status", {})
    blob = json.dumps([tool.model_dump(mode="json") for tool in tools]) + str(result.data)
    assert TEST_SECRET not in blob
