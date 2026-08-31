"""Das Schreibtool: Sichtbarkeit, Write-through-Kette, Read-back (SPEC ss18)."""

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

from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.metrics import METRICS_VERSION, warm_metrics_cache
from visualizer_mcp.server import build_mcp
from visualizer_mcp.sync import SyncCoordinator
from visualizer_mcp.visualizer_client import (
    RequestRejected,
    VisualizerClient,
    series_rows_from_detail,
    shot_row_from_detail,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
REFERENCE = "6eb25d36-ff0c-48d0-8f88-0b05f9c7418a"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "writes.db")
    database.migrate()
    payload = load("shot_reference.json")
    database.upsert_shot(
        shot_row_from_detail(payload, "2026-08-01T10:00:00Z"),
        series_rows_from_detail(payload),
    )
    warm_metrics_cache(database)
    yield database
    database.close()


@pytest.fixture
def writable(valid_env: dict[str, str]) -> Config:
    return Config.from_env({**valid_env, "WRITE_ENABLED": "true"})


class FakeVisualizer:
    """Haelt einen Shot im Speicher und verhaelt sich wie die echte API.

    Bildet die drei Eigenheiten nach, die die Verifikation am 2026-08-01
    zutage gefoerdert hat: PATCH braucht Accept: application/json, nicht
    erlaubte Felder werden still verworfen, und bleibt danach nichts uebrig,
    kommt 400.
    """

    #: Was Visualizer fuer dieses (Free-)Konto tatsaechlich annimmt.
    PERMITTED = {
        "bean_brand", "bean_type", "roast_date", "roast_level", "bean_notes",
        "grinder_setting", "bean_weight", "drink_weight",
        "espresso_enjoyment", "espresso_notes", "drink_tds", "drink_ey", "barista",
    }

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = json.loads(json.dumps(detail))
        self.patches: list[dict[str, Any]] = []
        self.gets = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets += 1
            return httpx.Response(200, json=self.detail)

        if request.headers.get("Accept") != "application/json":
            return httpx.Response(422, json={"error": "Request must be JSON."})

        body = json.loads(request.content)
        fields = body.get("shot", {})
        self.patches.append(fields)
        applied = {k: v for k, v in fields.items() if k in self.PERMITTED}
        if not applied:
            return httpx.Response(
                400,
                json={"error": "param is missing or the value is empty or invalid: shot"},
            )
        self.detail.update(applied)
        self.detail["updated_at"] = self.detail.get("updated_at", 0) + 1
        return httpx.Response(200, json=self.detail)


def make_coordinator(fake: FakeVisualizer, db: Database) -> SyncCoordinator:
    client = VisualizerClient(
        "shots@example.org", "hunter2-but-long-enough",
        transport=httpx.MockTransport(fake.handler),
    )
    return SyncCoordinator(client, db)


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


# --------------------------------------------------------- (4) Sichtbarkeit


async def test_tool_is_absent_without_the_switch(config: Config, db: Database) -> None:
    """SPEC ss18.4: aus heisst nicht vorhanden, nicht 'lehnt ab'."""
    fake = FakeVisualizer(load("shot_reference.json"))
    async with Client(build_mcp(config, db, make_coordinator(fake, db))) as client:
        names = {t.name for t in await client.list_tools()}
    assert "update_shot" not in names
    assert "get_shot" in names, "die Lesetools bleiben unberuehrt"


async def test_tool_appears_with_the_switch(writable: Config, db: Database) -> None:
    fake = FakeVisualizer(load("shot_reference.json"))
    async with Client(build_mcp(writable, db, make_coordinator(fake, db))) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert "update_shot" in tools
    assert tools["update_shot"].annotations.readOnlyHint is False
    assert tools["update_shot"].annotations.destructiveHint is False


async def test_tool_stays_absent_without_a_connection(writable: Config, db: Database) -> None:
    # Ohne Visualizer-Verbindung gibt es nichts zu schreiben.
    async with Client(build_mcp(writable, db, None)) as client:
        assert "update_shot" not in {t.name for t in await client.list_tools()}


# ------------------------------------------------- (1) Whitelist am Tool


async def test_unknown_field_never_reaches_the_api(writable: Config, db: Database) -> None:
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"kaffee": "x"}})

    assert "invalid_argument" in str(excinfo.value)
    assert fake.patches == [], "kein API-Aufruf bei ungueltiger Eingabe"


async def test_validation_happens_before_the_api_call(writable: Config, db: Database) -> None:
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError):
        await call(mcp, "update_shot",
                   {"id": REFERENCE, "fields": {"espresso_enjoyment": 999}})
    assert fake.patches == []


async def test_unknown_shot_is_caught_before_the_api(writable: Config, db: Database) -> None:
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot",
                   {"id": "gibt-es-nicht", "fields": {"espresso_enjoyment": 50}})
    assert "shot_not_found" in str(excinfo.value)
    assert fake.patches == []


