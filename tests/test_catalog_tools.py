"""audit_archive, get_workflow und die Katalog-Schreibtools (SPEC ss20.5, ss20.7)."""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import decaid_detail, store_shot

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_client import DecaidClient
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import SyncCoordinator

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
BEAN_ID = "bean-1"
BATCH_ID = "batch-1"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeDecaid:
    """Haelt Bohne, Charge und Workflow im Speicher.

    Bildet nach, was am 2026-09-14 gemessen wurde: PUT nimmt die erlaubten
    Felder an, geschuetzte weist es mit 400 ab, und Datumsangaben kommen mit
    angehaengter Uhrzeit zurueck.
    """

    def __init__(self) -> None:
        self.beans = [{"id": BEAN_ID, "name": "Testsorte", "roaster": "Tchibo",
                       "species": None, "processing": "washed", "decaf": False,
                       "archived": False, "notes": "alt",
                       "createdAt": None, "updatedAt": None}]
        self.batches = [{"id": BATCH_ID, "beanId": BEAN_ID,
                         "roastDate": "2026-09-01T00:00:00.000Z",
                         "buyDate": None, "freezeDate": None, "frozen": False,
                         "archived": False, "createdAt": None, "updatedAt": None}]
        self.workflow = {
            "id": "wf-1",
            "context": {"grinderSetting": "3.30", "grinderModel": "Niche",
                        "targetDoseWeight": 18.0, "targetYield": 45.0,
                        "beanBatchId": BATCH_ID, "coffeeName": "Testsorte",
                        "coffeeRoaster": "Tchibo"},
            "profile": {"title": "D-Flow", "steps": [{"name": "Filling"}]},
        }
        self.puts: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/beans"):
                return httpx.Response(200, json=self.beans)
            if path.endswith("/bean-batches"):
                return httpx.Response(200, json=self.batches)
            if path.endswith("/workflow"):
                return httpx.Response(200, json=self.workflow)
            if path.endswith("/info"):
                return httpx.Response(200, json=load("info.json"))
            return httpx.Response(404, json={"error": "unbekannt"})

        body = json.loads(request.content)
        self.puts.append((path, body))

        if "/beans/" in path:
            return self._patch(self.beans[0], body)
        if "/bean-batches/" in path:
            for field in ("roastDate", "buyDate", "freezeDate"):
                if isinstance(body.get(field), str):
                    body[field] = body[field] + "T00:00:00.000Z"
            return self._patch(self.batches[0], body)
        if path.endswith("/workflow"):
            if "profile" in body:
                return httpx.Response(400, json={"error": "profile is read-only"})
            self.workflow["context"].update(body.get("context") or {})
            return httpx.Response(200, json=self.workflow)
        return httpx.Response(404, json={"error": "unbekannt"})

    @staticmethod
    def _patch(target: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        if "id" in body:
            return httpx.Response(400, json={"error": "id is read-only"})
        target.update(body)
        return httpx.Response(200, json=target)


@pytest.fixture
def fake() -> FakeDecaid:
    return FakeDecaid()


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "katalog.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def coordinator(fake: FakeDecaid, db: Database) -> SyncCoordinator:
    client = DecaidClient("http://10.100.100.171:8080",
                          transport=httpx.MockTransport(fake.handler))
    return SyncCoordinator(client, db)


@pytest.fixture
def writable(valid_env: dict[str, str]) -> Config:
    return Config.from_env({**valid_env, "WRITE_ENABLED": "true"})


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


def stocked(db: Database) -> None:
    """Ein Bestand, an dem jede Waechterregel etwas zu melden hat."""
    db.upsert_beans([{
        "id": BEAN_ID, "name": "Testsorte", "roaster": "Tchibo", "species": None,
        "processing": None, "decaf": 0, "archived": 0, "notes": None,
        "created_at": None, "updated_at": None, "raw_json": "{}",
        "synced_at": "2026-09-14T12:00:00Z",
    }])
    db.upsert_bean_batches([{
        "id": BATCH_ID, "bean_id": BEAN_ID, "roast_date": "2026-01-01",
        "buy_date": None, "freeze_date": None, "unfreeze_date": None,
        "frozen": 0, "archived": 0, "created_at": None, "updated_at": None,
        "raw_json": "{}", "synced_at": "2026-09-14T12:00:00Z",
    }])
    # Der Bezug liegt im Meldefenster der Bewertungsregel, die Charge weit
    # ausserhalb der Altersschwelle - so hat jede Regel etwas zu sagen.
    started = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    detail = decaid_detail("de1app-1785525360", timestamp=started, enjoyment=None)
    detail["workflow"]["context"]["beanBatchId"] = BATCH_ID
    store_shot(db, detail)
    db.link_shots_to_beans()


# ------------------------------------------------------- audit_archive


async def test_audit_finds_the_old_bean(config: Config, db: Database) -> None:
    stocked(db)
    result = await call(build_mcp(config, db), "audit_archive")

    assert result["checked_shots"] == 1
    rules = {f["rule"] for f in result["findings"]}
    assert "bean_age" in rules
    assert "missing_rating" in rules
    assert result["by_rule"]["bean_age"] == 1


async def test_audit_reports_its_thresholds(config: Config, db: Database) -> None:
    """Ein Befund ohne seinen Massstab laesst sich nicht einordnen."""
    stocked(db)
    result = await call(build_mcp(config, db), "audit_archive")
    assert result["thresholds"]["bean_age_warn_days"] == 42
    assert result["active_rules"]


async def test_audit_can_filter_to_one_rule(config: Config, db: Database) -> None:
    stocked(db)
    result = await call(build_mcp(config, db), "audit_archive", {"rule": "bean_age"})
    assert {f["rule"] for f in result["findings"]} == {"bean_age"}


async def test_audit_rejects_an_unknown_rule(config: Config, db: Database) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(config, db), "audit_archive", {"rule": "quatsch"})
    assert "invalid_argument" in str(excinfo.value)
    assert "bean_age" in str(excinfo.value), "die Meldung nennt die gueltigen Regeln"


