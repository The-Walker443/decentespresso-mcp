"""The write tool: visibility, write-through chain, read-back (SPEC §11).

The stand-in Decaid reproduces what was measured against the real API on
2026-09-14: protected fields are refused with 400 rather than silently
discarded, and a 200 still does not prove that every field was taken - which
is why the tool always reads back.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import decaid_detail, store_shot

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_client import DecaidClient, DecaidRejected
from decentespresso_mcp.metrics import METRICS_VERSION, warm_metrics_cache
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import SyncCoordinator

REFERENCE = "de1app-1785525360"


def reference_detail() -> dict:
    return decaid_detail(REFERENCE, timestamp="2026-07-31T19:16:00",
                         enjoyment=40.0, notes="test")


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "writes.db")
    database.migrate()
    store_shot(database, reference_detail())
    warm_metrics_cache(database)
    yield database
    database.close()


@pytest.fixture
def writable(valid_env: dict[str, str]) -> Config:
    return Config.from_env({**valid_env, "WRITE_ENABLED": "true"})


class FakeDecaid:
    """Keeps one shot in memory and behaves like the real API."""

    #: Annotations Decaid accepts. It refuses ``measurements``, ``id`` and
    #: ``createdAt`` with 400 (T14).
    PERMITTED = {"espressoNotes", "enjoyment", "actualDoseWeight", "actualYield",
                 "extras"}

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = json.loads(json.dumps(detail))
        self.patches: list[dict[str, Any]] = []
        self.gets = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets += 1
            return httpx.Response(200, json=self.detail)

        body = json.loads(request.content)
        for protected in ("id", "createdAt", "measurements"):
            if protected in body:
                return httpx.Response(
                    400, json={"detail": f"{protected} is read-only"}
                )

        fields = body.get("annotations") or {}
        self.patches.append(fields)
        applied = {k: v for k, v in fields.items() if k in self.PERMITTED}
        if not applied:
            return httpx.Response(400, json={"detail": "nothing to update"})

        self.detail.setdefault("annotations", {}).update(applied)
        self.detail["updatedAt"] = "2026-09-14T18:00:00Z"
        return httpx.Response(200, json=self.detail)


def make_coordinator(fake: FakeDecaid, db: Database) -> SyncCoordinator:
    client = DecaidClient(
        "http://10.100.100.171:8080",
        transport=httpx.MockTransport(fake.handler),
    )
    return SyncCoordinator(client, db)


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


# ---------------------------------------------------------- (4) Visibility


async def test_tool_is_absent_without_the_switch(config: Config, db: Database) -> None:
    """SPEC §11: off means absent, not "refuses"."""
    fake = FakeDecaid(reference_detail())
    async with Client(build_mcp(config, db, make_coordinator(fake, db))) as client:
        names = {t.name for t in await client.list_tools()}
    assert "update_shot" not in names
    assert "get_shot" in names, "the read tools stay untouched"


async def test_tool_appears_with_the_switch(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    async with Client(build_mcp(writable, db, make_coordinator(fake, db))) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert "update_shot" in tools
    assert tools["update_shot"].annotations.readOnlyHint is False
    assert tools["update_shot"].annotations.destructiveHint is False


async def test_tool_stays_absent_without_a_connection(writable: Config, db: Database) -> None:
    # Without a Decaid connection there is nothing to write.
    async with Client(build_mcp(writable, db, None)) as client:
        assert "update_shot" not in {t.name for t in await client.list_tools()}


# ---------------------------------------------- (1) Whitelist at the tool


async def test_unknown_field_never_reaches_the_api(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"kaffee": "x"}})

    assert "invalid_argument" in str(excinfo.value)
    assert fake.patches == [], "no API call on invalid input"


async def test_validation_happens_before_the_api_call(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError):
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 999}})
    assert fake.patches == []


async def test_protected_fields_never_reach_the_api(writable: Config, db: Database) -> None:
    """Decaid would refuse them with 400 - the tool gets there first."""
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    for field in ("id", "timestamp", "measurements", "workflow"):
        with pytest.raises(ToolError) as excinfo:
            await call(mcp, "update_shot", {"id": REFERENCE, "fields": {field: "x"}})
        assert "not writable" in str(excinfo.value)
    assert fake.patches == []


async def test_unknown_shot_is_caught_before_the_api(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot",
                   {"id": "does-not-exist", "fields": {"enjoyment": 50}})
    assert "shot_not_found" in str(excinfo.value)
    assert fake.patches == []


# ------------------------------------------ (2) Write-through and read-back


async def test_write_through_updates_api_then_database(
    writable: Config, db: Database
) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot", {
        "id": REFERENCE,
        "fields": {"enjoyment": 88, "espressoNotes": "schmeckt jetzt rund"},
    })

    # 1. It went to the API - inside the annotations envelope.
    assert len(fake.patches) == 1
    assert fake.patches[0] == {"enjoyment": 88, "espressoNotes": "schmeckt jetzt rund"}

    # 2. The response names before and after, both from the read-back.
    assert result["changes"]["enjoyment"] == {"before": 40.0, "after": 88}
    assert result["changes"]["espressoNotes"] == {
        "before": "test", "after": "schmeckt jetzt rund"
    }
    assert "unchanged" not in result

    # 3. Updated locally.
    row = db.get_shot_row(REFERENCE)
    assert row["enjoyment"] == 88
    assert row["notes"] == "schmeckt jetzt rund"


async def test_the_metrics_cache_is_refilled_not_just_emptied(
    writable: Config, db: Database
) -> None:
    """The reason the read-back carries the metrics along."""
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))
    await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 70}})

    metrics = db.get_cached_metrics(REFERENCE, METRICS_VERSION)
    assert metrics is not None, "the cache must be refilled, not merely emptied"
    assert metrics["pi_end"] == 21.1


async def test_a_zero_rating_is_written_as_a_rating(
    writable: Config, db: Database
) -> None:
    """The zero rule applies when reading the import era, not to an input.

    The shot carries a de1app identifier; if the user explicitly sets 0 here it
    must arrive and not vanish as "not rated".
    """
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 0}})
    assert fake.patches == [{"enjoyment": 0}]
    assert result["changes"]["enjoyment"]["after"] == 0


async def test_silently_dropped_field_is_reported(writable: Config, db: Database) -> None:
    """The most dangerous case: a 200 comes back but nothing changed.

    Here the read-back alone reveals that the value already stood that way.
    """
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot",
                        {"id": REFERENCE, "fields": {"enjoyment": 40}})

    assert result["unchanged"] == ["enjoyment"]
    assert "did not take" in result["note"]


async def test_api_rejection_becomes_a_tool_error(writable: Config, db: Database) -> None:
    class Stubborn(FakeDecaid):
        PERMITTED: set[str] = set()

    fake = Stubborn(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 50}})
    assert "decaid_rejected" in str(excinfo.value)


async def test_a_protected_field_at_the_api_is_not_retried(db: Database) -> None:
    """Cross-check at the client: a 400 is final, no retry."""
    fake = FakeDecaid(reference_detail())
    client = DecaidClient("http://10.100.100.171:8080",
                          transport=httpx.MockTransport(fake.handler))
    async with client:
        with pytest.raises(DecaidRejected):
            await client.update_shot(REFERENCE, {"id": "anders"})


# -------------------------------------------------------------- (3) Logging


async def test_log_names_fields_but_never_values(
    writable: Config, db: Database, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="decentespresso_mcp.server")
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    await call(mcp, "update_shot", {
        "id": REFERENCE,
        "fields": {"espressoNotes": "streng vertraulich", "enjoyment": 60},
    })

    records = [r for r in caplog.records if r.getMessage() == "shot updated"]
    assert len(records) == 1
    fields = records[0].fields
    assert fields["shot"] == REFERENCE
    assert fields["wrote"] == "enjoyment,espressoNotes"
    assert "dur_ms" in fields

    blob = "\n".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert "streng vertraulich" not in blob
    assert "60" not in fields["wrote"]