# ------------------------------------------- (2) Write-through und Read-back


async def test_write_through_updates_api_then_database(
    writable: Config, db: Database
) -> None:
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot", {
        "id": REFERENCE,
        "fields": {"espresso_enjoyment": 88, "espresso_notes": "schmeckt jetzt rund"},
    })

    # 1. Es ging an die API - mit dem dokumentierten shot-Wrapper.
    assert len(fake.patches) == 1
    assert fake.patches[0] == {"espresso_enjoyment": 88,
                               "espresso_notes": "schmeckt jetzt rund"}

    # 2. Die Antwort nennt vorher und nachher, beides aus dem Read-back.
    assert result["changes"]["espresso_enjoyment"] == {"before": 40, "after": 88}
    assert result["changes"]["espresso_notes"] == {
        "before": "test", "after": "schmeckt jetzt rund"
    }
    assert "unchanged" not in result

    # 3. Lokal nachgezogen.
    row = db.get_shot_row(REFERENCE)
    assert row["enjoyment"] == 88
    assert row["notes"] == "schmeckt jetzt rund"


async def test_dose_change_recomputes_the_ratio(writable: Config, db: Database) -> None:
    """Der Grund, warum der Metrik-Cache mitgezogen wird."""
    before_row = db.get_shot_row(REFERENCE)
    assert before_row["dose_g"] == 18.0
    assert before_row["ratio"] == pytest.approx(2.011, abs=0.001)

    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))
    await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"bean_weight": 20.0}})

    row = db.get_shot_row(REFERENCE)
    assert row["dose_g"] == 20.0
    assert row["ratio"] == pytest.approx(36.2 / 20.0, abs=0.001)

    metrics = db.get_cached_metrics(REFERENCE, METRICS_VERSION)
    assert metrics is not None, "der Cache muss neu gefuellt sein, nicht bloss geleert"
    assert metrics["ratio"] == pytest.approx(36.2 / 20.0, abs=0.001)


async def test_silently_dropped_field_is_reported(writable: Config, db: Database) -> None:
    """Der gefaehrlichste Fall: 200 zurueck, aber nichts geschrieben.

    Visualizer verwirft private_notes ohne Premium still. Weil ein erlaubtes
    Feld mitkommt, meldet die API Erfolg - nur der Read-back deckt es auf.
    """
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot", {
        "id": REFERENCE,
        "fields": {"espresso_enjoyment": 70, "private_notes": "geheim"},
    })

    assert result["changes"]["espresso_enjoyment"]["after"] == 70
    assert result["unchanged"] == ["private_notes"]
    assert "Premium" in result["note"]


async def test_api_rejection_becomes_a_tool_error(writable: Config, db: Database) -> None:
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    # Nur ein nicht erlaubtes Feld -> die API laesst nichts uebrig -> 400.
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot",
                   {"id": REFERENCE, "fields": {"private_notes": "geheim"}})
    assert "visualizer_rejected" in str(excinfo.value)


async def test_client_sends_the_accept_header(db: Database) -> None:
    """Belegt, dass der Client den Header wirklich mitschickt."""
    fake = FakeVisualizer(load("shot_reference.json"))
    client = VisualizerClient(
        "a@b.org", "lang-genug-hier", transport=httpx.MockTransport(fake.handler)
    )
    async with client:
        detail = await client.update_shot(REFERENCE, {"espresso_enjoyment": 60})
    assert detail["espresso_enjoyment"] == 60


async def test_missing_accept_header_would_fail(db: Database) -> None:
    # Gegenprobe: ohne den Header antwortet die echte API mit 422.
    fake = FakeVisualizer(load("shot_reference.json"))
    client = VisualizerClient(
        "a@b.org", "lang-genug-hier", transport=httpx.MockTransport(fake.handler)
    )
    async with client:
        with pytest.raises(RequestRejected):
            await client._request(
                "PATCH", f"/shots/{REFERENCE}",
                json_body={"shot": {"espresso_enjoyment": 60}},
            )


# ------------------------------------------------------------ (3) Protokoll


async def test_log_names_fields_but_never_values(
    writable: Config, db: Database, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="visualizer_mcp.server")
    fake = FakeVisualizer(load("shot_reference.json"))
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    await call(mcp, "update_shot", {
        "id": REFERENCE,
        "fields": {"espresso_notes": "streng vertraulich", "espresso_enjoyment": 60},
    })

    records = [r for r in caplog.records if r.getMessage() == "shot updated"]
    assert len(records) == 1
    fields = records[0].fields
    assert fields["shot"] == REFERENCE
    assert fields["wrote"] == "espresso_enjoyment,espresso_notes"
    assert "dur_ms" in fields

    blob = "\n".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert "streng vertraulich" not in blob
    assert "60" not in fields["wrote"]
