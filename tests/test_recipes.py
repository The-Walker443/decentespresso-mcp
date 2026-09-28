"""Recipes (SPEC §11.6): a bean's most recent real shot, as Beanie restores it.

The rule is Beanie's (github.com/giladger/Beanie at a54a625): newest non-service
shot of the bean, newest usable batch by roast date, planned dose and yield, the
stored profile of the shot's title at the shot's brew temperature. The shot is
the real one of 2026-09-14 (tests/fixtures/decaid/shot_detail.json); the
stand-in is the onboarding one, which models what the live instance does with
profiles and the workflow.
"""

from __future__ import annotations

import copy
import json
import pathlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import decaid_detail, store_shot
from test_onboarding import BEAN_ID, PROFILES, FakeDecaid, _no_sleep

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database, default_migrations_dir
from decentespresso_mcp.decaid_client import DecaidClient
from decentespresso_mcp.decaid_mapping import batch_row_from_decaid, bean_row_from_decaid
from decentespresso_mcp.recipes import (
    base_temperature,
    recipe_batch,
    recipe_shot,
    recipe_workflow,
)
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import SyncCoordinator

SHOT = decaid_detail()
BATCH = SHOT["workflow"]["context"]["beanBatchId"]
BEAN = {"id": BEAN_ID, "name": "Arabica Honey Process", "roaster": "Tugu Kawisari"}
STORED_DFLOW = next(r for r in PROFILES if r["profile"]["title"] == "D-Flow"
                    and r.get("visibility") == "visible")


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "recipes.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def writable(valid_env: dict[str, str]) -> Config:
    return Config.from_env({**valid_env, "WRITE_ENABLED": "true"})


def tablet() -> FakeDecaid:
    fake = FakeDecaid()
    fake.batches.append({"id": BATCH, "beanId": BEAN_ID, "roastDate": "2026-09-01T00:00:00.000Z",
                         "frozen": False, "archived": False})
    return fake


def archived(db: Database, fake: FakeDecaid, *shots: dict[str, Any]) -> None:
    db.upsert_beans([bean_row_from_decaid(b, "x") for b in fake.beans])
    db.upsert_bean_batches([batch_row_from_decaid(b, "x") for b in fake.batches])
    for shot in shots or (SHOT,):
        store_shot(db, shot)
    db.link_shots_to_beans()


def server(fake: FakeDecaid, config: Config, db: Database):
    client = DecaidClient("http://10.100.100.171:8080",
                          transport=httpx.MockTransport(fake.handler))
    client._sleep_backoff = _no_sleep  # type: ignore[method-assign]
    return build_mcp(config, db, SyncCoordinator(client, db, config))


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


async def refused(mcp, name: str, args: dict | None = None) -> str:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, name, args)
    return str(excinfo.value)


def service(shot: dict[str, Any], beverage: str) -> dict[str, Any]:
    out = copy.deepcopy(shot)
    out["workflow"]["profile"]["beverage_type"] = beverage
    return out


# ------------------------------------------------------------- The rule


def test_the_recipe_shot_skips_service_shots() -> None:
    """Beanie's isServiceShot: by the profile's beverage type or the context's
    finalBeverageType - a flush pulled last must not become the recipe."""
    flush = service(SHOT, "cleaning")
    steam = copy.deepcopy(SHOT)
    steam["workflow"]["context"]["finalBeverageType"] = "Steam"
    assert recipe_shot([flush, steam, SHOT]) is SHOT
    assert recipe_shot([flush]) is None


def test_the_batch_is_the_newest_usable_one_not_the_shots() -> None:
    """Beanie picks latestBatch(usable) by roast date; a bag under 5 g left is
    used up. Only when all are, the newest of all."""
    old = {"id": "old", "roastDate": "2026-08-01"}
    new = {"id": "new", "roastDate": "2026-09-20"}
    empty = {"id": "empty", "roastDate": "2026-09-25", "weightRemaining": 2}
    assert recipe_batch([old, empty, new])["id"] == "new"
    assert recipe_batch([empty])["id"] == "empty"
    assert recipe_batch([]) is None


