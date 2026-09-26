"""Recipes (SPEC §11.6): ours in SQLite, DYE2's read from its store.

The stand-in is the onboarding one (test_onboarding.FakeDecaid), which models
what the live instance does with profiles (T37-T39), extended by the plugin
store. The workflow fixture is the live one from 2026-09-26: a D-Flow tuned on
the tablet and stored nowhere, on a Grano Gayo batch.
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
from test_onboarding import WORKFLOW, FakeDecaid, _no_sleep

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_client import DecaidClient
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import SyncCoordinator

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
GAYO_BATCH = WORKFLOW["context"]["beanBatchId"]
GAYO_BEAN = "33333333-3333-4333-8333-333333333333"


class StoreFake(FakeDecaid):
    """FakeDecaid plus the plugin store and the workflow's own batch."""

    def __init__(self, *, store: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.beans.append({"id": GAYO_BEAN, "name": WORKFLOW["context"]["coffeeName"],
                           "roaster": WORKFLOW["context"]["coffeeRoaster"],
                           "decaf": False, "archived": False})
        self.batches.append({"id": GAYO_BATCH, "beanId": GAYO_BEAN,
                             "frozen": False, "archived": False})
        self.store = store or {}
        self.offline = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("tablet off")
        path = request.url.path
        if path.startswith("/api/v1/store/"):
            self.calls.append((request.method, path))
            if request.method != "GET":
                return httpx.Response(500, json={"error": "a test stand-in: never write"})
            key = "/".join(path.split("/")[4:6])
            # The literal `null` for a key never written, as the contract says -
            # json=None would send an empty body instead, which Decaid does not.
            return httpx.Response(200, content=json.dumps(self.store.get(key)),
                                  headers={"Content-Type": "application/json"})
        return super().handler(request)

    def store_writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if p.startswith("/api/v1/store/") and m != "GET"]


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "recipes.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def writable(valid_env: dict[str, str]) -> Config:
    return Config.from_env({**valid_env, "WRITE_ENABLED": "true"})


def server(fake: FakeDecaid, config: Config, db: Database):
    client = DecaidClient("http://10.100.100.171:8080",
                          transport=httpx.MockTransport(fake.handler))
    client._sleep_backoff = _no_sleep  # type: ignore[method-assign]
    return build_mcp(config, db, SyncCoordinator(client, db))


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


async def refused(mcp, name: str, args: dict | None = None) -> str:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, name, args)
    return str(excinfo.value)


async def saved(fake: StoreFake, mcp) -> dict[str, Any]:
    """The live situation resolved: the tuned profile kept, then the recipe."""
    await call(mcp, "save_workflow_profile", {})
    return (await call(mcp, "save_recipe", {}))["recipe"]


# ---------------------------------------------------- Keeping the tune


async def test_the_tuned_profile_is_kept_under_the_coffees_name(writable, db) -> None:
    fake = StoreFake()
    result = await call(server(fake, writable, db), "save_workflow_profile", {})

    assert result["title"] == "Coffee Circle – Grano Gayo"
    assert result["was_titled_in_workflow"] == "D-Flow"
    assert fake.workflow["profile"]["title"] == "Coffee Circle – Grano Gayo", (
        "the workflow carries the stored copy - same brew, now with its name")
    stored = next(r for r in fake.profiles if r["id"] == result["id"])
    assert stored["metadata"]["createdBy"] == "decentespresso-mcp"


async def test_content_already_stored_is_named_not_duplicated(writable, db) -> None:
    """T39: Decaid would answer 201 with the old record and drop the title."""
    fake = StoreFake(workflow_profile=next(
        r for r in fake_profiles() if r["visibility"] == "visible"
        and r["profile"]["title"] == "D-Flow")["profile"])
    message = await refused(server(fake, writable, db), "save_workflow_profile",
                            {"title": "Mine"})
    assert "already stored as 'D-Flow'" in message
    assert fake.posts() == []


def fake_profiles() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "profiles_sample.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------- Saving


