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


# ------------------------------------------------------ DYE2 favourites

FAVS = json.loads((FIXTURES / "dye2_favourites.json").read_text(encoding="utf-8"))
SENIMAN_BATCH = "140c8857-55a9-44c6-87f1-a381af1bf85f"
YIRGA_BEAN = "936b19ea-fb28-462f-b4f8-3753de6715c5"
YIRGA_BATCH = "44444444-4444-4444-8444-444444444444"


class FavFake(StoreFake):
    """StoreFake with the live favourites and the beans and batches they name."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(store={"dye2.reaplugin/autoFavourites": copy.deepcopy(FAVS),
                                "dye2.reaplugin/recipes": copy.deepcopy(DYE2)}, **kwargs)
        self.beans += [
            {"id": "55555555-5555-4555-8555-555555555555", "name": "House Blend",
             "roaster": "Seniman", "decaf": False, "archived": False},
            {"id": YIRGA_BEAN, "name": "Coffee Circle Yirga Santos",
             "roaster": "Coffee Circle", "decaf": False, "archived": False},
            {"id": "a705e101-7572-4b62-9514-f3e9245faa20", "name": "Sugar Cane Decaf",
             "roaster": "Rösttrommel", "decaf": False, "archived": False},
        ]
        self.batches += [
            {"id": SENIMAN_BATCH, "beanId": "55555555-5555-4555-8555-555555555555",
             "frozen": False, "archived": False},
            {"id": YIRGA_BATCH, "beanId": YIRGA_BEAN, "roastDate": "2026-09-01T00:00:00.000Z",
             "frozen": False, "archived": False},
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/v1/beans/") \
                and path.endswith("/batches"):
            bean_id = path.split("/")[4]
            return httpx.Response(200, json=[b for b in self.batches
                                             if b["beanId"] == bean_id])
        return super().handler(request)


def favourite(title: str) -> dict[str, Any]:
    return next(f for f in FAVS if f.get("title") == title)


async def test_favourites_are_listed_with_the_contracts_fallbacks(writable, db) -> None:
    """Eight on the live store: three titled, five derived as DYE2 does."""
    listing = (await call(server(FavFake(), writable, db), "list_recipes",
                          {"source": "dye2_favs"}))["recipes"]
    names = [f["name"] for f in listing]
    assert names[:3] == ["Seniman House Blend", "RT Decaf", "Decaf"]
    assert names.count("Coffee Circle · Coffee Circle Yirga Santos") == 5
    kinds = sorted(f["profile"] for f in listing)
    assert kinds == ["name"] * 7 + ["reference"], "T44, as measured"
    assert "barista" in listing[0]["maskedOff"]


async def test_a_name_only_profile_is_not_applied_and_says_so(writable, db) -> None:
    """The brief's T44 guard, with the live Seniman House Blend.

    Its profile is {"id": null, "title": "D-Flow"}. PUT as DYE2 does, it would
    rename whatever is running; here the context goes on and the profile stays.
    """
    fake = FavFake()
    before = copy.deepcopy(fake.workflow["profile"])
    result = await call(server(fake, writable, db), "apply_recipe",
                        {"name": "Seniman House Blend"})

    assert fake.workflow["profile"] == before, "not renamed, not touched"
    assert result["profile"]["note"].startswith(
        "favourite carries a name-only profile ('D-Flow'); keeping the current "
        "profile untouched")
    context = fake.workflow["context"]
    wanted = favourite("Seniman House Blend")["workflow"]["context"]
    assert context["beanBatchId"] == SENIMAN_BATCH
    assert context["grinderSetting"] == wanted["grinderSetting"]
    assert context["coffeeRoaster"] == "Seniman"


async def test_a_reference_is_resolved_and_sent_in_full(writable, db) -> None:
    """The one live favourite with an id: Decaid drops the id and keeps the old
    steps (measured), so the stub alone would select nothing."""
    fake = FavFake(workflow_profile=PROFILES_BY_TITLE["Adaptive v3"])
    ref = next(f for f in FAVS if (f["workflow"]["profile"] or {}).get("id"))
    fake.store["dye2.reaplugin/autoFavourites"] = [dict(copy.deepcopy(ref),
                                                        title="With a reference")]
    fake.store["dye2.reaplugin/autoFavourites"][0]["workflow"]["context"]["beanBatchId"] = \
        YIRGA_BATCH
    result = await call(server(fake, writable, db), "apply_recipe",
                        {"name": "With a reference"})
    record = next(r for r in fake_profiles() if r["id"] == ref["workflow"]["profile"]["id"])
    assert fake.workflow["profile"]["steps"] == record["profile"]["steps"]
    assert result["profile"]["kind"] == "reference"
    assert "resolved and sent in full" in result["profile"]["note"]


async def test_a_bean_id_where_a_batch_belongs_is_refused_with_the_batches(
    writable, db
) -> None:
    """T45: six of eight live favourites store a bean's id as beanBatchId.

    Decaid would take it (T29); every shot afterwards would point at a batch
    that does not exist. The bean's batches are named instead of one guessed.
    """
    fake = FavFake()
    message = await refused(server(fake, writable, db), "apply_recipe",
                            {"name": favourite("RT Decaf")["id"]})
    assert "stores the id of the bean 'Sugar Cane Decaf'" in message
    assert fake.puts() == []

    message = await refused(server(fake, writable, db), "apply_recipe",
                            {"name": next(f["id"] for f in FAVS if not f.get("title"))})
    assert YIRGA_BATCH in message, "names the bean's batch"


async def test_the_copymask_is_honoured(writable, db) -> None:
    """A group masked off is not sent, whatever the stored workflow carries."""
    from decentespresso_mcp.recipes import favourite_workflow
    fav = copy.deepcopy(favourite("Seniman House Blend"))
    fav["copyMask"]["grindSetting"] = False
    fav["copyMask"]["dose"] = False
    context, _, off = favourite_workflow(fav)
    assert "grinderSetting" not in context
    assert "targetDoseWeight" not in context
    assert {"grindSetting", "dose"} <= set(off)
    assert context["beanBatchId"] == SENIMAN_BATCH, "the rest still goes"


def test_a_legacy_favourite_is_derived_from_its_snapshot() -> None:
    """No `workflow`: the contract's legacy path, mapped as DYE2's builder does."""
    from decentespresso_mcp.recipes import favourite_workflow
    fav = {k: v for k, v in copy.deepcopy(favourite("Seniman House Blend")).items()
           if k != "workflow"}
    context, profile, _ = favourite_workflow(fav)
    snap = fav["snapshot"]
    assert context["targetDoseWeight"] == snap["dose"]
    assert context["grinderSetting"] == str(snap["grindSetting"])
    assert profile == {"id": snap.get("profileId"), "title": snap.get("profileTitle")}


async def test_five_favourites_with_one_label_are_named_by_id(writable, db) -> None:
    fake = FavFake()
    message = await refused(server(fake, writable, db), "apply_recipe",
                            {"name": "coffee circle · coffee circle yirga santos"})
    assert "5 entries in dye2_favs" in message
    assert "name one by its id" in message


async def test_favourites_are_never_written_either(writable, db) -> None:
    fake = FavFake()
    mcp = server(fake, writable, db)
    await call(mcp, "list_recipes", {})
    await call(mcp, "apply_recipe", {"name": "Seniman House Blend"})
    assert fake.store_writes() == []


PROFILES_BY_TITLE = {r["profile"]["title"]: r["profile"] for r in fake_profiles()
                     if r["visibility"] == "visible"}


# ------------------------------------------------------ Issues, duplicates

YIRGA = "Coffee Circle · Coffee Circle Yirga Santos"
YIRGA_TWINS = ["83208701-b188-4068-a8d1-899d96cded16", "4ed2bb1b-c361-472d-bdab-ef71cd874959",
               "1fe127b1-04fc-4102-b0fe-ff3962e03c6f", "0b097e5f-258c-46bf-8fa9-c444e88691ed"]


async def listed(fake: StoreFake, config: Config, db: Database) -> dict[str, dict[str, Any]]:
    out = (await call(server(fake, config, db), "list_recipes", {}))["recipes"]
    return {str(r.get("id") or r["name"]): r for r in out}


async def test_broken_favourites_are_flagged_before_anyone_applies_them(writable, db) -> None:
    """The live store as measured on 2026-09-26: without these flags a listing
    shows eight usable favourites, and applying most of them either leaves the
    profile alone (T44) or points the machine at a batch that does not exist (T45).
    """
    entries = await listed(FavFake(), writable, db)
    assert entries["af-1789560226867"]["issues"] == ["name_only_profile"]
    assert entries["af-1789560386859"]["issues"] == ["name_only_profile", "batch_is_bean_id"]
    reference = entries["a3f6c671-7313-46c1-9cef-db8ef7210385"]
    assert "batch_is_bean_id" in reference["issues"]
    assert "name_only_profile" not in reference["issues"], "its profile is a real reference"
    assert entries["1"]["issues"] == ["labels_without_batch"], "T42"


async def test_identical_favourites_are_marked_and_point_at_each_other(writable, db) -> None:
    """Four of the five Yirga Santos favourites are the same values under the
    same label; the fifth differs (reference profile, no rpm) and must not be
    reported as a copy, or deleting "the duplicates" would lose it.
    """
    entries = await listed(FavFake(), writable, db)
    for twin in YIRGA_TWINS:
        assert "duplicate" in entries[twin]["issues"]
        assert sorted(entries[twin]["duplicate_of"]) == sorted(set(YIRGA_TWINS) - {twin})
    fifth = entries["a3f6c671-7313-46c1-9cef-db8ef7210385"]
    assert "name_not_unique" in fifth["issues"]
    assert "duplicate" not in fifth["issues"] and "duplicate_of" not in fifth


async def test_a_sound_entry_carries_no_issues_key(writable, db) -> None:
    """The key only appears when something is wrong, so its absence reads as fine."""
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    (mine,) = (await call(mcp, "list_recipes", {}))["recipes"]
    assert "issues" not in mine


async def test_duplicates_are_counted_within_a_source_only(writable, db) -> None:
    """A DYE2 favourite and a DYE2 recipe with one name are two different things."""
    fake = FavFake()
    entries = (await call(server(fake, writable, db), "list_recipes", {}))["recipes"]
    decafs = [r for r in entries if r["name"] == "Decaf"]
    assert {r["source"] for r in decafs} == {"dye2", "dye2_favs"}
    for entry in decafs:
        assert not {"duplicate", "name_not_unique"} & set(entry.get("issues", []))


async def test_with_the_tablet_off_only_what_the_entry_shows_is_judged(writable, db) -> None:
    """No catalogue, no guessing: a batch cannot be called unknown unseen."""
    from decentespresso_mcp.recipes import issues_of
    rt_decaf = favourite("RT Decaf")
    assert issues_of("dye2_favs", rt_decaf, None) == ["name_only_profile"]
    fake = StoreFake()
    mcp = server(fake, writable, db)
    await saved(fake, mcp)
    fake.offline = True
    (mine,) = (await call(mcp, "list_recipes", {}))["recipes"]
    assert "issues" not in mine


def test_an_own_recipe_whose_profile_is_gone_says_so() -> None:
    """Unpinned recipes apply the profile by title; with that title deleted
    apply would fail halfway, so the listing says it first.
    """
    from decentespresso_mcp.recipes import Catalogue, issues_of
    row = {"name": "house", "profile_title": "Gone", "pin_profile": 0}
    empty = Catalogue(batches={}, beans={}, profile_ids=frozenset(),
                      profile_titles=frozenset({"d-flow"}))
    assert issues_of("mine", row, empty) == ["profile_missing"]
    assert issues_of("mine", {**row, "profile_title": "D-FLOW"}, empty) == []


def test_a_reference_to_a_missing_profile_is_flagged() -> None:
    from decentespresso_mcp.recipes import Catalogue, issues_of
    fav = next(f for f in FAVS if f.get("id") == "a3f6c671-7313-46c1-9cef-db8ef7210385")
    none = Catalogue(batches={}, beans={}, profile_ids=frozenset(), profile_titles=frozenset())
    assert "profile_reference_unresolved" in issues_of("dye2_favs", fav, none)