def test_planned_dose_and_yield_come_back_not_the_measured_ones() -> None:
    """Beanie loads 'planned': repeat what was aimed for, not one pour's noise
    (18 -> 42 g planned, 41.6 g measured on the real shot)."""
    body = recipe_workflow(BEAN, {"id": BATCH}, SHOT, [])
    context = body["context"]
    assert (context["targetDoseWeight"], context["targetYield"]) == (18.0, 42.0)
    assert context["beanBatchId"] == BATCH and context["beanId"] == BEAN_ID
    assert context["coffeeName"] == "Arabica Honey Process"
    assert context["grinderSetting"] == "2.70"
    assert context["finalBeverageType"] == "espresso"


def test_a_missing_plan_falls_back_to_the_measurement() -> None:
    shot = copy.deepcopy(SHOT)
    shot["workflow"]["context"]["targetYield"] = 0
    assert recipe_workflow(BEAN, None, shot, [])["context"]["targetYield"] == 41.6


def test_the_stored_profile_of_that_title_runs_at_the_shots_temperature() -> None:
    """Beanie's normalizeDraft swaps the embedded profile for the stored one of
    the same title, then applies the shot's brew temperature to it. So a
    tablet tune under a stock title comes back as the stock profile - Beanie's
    behaviour, matched rather than improved on, so chat and tablet agree."""
    body = recipe_workflow(BEAN, None, SHOT, [STORED_DFLOW])
    shot_temperature = base_temperature(SHOT["workflow"]["profile"])
    assert base_temperature(body["profile"]) == shot_temperature
    stripped = [{k: v for k, v in s.items() if k != "temperature"}
                for s in body["profile"]["steps"]]
    stored = [{k: v for k, v in s.items() if k != "temperature"}
              for s in STORED_DFLOW["profile"]["steps"]]
    assert stripped == stored


def test_without_a_stored_profile_the_shots_own_is_sent_unchanged() -> None:
    body = recipe_workflow(BEAN, None, SHOT, [])
    assert body["profile"] == SHOT["workflow"]["profile"]


def test_a_profile_without_weight_stop_arms_none() -> None:
    """Beanie: target_weight 0 means stop-at-weight off, sent as targetYield 0."""
    shot = copy.deepcopy(SHOT)
    shot["workflow"]["profile"]["target_weight"] = 0
    assert recipe_workflow(BEAN, None, shot, [])["context"]["targetYield"] == 0


# ------------------------------------------------------------- The tools


async def test_apply_sets_the_machine_to_the_beans_last_real_shot(writable, db) -> None:
    fake = tablet()
    archived(db, fake, service(decaid_detail("aaaa1111-0000-4000-8000-000000000009",
                                             timestamp="2026-09-15T07:50:12"), "cleaning"))
    store_shot(db, SHOT)
    db.link_shots_to_beans()
    result = await call(server(fake, writable, db), "apply_recipe", {"bean": "honey"})

    assert result["name"] == "Tugu Kawisari – Arabica Honey Process"
    assert result["from"]["shot"] == SHOT["id"], "the flush after it is no recipe"
    context = fake.workflow["context"]
    assert context["beanBatchId"] == BATCH
    assert context["grinderSetting"] == "2.70"
    assert (context["targetDoseWeight"], context["targetYield"]) == (18.0, 42.0)
    assert fake.workflow["profile"]["title"] == "D-Flow"
    assert "machine" in result, "the upload watch of §11.7 runs on it"


async def test_a_bean_without_a_real_shot_points_at_set_workflow(writable, db) -> None:
    fake = tablet()
    archived(db, fake, service(SHOT, "cleaning"))
    message = await refused(server(fake, writable, db), "apply_recipe", {"bean": "honey"})
    assert "no_recipe" in message and "set_workflow" in message