async def test_a_recipe_never_points_at_a_profile_that_lives_only_in_the_workflow(
    writable, db
) -> None:
    fake = StoreFake()
    message = await refused(server(fake, writable, db), "save_recipe", {})
    assert "save_workflow_profile" in message
    assert "Coffee Circle – Grano Gayo" in message, "offers the title too"
    assert db.recipes() == []


async def test_a_recipe_captures_what_the_machine_is_set_to(writable, db) -> None:
    fake = StoreFake()
    recipe = await saved(fake, server(fake, writable, db))
    context = WORKFLOW["context"]

    assert recipe["name"] == "Coffee Circle – Grano Gayo"
    assert recipe["beanBatchId"] == GAYO_BATCH
    assert recipe["profileTitle"] == "Coffee Circle – Grano Gayo"
    assert recipe["dashboardVariables"]["grind"] == context["grinderSetting"]
    assert recipe["dashboardVariables"]["dose"] == context["targetDoseWeight"]
    assert recipe["dashboardVariables"]["drink"] == context["targetYield"]
    assert db.recipes()[0]["bean_id"] == GAYO_BEAN


async def test_a_taken_recipe_name_is_refused_with_a_suggestion(writable, db) -> None:
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    message = await refused(mcp, "save_recipe", {"name": "coffee circle – grano  gayo"})
    assert "(2)" in message


# --------------------------------------------------------- Applying


async def test_apply_puts_the_machine_back_field_for_field(writable, db) -> None:
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    wanted = copy.deepcopy(fake.workflow)

    fake.workflow["context"].update({"grinderSetting": "9.9", "targetDoseWeight": 20.0,
                                     "targetYield": 50.0})
    fake.workflow["profile"] = fake_profiles()[2]["profile"]      # Adaptive v3
    result = await call(mcp, "apply_recipe", {"name": "Coffee Circle – Grano Gayo"})

    for key in ("beanBatchId", "grinderSetting", "targetDoseWeight", "targetYield",
                "coffeeName", "coffeeRoaster"):
        assert fake.workflow["context"][key] == wanted["context"][key], key
    assert fake.workflow["profile"]["steps"] == wanted["profile"]["steps"]
    assert result["profile"]["pinned"] is False


async def test_an_unpinned_recipe_follows_the_tuning_and_a_pinned_one_stays(
    writable, db
) -> None:
    """The reason the profile is held by title: its id changes with every tune."""
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    await call(mcp, "save_recipe", {"name": "Pinned", "pin_profile": True})
    before = copy.deepcopy(fake.workflow["profile"])

    tuned = await call(mcp, "update_profile", {
        "id": "Coffee Circle – Grano Gayo", "overrides": {"temperature_c": 90}})
    assert tuned["recipes"] == {"follow_the_change": ["Coffee Circle – Grano Gayo"],
                                "pinned_to_the_old_version": ["Pinned"]}

    followed = await call(mcp, "apply_recipe", {"name": "Coffee Circle – Grano Gayo"})
    assert followed["profile"]["id"] == tuned["id"]
    assert fake.workflow["profile"]["steps"][-1]["temperature"] == 90.0

    await call(mcp, "apply_recipe", {"name": "Pinned"})
    assert fake.workflow["profile"]["steps"] == before["steps"], "the snapshot, untouched"


async def test_refreshing_a_pinned_snapshot_takes_the_current_version(writable, db) -> None:
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    await call(mcp, "save_recipe", {"name": "Pinned", "pin_profile": True})
    await call(mcp, "update_profile", {"id": "Coffee Circle – Grano Gayo",
                                       "overrides": {"temperature_c": 90}})
    await call(mcp, "update_recipe", {"name": "Pinned", "fields": {"refresh_snapshot": True}})
    await call(mcp, "apply_recipe", {"name": "Pinned"})
    assert fake.workflow["profile"]["steps"][-1]["temperature"] == 90.0