async def test_audit_respects_since(config: Config, db: Database) -> None:
    stocked(db)
    result = await call(build_mcp(config, db), "audit_archive", {"since": "1d"})
    assert result["checked_shots"] == 0


async def test_switched_off_rules_report_nothing(
    valid_env: dict[str, str], db: Database
) -> None:
    stocked(db)
    quiet = Config.from_env({**valid_env, "GUARD_RULES": "none"})
    result = await call(build_mcp(quiet, db), "audit_archive")
    assert result["active_rules"] == []
    assert result["findings"] == []


async def test_findings_never_carry_notes(config: Config, db: Database) -> None:
    """Befunde koennen per ntfy das Haus verlassen."""
    stocked(db)
    detail = decaid_detail("de1app-1785525999", timestamp="2026-08-02T05:32:50",
                           notes="streng vertraulich", enjoyment=None)
    detail["workflow"]["context"]["beanBatchId"] = BATCH_ID
    store_shot(db, detail)

    result = await call(build_mcp(config, db), "audit_archive")
    assert "streng vertraulich" not in json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------- get_workflow


async def test_get_workflow_reads_live(
    config: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    result = await call(build_mcp(config, db, coordinator), "get_workflow")
    assert result["grinder_setting"] == "3.30"
    assert result["target_dose_g"] == 18.0
    assert result["profile"] == "D-Flow"


async def test_get_workflow_is_absent_without_a_connection(
    config: Config, db: Database
) -> None:
    async with Client(build_mcp(config, db)) as client:
        assert "get_workflow" not in {t.name for t in await client.list_tools()}


# ------------------------------------------------------- Sichtbarkeit


async def test_catalog_writes_need_the_switch(
    config: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    async with Client(build_mcp(config, db, coordinator)) as client:
        names = {t.name for t in await client.list_tools()}
    assert not ({"update_bean", "update_batch", "set_workflow"} & names)


async def test_catalog_writes_appear_with_the_switch(
    writable: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    async with Client(build_mcp(writable, db, coordinator)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("update_bean", "update_batch", "set_workflow"):
        assert name in tools
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False


# ----------------------------------------------------------- update_bean


async def test_update_bean_writes_and_reads_back(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "update_bean",
                        {"id": BEAN_ID, "fields": {"notes": "fruchtig", "decaf": False}})

    assert fake.puts[0][1] == {"notes": "fruchtig", "decaf": False}
    assert result["changes"]["notes"] == {"before": "alt", "after": "fruchtig"}
    # Lokal nachgezogen, damit list_beans sofort stimmt.
    assert db.list_beans()[0]["bean_name"] == "Testsorte"


async def test_update_bean_refuses_unknown_fields(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "update_bean",
                   {"id": BEAN_ID, "fields": {"kaffeegrad": "x"}})
    assert "invalid_argument" in str(excinfo.value)
    assert fake.puts == []


async def test_update_bean_reports_an_unknown_id(
    writable: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "update_bean",
                   {"id": "gibt-es-nicht", "fields": {"notes": "x"}})
    assert "not_found" in str(excinfo.value)


# ---------------------------------------------------------- update_batch


async def test_update_batch_writes_dates(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "update_batch",
                        {"id": BATCH_ID, "fields": {"roastDate": "2026-09-05"}})
    assert fake.puts[0][1]["roastDate"].startswith("2026-09-05")
    assert result["changes"]["roastDate"]["after"].startswith("2026-09-05")
    # Im Archiv steht das Datum ohne Uhrzeit.
    assert db.batch_row(BATCH_ID)["roast_date"] == "2026-09-05"


async def test_freezing_a_batch_goes_through(
    writable: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "update_batch",
                        {"id": BATCH_ID,
                         "fields": {"frozen": True, "freezeDate": "2026-09-10"}})
    assert result["changes"]["frozen"]["after"] is True
    assert db.batch_row(BATCH_ID)["frozen"] == 1


async def test_a_thaw_date_is_refused_with_the_way_out(
    writable: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "update_batch",
                   {"id": BATCH_ID, "fields": {"unfreezeDate": "2026-09-12"}})
    assert "kein Auftaudatum" in str(excinfo.value)


# ---------------------------------------------------------- set_workflow


async def test_set_workflow_changes_the_grind(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "set_workflow",
                        {"fields": {"grinderSetting": "3.10"}})
    assert fake.puts[0][1] == {"context": {"grinderSetting": "3.10"}}
    assert result["changes"]["grinderSetting"] == {"before": "3.30", "after": "3.10"}
    assert "id" not in result, "der Workflow ist einer, er braucht keine Kennung"


async def test_a_profile_change_never_reaches_the_machine(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    """Die einzige Stelle, an der v1 ausdruecklich nein sagt."""
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "set_workflow",
                   {"fields": {"profile": {"title": "anderes"}}})
    assert "an der Maschine" in str(excinfo.value)
    assert fake.puts == []


async def test_an_unchanged_value_is_reported_as_such(
    writable: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "set_workflow",
                        {"fields": {"grinderSetting": "3.30"}})
    assert result["unchanged"] == ["grinderSetting"]
    assert "nicht uebernommen" in result["note"]


async def test_the_log_names_fields_but_never_values(
    writable: Config, db: Database, coordinator: SyncCoordinator, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="decentespresso_mcp.server")
    await call(build_mcp(writable, db, coordinator), "update_bean",
               {"id": BEAN_ID, "fields": {"notes": "streng vertraulich"}})

    records = [r for r in caplog.records if r.getMessage() == "bean updated"]
    assert len(records) == 1
    assert records[0].fields["wrote"] == "notes"
    blob = "\n".join(str(getattr(r, "fields", "")) for r in caplog.records)
    assert "streng vertraulich" not in blob
