"""Das Schreibtool: Sichtbarkeit, Write-through-Kette, Read-back (SPEC ss18).

Der Ersatz-Decaid bildet nach, was am 2026-09-14 gegen die echte API gemessen
wurde: geschuetzte Felder werden mit 400 abgewiesen statt stillschweigend
verworfen, und ein 200 belegt trotzdem nicht, dass jedes Feld uebernommen
wurde - deshalb liest das Tool immer frisch nach.
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
    """Haelt einen Bezug im Speicher und verhaelt sich wie die echte API."""

    #: Annotationen, die Decaid entgegennimmt. ``measurements``, ``id`` und
    #: ``createdAt`` weist es mit 400 ab (T14).
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


# --------------------------------------------------------- (4) Sichtbarkeit


async def test_tool_is_absent_without_the_switch(config: Config, db: Database) -> None:
    """SPEC ss18.4: aus heisst nicht vorhanden, nicht "lehnt ab"."""
    fake = FakeDecaid(reference_detail())
    async with Client(build_mcp(config, db, make_coordinator(fake, db))) as client:
        names = {t.name for t in await client.list_tools()}
    assert "update_shot" not in names
    assert "get_shot" in names, "die Lesetools bleiben unberuehrt"


async def test_tool_appears_with_the_switch(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    async with Client(build_mcp(writable, db, make_coordinator(fake, db))) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert "update_shot" in tools
    assert tools["update_shot"].annotations.readOnlyHint is False
    assert tools["update_shot"].annotations.destructiveHint is False


async def test_tool_stays_absent_without_a_connection(writable: Config, db: Database) -> None:
    # Ohne Decaid-Verbindung gibt es nichts zu schreiben.
    async with Client(build_mcp(writable, db, None)) as client:
        assert "update_shot" not in {t.name for t in await client.list_tools()}


# ------------------------------------------------- (1) Whitelist am Tool


async def test_unknown_field_never_reaches_the_api(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"kaffee": "x"}})

    assert "invalid_argument" in str(excinfo.value)
    assert fake.patches == [], "kein API-Aufruf bei ungueltiger Eingabe"


async def test_validation_happens_before_the_api_call(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError):
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 999}})
    assert fake.patches == []


async def test_protected_fields_never_reach_the_api(writable: Config, db: Database) -> None:
    """Decaid wuerde sie mit 400 abweisen - das Tool kommt ihm zuvor."""
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    for field in ("id", "timestamp", "measurements", "workflow"):
        with pytest.raises(ToolError) as excinfo:
            await call(mcp, "update_shot", {"id": REFERENCE, "fields": {field: "x"}})
        assert "nicht aenderbar" in str(excinfo.value)
    assert fake.patches == []


async def test_unknown_shot_is_caught_before_the_api(writable: Config, db: Database) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot",
                   {"id": "gibt-es-nicht", "fields": {"enjoyment": 50}})
    assert "shot_not_found" in str(excinfo.value)
    assert fake.patches == []


# ------------------------------------------- (2) Write-through und Read-back


async def test_write_through_updates_api_then_database(
    writable: Config, db: Database
) -> None:
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot", {
        "id": REFERENCE,
        "fields": {"enjoyment": 88, "espressoNotes": "schmeckt jetzt rund"},
    })

    # 1. Es ging an die API - im annotations-Umschlag.
    assert len(fake.patches) == 1
    assert fake.patches[0] == {"enjoyment": 88, "espressoNotes": "schmeckt jetzt rund"}

    # 2. Die Antwort nennt vorher und nachher, beides aus dem Read-back.
    assert result["changes"]["enjoyment"] == {"before": 40.0, "after": 88}
    assert result["changes"]["espressoNotes"] == {
        "before": "test", "after": "schmeckt jetzt rund"
    }
    assert "unchanged" not in result

    # 3. Lokal nachgezogen.
    row = db.get_shot_row(REFERENCE)
    assert row["enjoyment"] == 88
    assert row["notes"] == "schmeckt jetzt rund"


async def test_the_metrics_cache_is_refilled_not_just_emptied(
    writable: Config, db: Database
) -> None:
    """Der Grund, warum der Read-back die Metriken mitzieht."""
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))
    await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 70}})

    metrics = db.get_cached_metrics(REFERENCE, METRICS_VERSION)
    assert metrics is not None, "der Cache muss neu gefuellt sein, nicht bloss geleert"
    assert metrics["pi_end"] == 21.1


async def test_a_zero_rating_is_written_as_a_rating(
    writable: Config, db: Database
) -> None:
    """Die Null-Regel gilt beim Lesen der Import-Aera, nicht bei einer Eingabe.

    Der Bezug traegt eine de1app-Kennung; setzt der Nutzer hier ausdruecklich 0,
    muss das ankommen und nicht als "nicht bewertet" verschwinden.
    """
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 0}})
    assert fake.patches == [{"enjoyment": 0}]
    assert result["changes"]["enjoyment"]["after"] == 0


async def test_silently_dropped_field_is_reported(writable: Config, db: Database) -> None:
    """Der gefaehrlichste Fall: 200 zurueck, aber nichts geaendert.

    Hier deckt allein der Read-back auf, dass der Wert schon so dastand.
    """
    fake = FakeDecaid(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    result = await call(mcp, "update_shot",
                        {"id": REFERENCE, "fields": {"enjoyment": 40}})

    assert result["unchanged"] == ["enjoyment"]
    assert "nicht uebernommen" in result["note"]


async def test_api_rejection_becomes_a_tool_error(writable: Config, db: Database) -> None:
    class Stubborn(FakeDecaid):
        PERMITTED: set[str] = set()

    fake = Stubborn(reference_detail())
    mcp = build_mcp(writable, db, make_coordinator(fake, db))

    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "update_shot", {"id": REFERENCE, "fields": {"enjoyment": 50}})
    assert "decaid_rejected" in str(excinfo.value)


async def test_a_protected_field_at_the_api_is_not_retried(db: Database) -> None:
    """Gegenprobe direkt am Client: 400 ist endgueltig, kein Wiederholen."""
    fake = FakeDecaid(reference_detail())
    client = DecaidClient("http://10.100.100.171:8080",
                          transport=httpx.MockTransport(fake.handler))
    async with client:
        with pytest.raises(DecaidRejected):
            await client.update_shot(REFERENCE, {"id": "anders"})


# ------------------------------------------------------------ (3) Protokoll


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
