"""audit_archive, get_workflow and the catalogue write tools (SPEC §11, §10)."""

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
#: UUID-shaped, because `beanBatchId` is validated as an identifier.
OTHER_BEAN_ID = "2a6388a8-4092-49d1-a815-89d4192562db"
OTHER_BATCH_ID = "0a640616-b680-4a10-8062-c1f9fd892cfe"
MISSING_BATCH_ID = "00000000-0000-4000-8000-000000000000"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeDecaid:
    """Keeps bean, batch and workflow in memory.

    Reproduces what was measured on 2026-09-14: PUT takes the allowed fields,
    refuses protected ones with 400, and returns dates with a time attached.
    """

    def __init__(self) -> None:
        self.beans = [{"id": BEAN_ID, "name": "Testsorte", "roaster": "Tchibo",
                       "species": None, "processing": "washed", "decaf": False,
                       "archived": False, "notes": "alt",
                       "createdAt": None, "updatedAt": None},
                      {"id": OTHER_BEAN_ID, "name": "Sugar Cane Decaf",
                       "roaster": "Rösttrommel", "species": None,
                       "processing": None, "decaf": True, "archived": False,
                       "notes": None, "createdAt": None, "updatedAt": None}]
        self.batches = [{"id": BATCH_ID, "beanId": BEAN_ID,
                         "roastDate": "2026-09-01T00:00:00.000Z",
                         "buyDate": None, "freezeDate": None, "frozen": False,
                         "archived": False, "createdAt": None, "updatedAt": None},
                        {"id": OTHER_BATCH_ID, "beanId": OTHER_BEAN_ID,
                         "roastDate": "2026-09-05T00:00:00.000Z",
                         "buyDate": None, "freezeDate": None, "frozen": False,
                         "archived": False, "createdAt": None,
                         "updatedAt": None}]
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
            return httpx.Response(404, json={"error": "unknown"})

        body = json.loads(request.content)
        self.puts.append((path, body))

        if "/beans/" in path:
            return self._patch(self._by_id(self.beans, path), body)
        if "/bean-batches/" in path:
            for field in ("roastDate", "buyDate", "freezeDate"):
                if isinstance(body.get(field), str):
                    body[field] = body[field] + "T00:00:00.000Z"
            return self._patch(self._by_id(self.batches, path), body)
        if path.endswith("/workflow"):
            if "profile" in body:
                return httpx.Response(400, json={"error": "profile is read-only"})
            self.workflow["context"].update(body.get("context") or {})
            return httpx.Response(200, json=self.workflow)
        return httpx.Response(404, json={"error": "unknown"})

    @staticmethod
    def _by_id(rows: list[dict[str, Any]], path: str) -> dict[str, Any]:
        wanted = path.rstrip("/").rsplit("/", 1)[-1]
        return next(r for r in rows if r["id"] == wanted)

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
    """An archive on which every guard rule has something to report."""
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
    # The shot sits inside the rating rule's window and the batch well beyond
    # the age threshold - so every rule has something to say.
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
    """A finding without its yardstick cannot be placed."""
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
    assert "bean_age" in str(excinfo.value), "the message names the valid rules"


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
    """Findings can leave the house over ntfy."""
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


# --------------------------------------------------------- Visibility


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
    # Updated locally, so list_beans is correct straight away.
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
                   {"id": "does-not-exist", "fields": {"notes": "x"}})
    assert "not_found" in str(excinfo.value)


# ---------------------------------------------------------- update_batch


async def test_update_batch_writes_dates(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "update_batch",
                        {"id": BATCH_ID, "fields": {"roastDate": "2026-09-05"}})
    assert fake.puts[0][1]["roastDate"].startswith("2026-09-05")
    assert result["changes"]["roastDate"]["after"].startswith("2026-09-05")
    # In the archive the date is stored without a time.
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
    assert "no thaw date" in str(excinfo.value)


# ---------------------------------------------------------- set_workflow


async def test_set_workflow_changes_the_grind(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "set_workflow",
                        {"fields": {"grinderSetting": "3.10"}})
    assert fake.puts[0][1] == {"context": {"grinderSetting": "3.10"}}
    assert result["changes"]["grinderSetting"] == {"before": "3.30", "after": "3.10"}
    assert "id" not in result, "there is one workflow, it needs no identifier"


async def test_a_profile_change_never_reaches_the_machine(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    """The one place where v1 explicitly says no."""
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "set_workflow",
                   {"fields": {"profile": {"title": "anderes"}}})
    assert "at the machine" in str(excinfo.value)
    assert fake.puts == []


async def test_a_batch_change_carries_the_coffee_labels(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    """Otherwise the machine keeps showing the previous coffee (SPEC T28).

    Measured on the live instance on 2026-09-15: after `beanBatchId` was moved
    to the decaf batch, `coffeeName` still read "Arabica Honey Process".
    Decaid keeps the managed reference and the two display strings side by
    side and derives neither from the other, so resolving batch -> bean ->
    labels is the client's job. Decaid's own API examples write all three
    together.
    """
    result = await call(build_mcp(writable, db, coordinator), "set_workflow",
                        {"fields": {"beanBatchId": OTHER_BATCH_ID}})

    assert fake.puts[0][1] == {"context": {
        "beanBatchId": OTHER_BATCH_ID,
        "coffeeName": "Sugar Cane Decaf",
        "coffeeRoaster": "Rösttrommel",
    }}
    assert result["changes"]["coffeeName"]["after"] == "Sugar Cane Decaf"
    assert result["alongside"] == ["coffeeName", "coffeeRoaster"]
    assert "without being asked for" in result["note_alongside"]


async def test_an_unknown_batch_is_refused_before_anything_is_written(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    """Decaid accepts any string as a beanBatchId with 200 (SPEC T29).

    There is no referential integrity on the API side, so this refusal is the
    only thing between a typo and a workflow pointing at nothing.
    """
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "set_workflow",
                   {"fields": {"beanBatchId": MISSING_BATCH_ID}})
    assert "still points at the batch it did before" in str(excinfo.value)
    assert fake.puts == [], "nothing may reach the machine on a failed lookup"


async def test_the_coffee_labels_are_not_the_callers_to_set(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    """Setting them by hand is how the machine shows one coffee while pulling
    another - they follow from the batch or not at all."""
    with pytest.raises(ToolError) as excinfo:
        await call(build_mcp(writable, db, coordinator), "set_workflow",
                   {"fields": {"coffeeName": "Something Else"}})
    assert "follows from beanBatchId" in str(excinfo.value)
    assert fake.puts == []


async def test_a_grind_change_leaves_the_labels_alone(
    writable: Config, db: Database, coordinator: SyncCoordinator, fake: FakeDecaid
) -> None:
    """The derivation hangs on beanBatchId, not on every write."""
    result = await call(build_mcp(writable, db, coordinator), "set_workflow",
                        {"fields": {"grinderSetting": "3.10"}})
    assert fake.puts[0][1] == {"context": {"grinderSetting": "3.10"}}
    assert "alongside" not in result


async def test_an_unchanged_value_is_reported_as_such(
    writable: Config, db: Database, coordinator: SyncCoordinator
) -> None:
    result = await call(build_mcp(writable, db, coordinator), "set_workflow",
                        {"fields": {"grinderSetting": "3.30"}})
    assert result["unchanged"] == ["grinderSetting"]
    assert "did not take" in result["note"]


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