async def test_an_ambiguous_name_is_not_guessed(writable, db) -> None:
    fake = tablet()
    fake.beans.append({"id": "b2", "name": "Honey Espresso", "roaster": "Other"})
    archived(db, fake)
    message = await refused(server(fake, writable, db), "apply_recipe", {"bean": "honey"})
    assert "2 beans match" in message


async def test_the_listing_is_each_beans_last_real_shot(writable, db) -> None:
    fake = tablet()
    archived(db, fake, SHOT, service(decaid_detail("aaaa1111-0000-4000-8000-000000000009",
                                                   timestamp="2026-09-15T07:50:12"), "cleaning"))
    (recipe,) = (await call(server(fake, writable, db), "list_recipes"))["recipes"]
    assert recipe["shot_at"] == SHOT["timestamp"]
    assert (recipe["grinder_setting"], recipe["dose_g"], recipe["yield_g"]) == ("2.70", 18.0, 42.0)
    assert recipe["profile"] == "D-Flow"
    assert recipe["bean_age_days"] == 13, "roasted 1 Sep, pulled 14 Sep"


async def test_nothing_writes_the_plugin_store_any_more(writable, db) -> None:
    """0.14 is back to DYE2's contract: the server never writes the KV store."""
    fake = tablet()
    archived(db, fake)
    mcp = server(fake, writable, db)
    await call(mcp, "list_recipes")
    await call(mcp, "apply_recipe", {"bean": BEAN_ID})
    await call(mcp, "set_workflow", {"fields": {"grinderSetting": "2.8"}})
    assert [c for c in fake.calls if c[1].startswith("/api/v1/store/")] == []


async def test_saving_and_deleting_recipes_is_gone(writable, config, db) -> None:
    async with Client(server(tablet(), writable, db)) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"list_recipes", "apply_recipe", "save_workflow_profile"} <= names
    assert not names & {"save_recipe", "delete_recipe", "update_recipe"}
    async with Client(server(tablet(), config, db)) as client:
        names = {t.name for t in await client.list_tools()}
    assert "list_recipes" in names and "apply_recipe" not in names


# ------------------------------------------------------ Keeping the tune


async def test_the_tuned_profile_is_kept_under_the_coffees_name(writable, db) -> None:
    fake = tablet()
    result = await call(server(fake, writable, db), "save_workflow_profile", {})
    assert result["was_titled_in_workflow"] == "D-Flow"
    stored = next(r for r in fake.profiles if r["id"] == result["id"])
    assert stored["metadata"]["createdBy"] == "decentespresso-mcp"


async def test_content_already_stored_is_named_not_duplicated(writable, db) -> None:
    """T39: Decaid would answer 201 with the old record and drop the title."""
    fake = FakeDecaid(workflow_profile=STORED_DFLOW["profile"])
    message = await refused(server(fake, writable, db), "save_workflow_profile",
                            {"title": "Mine"})
    assert "already stored as 'D-Flow'" in message


# ------------------------------------------------------------ Migration


def test_the_stored_recipes_of_013_are_dropped(tmp_path: pathlib.Path) -> None:
    """Migration 007: bean_recipes held nothing the shots do not - it goes."""
    import sqlite3
    before = tmp_path / "migrations"
    before.mkdir()
    for sql in default_migrations_dir().glob("*.sql"):
        if sql.name < "007":
            (before / sql.name).write_text(sql.read_text(encoding="utf-8"), encoding="utf-8")
    path = tmp_path / "old.db"
    old = Database(path, before)
    old.migrate()
    old.close()
    new = Database(path)
    assert new.migrate()[0] == "007_drop_bean_recipes.sql"
    new.close()
    tables = {r[0] for r in sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bean_recipes" not in tables
    assert json  # the fixture module stays imported for the rule tests