async def test_a_vanished_profile_is_not_replaced_by_its_snapshot(writable, db) -> None:
    """The user asked for the profile as it is now; an old copy answers another question."""
    fake = StoreFake()
    mcp = server(fake, writable, db)
    recipe = await saved(fake, mcp)
    fake.profiles = [r for r in fake.profiles
                     if r["profile"]["title"] != recipe["profileTitle"]]
    message = await refused(mcp, "apply_recipe", {"name": recipe["name"]})
    assert "no longer on the tablet" in message
    assert "Pin the recipe" in message


async def test_applying_over_an_unsaved_profile_is_refused(writable, db) -> None:
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    fake.workflow["profile"] = dict(fake.workflow["profile"], target_weight=41.5,
                                    title="tuned again, not saved")
    message = await refused(mcp, "apply_recipe", {"name": "Coffee Circle – Grano Gayo"})
    assert "stored nowhere" in message or "not saved as a profile" in message


# ------------------------------------------------------ Maintaining


async def test_update_and_delete_are_for_mine_only(writable, db) -> None:
    fake = StoreFake()
    mcp = server(fake, writable, db)
    recipe = await saved(fake, mcp)
    renamed = await call(mcp, "update_recipe", {"name": recipe["name"],
                                                "fields": {"name": "House", "grind": "3.1"}})
    assert renamed["recipe"]["name"] == "House"
    assert renamed["recipe"]["dashboardVariables"]["grind"] == "3.1"

    assert "not changeable" in await refused(mcp, "update_recipe", {
        "name": "House", "fields": {"beanBatchId": "x"}})
    assert (await call(mcp, "delete_recipe", {"name": "house"}))["deleted"] == "house"
    assert "DYE2's are deleted in DYE2" in await refused(mcp, "delete_recipe",
                                                          {"name": "House"})


async def test_with_the_tablet_off_mine_are_still_listed(writable, db) -> None:
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    fake.offline = True
    listing = await call(mcp, "list_recipes", {})
    assert [r["source"] for r in listing["recipes"]] == ["mine"]
    assert "waiting_for_tablet" in listing["dye2"]


async def test_the_recipe_tools_that_write_need_write_mode(config, db) -> None:
    async with Client(server(StoreFake(), config, db)) as client:
        names = {t.name for t in await client.list_tools()}
    assert "list_recipes" in names
    assert not names & {"save_recipe", "apply_recipe", "update_recipe", "delete_recipe",
                        "save_workflow_profile"}


# --------------------------------------------------------------- DYE2's

DYE2 = json.loads((FIXTURES / "dye2_recipes.json").read_text(encoding="utf-8"))
SENIMAN = json.loads((FIXTURES / "dye2_favourite_seniman.json").read_text(encoding="utf-8"))


def with_dye2(**kwargs: Any) -> StoreFake:
    return StoreFake(store={"dye2.reaplugin/recipes": copy.deepcopy(DYE2)}, **kwargs)


async def test_dye2_recipes_are_listed_in_the_shared_shape(writable, db) -> None:
    """The live store held one recipe, 'Decaf', on 2026-09-26."""
    listing = await call(server(with_dye2(), writable, db), "list_recipes",
                         {"source": "dye2"})
    (decaf,) = listing["recipes"]
    assert decaf["source"] == "dye2"
    assert decaf["name"] == "Decaf"
    assert decaf["dashboardVariables"]["dose"] == 18
    assert decaf["applicable"] is True


async def test_a_never_written_key_reads_as_empty(writable, db) -> None:
    """KV contract: 200 with a literal null, to be read as []."""
    listing = await call(server(StoreFake(), writable, db), "list_recipes",
                         {"source": "dye2"})
    assert listing["recipes"] == []


async def test_a_dye2_recipe_is_applied_exactly_as_stored(writable, db) -> None:
    """The contract's apply: its `workflow`, PUT unchanged - nothing added."""
    fake = with_dye2()
    sent: list[dict[str, Any]] = []
    original = fake._route

    def spy(method: str, path: str, body: Any) -> httpx.Response:
        if method == "PUT" and path == "/api/v1/workflow":
            sent.append(copy.deepcopy(body))
        return original(method, path, body)

    fake._route = spy  # type: ignore[method-assign]
    await call(server(fake, writable, db), "apply_recipe", {"name": "Decaf"})
    assert sent == [DYE2[0]["workflow"]]


async def test_a_dye2_recipe_that_mislabels_the_coffee_says_so(writable, db) -> None:
    """Measured (T42): DYE2's 'Decaf' sets coffeeName and neither the roaster
    nor the batch. Applied unchanged on a Grano Gayo batch, the machine would
    call Coffee Circle's batch "Sugar Cane Decaf". Unchanged is what the
    contract asks for, so it is applied as it is - and the mismatch reported,
    not left for the next shot to be labelled wrongly."""
    fake = with_dye2()
    result = await call(server(fake, writable, db), "apply_recipe", {"name": "Decaf"})
    assert "Sugar Cane Decaf" in result["inconsistent"]
    assert "set_workflow" in result["inconsistent"]


async def test_nothing_here_ever_writes_dye2s_store(writable, db) -> None:
    """The single-writer rule of the KV contract, checked across a whole session."""
    fake = with_dye2()
    mcp = server(fake, writable, db)
    await call(mcp, "list_recipes", {})
    await call(mcp, "apply_recipe", {"name": "Decaf"})
    mine = await saved(fake, mcp)
    await call(mcp, "apply_recipe", {"name": mine["name"], "source": "mine"})
    assert fake.store_writes() == []
    assert "Decaf" not in {r["name"] for r in db.recipes()}


async def test_a_name_in_both_sources_needs_the_source(writable, db) -> None:
    fake = with_dye2()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    await call(mcp, "update_recipe", {"name": "Coffee Circle – Grano Gayo",
                                      "fields": {"name": "Decaf"}})
    message = await refused(mcp, "apply_recipe", {"name": "decaf"})
    assert "source='mine' or source='dye2'" in message
    both = [r["source"] for r in (await call(mcp, "list_recipes", {}))["recipes"]
            if r["name"] == "Decaf"]
    assert sorted(both) == ["dye2", "mine"]


def test_a_legacy_dye2_recipe_is_listed_but_not_applied() -> None:
    from decentespresso_mcp.recipes import dye2_view
    legacy = {k: v for k, v in DYE2[0].items() if k != "workflow"}
    view = dye2_view(legacy)
    assert view["applicable"] is False
    assert "older DYE2" in view["why_not"]


def test_a_dye2_profile_can_be_a_name_without_a_profile() -> None:
    """T44, measured on the live store: "Seniman House Blend" keeps
    `profile: {id: null, title: "D-Flow"}` - no steps, no id. PUT as stored it
    renames whatever is running. An earlier reading of this data compared these
    stubs with each other, found them "identical and stored nowhere", and drew
    a wrong conclusion from it; the stub is the finding."""
    from decentespresso_mcp.recipes import profile_reference_only
    assert set(SENIMAN["workflow"]["profile"]) == {"id", "title"}
    assert SENIMAN["workflow"]["profile"]["id"] is None
    assert "keeps brewing on the profile it was already running" in (
        profile_reference_only(SENIMAN["workflow"]))
    assert profile_reference_only(DYE2[0]["workflow"]) is None, "no profile at all"
    assert profile_reference_only({"profile": fake_profiles()[0]["profile"]}) is None


async def test_applying_a_profile_stub_says_what_it_did(writable, db) -> None:
    item = dict(copy.deepcopy(DYE2[0]), workflow=copy.deepcopy(SENIMAN["workflow"]))
    fake = StoreFake(store={"dye2.reaplugin/recipes": [item]})
    result = await call(server(fake, writable, db), "apply_recipe", {"name": "Decaf"})
    assert "only as the name 'D-Flow'" in result["profile_note"]
