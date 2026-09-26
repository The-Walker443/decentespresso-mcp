"""Coffee onboarding: create_bean, create_batch, clone_profile, update_profile,
and profile selection in set_workflow (SPEC §11.5).

The stand-in reproduces what was measured on the live instance (Decaid 0.8.6,
2026-09-26) rather than what the documentation promises - where the two
differed, the difference is the point of a test here:

- a profile's id is a hash of its brewing content;
- a POST of content that already exists answers 201 with the *existing*
  record and silently drops the new title, parent and metadata (T39);
- a content change through PUT moves the record to a new id and the old one
  answers 404; parent and metadata survive (T37);
- a bundled default refuses content changes with 400;
- the workflow takes a profile object and holds no reference to one (T38);
- a bean without name or roaster is refused with 400.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_client import DecaidClient
from decentespresso_mcp.profile_forge import _brewing
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import SyncCoordinator, same_bean

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
PROFILES = json.loads((FIXTURES / "profiles_sample.json").read_text(encoding="utf-8"))
WORKFLOW = json.loads((FIXTURES / "workflow_unsaved_dflow.json").read_text(encoding="utf-8"))
BEAN_ID = "11111111-1111-4111-8111-111111111111"


def content_id(profile: dict[str, Any]) -> str:
    """Decaid's scheme in shape - 'profile:' and 20 hex - over the brewing content."""
    return "profile:" + hashlib.sha256(_brewing(profile).encode()).hexdigest()[:20]


class FakeDecaid:
    def __init__(self, *, workflow_profile: dict[str, Any] | None = None,
                 fail_posts: bool = False) -> None:
        self.beans = [{"id": BEAN_ID, "name": "Arabica Honey Process",
                       "roaster": "Tugu Kawisari", "decaf": False, "archived": False}]
        self.batches: list[dict[str, Any]] = []
        self.profiles = copy.deepcopy(PROFILES)
        self.workflow = copy.deepcopy(WORKFLOW)
        if workflow_profile is not None:
            self.workflow["profile"] = copy.deepcopy(workflow_profile)
        self.fail_posts = fail_posts
        self.calls: list[tuple[str, str]] = []
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else None
        if method == "POST" and self.fail_posts:
            return httpx.Response(503, json={"error": "busy"})
        try:
            return self._route(method, path, body)
        except StopIteration:
            return httpx.Response(404, json={"error": "not found"})

    def _route(self, method: str, path: str, body: Any) -> httpx.Response:
        tail = path.rsplit("/", 1)[-1]
        if path == "/api/v1/beans":
            if method == "GET":
                return httpx.Response(200, json=self.beans)
            if not isinstance(body.get("name"), str) or not isinstance(body.get("roaster"), str):
                return httpx.Response(400, json={"error": "type 'Null' is not a "
                                                 "subtype of type 'String' in type cast"})
            bean = {"id": self._new_id(), **body, "archived": False}
            self.beans.append(bean)
            return httpx.Response(201, json=bean)
        if path.startswith("/api/v1/beans/") and path.endswith("/batches"):
            bean_id = path.split("/")[4]
            next(b for b in self.beans if b["id"] == bean_id)
            batch = {"id": self._new_id(), "beanId": bean_id, **body,
                     "frozen": False, "archived": False}
            if "weight" in body:
                batch["weightRemaining"] = body["weight"]
            self.batches.append(batch)
            return httpx.Response(201, json=batch)
        if path.startswith("/api/v1/beans/"):
            return httpx.Response(200, json=next(b for b in self.beans if b["id"] == tail))
        if path == "/api/v1/bean-batches":
            return httpx.Response(200, json=self.batches)
        if path.startswith("/api/v1/bean-batches/"):
            return httpx.Response(200, json=next(b for b in self.batches if b["id"] == tail))
        if path == "/api/v1/profiles":
            if method == "GET":
                return httpx.Response(200, json=self.profiles)
            return self._post_profile(body)
        if path.startswith("/api/v1/profiles/"):
            record = next(r for r in self.profiles if r["id"] == tail)
            if method == "GET":
                return httpx.Response(200, json=record)
            return self._put_profile(record, body)
        if path == "/api/v1/workflow":
            if method == "PUT":
                if "profile" in body:
                    self.workflow["profile"] = body["profile"]
                self.workflow["context"].update(body.get("context") or {})
            return httpx.Response(200, json=self.workflow)
        return httpx.Response(404, json={"error": "unknown"})

    def _post_profile(self, body: dict[str, Any]) -> httpx.Response:
        new_id = content_id(body["profile"])
        existing = next((r for r in self.profiles if r["id"] == new_id), None)
        if existing is not None:
            return httpx.Response(201, json=existing)          # T39
        record = {"id": new_id, "profile": body["profile"], "parentId": body.get("parentId"),
                  "metadata": body.get("metadata"), "visibility": "visible",
                  "isDefault": False}
        self.profiles.append(record)
        return httpx.Response(201, json=record)

    def _put_profile(self, record: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        if record["isDefault"]:
            return httpx.Response(400, json={"error": "Invalid request", "message":
                                             "Cannot modify default profile content"})
        self.profiles.remove(record)                          # T37: replaced, not kept
        moved = dict(record, profile=body["profile"], id=content_id(body["profile"]))
        self.profiles.append(moved)
        return httpx.Response(200, json=moved)

    def _new_id(self) -> str:
        self.counter += 1
        return f"00000000-0000-4000-8000-{self.counter:012d}"

    def posts(self) -> list[str]:
        return [p for m, p in self.calls if m == "POST"]

    def puts(self) -> list[str]:
        return [p for m, p in self.calls if m == "PUT"]


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "onboarding.db")
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


async def _no_sleep(attempt: int) -> None:
    return None


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


async def refused(mcp, name: str, args: dict) -> str:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, name, args)
    return str(excinfo.value)


# --------------------------------------------------------------- Beans


async def test_create_bean_reads_back_and_archives(writable, db) -> None:
    fake = FakeDecaid()
    result = await call(server(fake, writable, db), "create_bean", {"fields": {
        "name": "Grano Gayo", "roaster": "Coffee Circle", "altitude": [1400, 1600]}})
    assert result["id"].startswith("00000000")
    assert result["bean"]["altitude"] == [1400, 1600]
    assert db.list_beans()[-1]["bean_name"] in {"Grano Gayo", "Arabica Honey Process"}


async def test_a_second_bean_by_accident_is_refused(writable, db) -> None:
    """Otherwise one coffee's history splits in two and never joins again.

    Case and spacing do not make a different coffee - measured live, the guard
    caught "probe-m10" / "PROBE-M10  Roaster" against an existing entry.
    """
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "create_bean", {"fields": {
        "name": "arabica honey  process", "roaster": "TUGU KAWISARI"}})
    assert "already_exists" in message
    assert BEAN_ID in message, "names the one that exists"
    assert fake.posts() == []


async def test_name_and_roaster_are_required_before_sending(writable, db) -> None:
    """Decaid refuses the omission too - with a Dart type-cast error. Ours says which."""
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "create_bean",
                            {"fields": {"name": "Only a name"}})
    assert "required: roaster" in message
    assert fake.posts() == []


def test_same_bean_compares_as_a_person_would() -> None:
    beans = [{"name": "Arabica Honey Process", "roaster": "Tugu Kawisari"}]
    assert same_bean(beans, " arabica   HONEY process", "tugu kawisari")
    assert same_bean(beans, "Arabica Honey Process", "Other Roaster") is None


# -------------------------------------------------------------- Batches


async def test_create_batch_initialises_the_remaining_weight(writable, db) -> None:
    fake = FakeDecaid()
    result = await call(server(fake, writable, db), "create_batch", {
        "bean_id": BEAN_ID, "fields": {"roastDate": "2026-09-20", "weight": 250,
                                       "bestBeforeDate": "2027-03-01"}})
    assert result["batch"]["weightRemaining"] == 250
    assert result["bean"] == {"name": "Arabica Honey Process", "roaster": "Tugu Kawisari"}


async def test_a_batch_needs_a_bean_that_exists(writable, db) -> None:
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "create_batch", {
        "bean_id": "22222222-2222-4222-8222-222222222222", "fields": {"weight": 250}})
    assert "no bean" in message
    assert fake.posts() == []


async def test_a_creation_is_sent_once_even_when_it_fails(writable, db) -> None:
    """A PUT sent twice leaves the same state; a POST sent twice leaves two beans.

    A 503 is retried for reads and updates. For a creation the tablet may have
    acted on the first request before failing to answer, so it is sent exactly
    once and the message says the thing may exist anyway.
    """
    fake = FakeDecaid(fail_posts=True)
    message = await refused(server(fake, writable, db), "create_bean", {"fields": {
        "name": "Grano Gayo", "roaster": "Coffee Circle"}})
    assert len(fake.posts()) == 1
    assert "may exist anyway" in message


# ---------------------------------------------------------- Cloning


async def test_clone_by_title_takes_the_visible_one_and_names_it_after_the_bean(
    writable, db
) -> None:
    fake = FakeDecaid()
    result = await call(server(fake, writable, db), "clone_profile", {
        "source": "D-Flow", "bean_id": BEAN_ID,
        "overrides": {"temperature_c": 91.5, "target_weight_g": 40}})

    assert result["title"] == "Tugu Kawisari – Arabica Honey Process"
    assert result["cloned_from"]["id"] == "profile:198546fc983546de03b7", "the visible one"
    assert result["parent_id"] == "profile:198546fc983546de03b7", "lineage to it"
    assert result["visible_on_tablet"] is True
    created = next(r for r in fake.profiles if r["id"] == result["id"])
    assert created["metadata"]["createdBy"] == "decentespresso-mcp"


async def test_a_copy_that_brews_identically_is_refused(writable, db) -> None:
    """Measured (T39): Decaid answers 201 with the original's id and quietly
    drops the new title - a success report for something that never happened."""
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "clone_profile", {
        "source": "D-Flow", "title": "Renamed only", "overrides": {}})
    assert "brews identically" in message
    assert fake.posts() == []


async def test_a_copy_matching_another_record_names_that_record(writable, db) -> None:
    """Shifting the visible D-Flow back to 88 C gives the hidden earlier edit's
    temperatures - but not its other settings, so this one is still new. A
    content twin is only refused when everything brewing-relevant matches."""
    fake = FakeDecaid()
    first = await call(server(fake, writable, db), "clone_profile", {
        "source": "D-Flow", "title": "Twin A", "overrides": {"target_weight_g": 41}})
    message = await refused(server(fake, writable, db), "clone_profile", {
        "source": "D-Flow", "title": "Twin B", "overrides": {"target_weight_g": 41}})
    assert "already exists" in message
    assert first["id"] in message


async def test_a_taken_title_is_refused_with_a_suggestion(writable, db) -> None:
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "clone_profile", {
        "source": "D-Flow", "title": "adaptive V3", "overrides": {"temperature_c": 90}})
    assert "(2)" in message
    assert fake.posts() == []


async def test_without_title_or_bean_there_is_nothing_to_name_it(writable, db) -> None:
    message = await refused(server(FakeDecaid(), writable, db), "clone_profile", {
        "source": "D-Flow", "overrides": {"temperature_c": 90}})
    assert "give a title" in message


# ---------------------------------------------------------- Updating


async def test_update_moves_the_id_and_says_so(writable, db) -> None:
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    clone = await call(mcp, "clone_profile", {
        "source": "D-Flow", "title": "Favourite", "overrides": {"temperature_c": 91.5}})
    result = await call(mcp, "update_profile", {
        "id": clone["id"], "overrides": {"temperature_c": 93}})

    assert result["previous_id"] == clone["id"]
    assert result["id"] != clone["id"]
    assert "Use the new one" in result["note"]
    stored = next(r for r in fake.profiles if r["id"] == result["id"])
    assert stored["profile"]["steps"][-1]["temperature"] == 93.0
    assert stored["metadata"]["createdBy"] == "decentespresso-mcp", "survives the move"


async def test_update_warns_when_the_workflow_still_runs_the_old_version(
    writable, db
) -> None:
    """The workflow holds a copy, not a reference - it does not follow."""
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    clone = await call(mcp, "clone_profile", {
        "source": "D-Flow", "title": "Favourite", "overrides": {"temperature_c": 91.5}})
    await call(mcp, "set_workflow", {"fields": {"profileId": clone["id"]},
                                     "replace_unsaved_profile": True})
    result = await call(mcp, "update_profile", {
        "id": clone["id"], "overrides": {"temperature_c": 93}})
    assert "still is" in result["workflow"]


async def test_the_refresh_update_profile_offers_is_not_refused(writable, db) -> None:
    """A bug M10 shipped with, found while building recipes in M11.

    After update_profile the workflow still runs the previous version, which
    Decaid no longer stores (T37). The unsaved-profile guard took that for a
    tablet tune and refused the very set_workflow update_profile offers next.
    A content this server replaced on purpose is not something to protect.
    """
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    clone = await call(mcp, "clone_profile", {
        "source": "D-Flow", "title": "Favourite", "overrides": {"temperature_c": 91.5}})
    await call(mcp, "set_workflow", {"fields": {"profileId": clone["id"]},
                                     "replace_unsaved_profile": True})
    tuned = await call(mcp, "update_profile", {
        "id": clone["id"], "overrides": {"temperature_c": 93}})

    await call(mcp, "set_workflow", {"fields": {"profileId": tuned["id"]}})
    assert fake.workflow["profile"]["steps"][-1]["temperature"] == 93.0


async def test_a_real_tablet_tune_is_still_protected_after_that(writable, db) -> None:
    """The exemption covers what this server replaced - nothing tuned by hand."""
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    clone = await call(mcp, "clone_profile", {
        "source": "D-Flow", "title": "Favourite", "overrides": {"temperature_c": 91.5}})
    await call(mcp, "update_profile", {"id": clone["id"], "overrides": {"temperature_c": 93}})
    fake.workflow["profile"] = dict(PROFILES[1]["profile"], target_weight=37.5)

    message = await refused(mcp, "set_workflow", {"fields": {"profileId": "Adaptive v3"}})
    assert "not saved as a profile" in message


async def test_a_bundled_default_is_never_changed(writable, db) -> None:
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "update_profile", {
        "id": "D-Flow / default", "overrides": {"temperature_c": 90}})
    assert "bundled defaults" in message
    assert fake.puts() == []


async def test_even_allow_foreign_does_not_open_a_default(writable, db) -> None:
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "update_profile", {
        "id": "Adaptive v3", "overrides": {"temperature_c": 86},
        "allow_foreign": True})
    assert "bundled defaults" in message
    assert fake.puts() == []


async def test_a_hand_tuned_profile_needs_to_be_named(writable, db) -> None:
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    message = await refused(mcp, "update_profile", {
        "id": "D-Flow", "overrides": {"temperature_c": 90}})
    assert "allow_foreign" in message
    assert fake.puts() == []

    result = await call(mcp, "update_profile", {
        "id": "D-Flow", "overrides": {"temperature_c": 90}, "allow_foreign": True})
    assert result["changes"]["temperature_c"]["after"] == 90.0


# ---------------------------------------------------- Selecting a profile


async def test_selection_copies_the_profile_in_and_reports_it(writable, db) -> None:
    fake = FakeDecaid(workflow_profile=PROFILES[0]["profile"])
    result = await call(server(fake, writable, db), "set_workflow", {
        "fields": {"profileId": "Adaptive v3"}})

    assert fake.workflow["profile"]["title"] == "Adaptive v3"
    assert result["changes"]["profileId"]["after"] == "profile:98fa00c191551b435845"
    assert "profileTitle" in result["alongside"]


async def test_an_unsaved_profile_is_not_overwritten_by_accident(writable, db) -> None:
    """The live case: a D-Flow tuned on the tablet, stored nowhere else.

    Measured 2026-09-26 - the workflow's profile matched none of 85 records.
    Selecting another would have lost it with no way back from this server.
    """
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    message = await refused(mcp, "set_workflow", {"fields": {"profileId": "Adaptive v3"}})
    assert "not saved as a profile" in message
    assert fake.puts() == []

    await call(mcp, "set_workflow", {"fields": {"profileId": "Adaptive v3"},
                                     "replace_unsaved_profile": True})
    assert fake.workflow["profile"]["title"] == "Adaptive v3"


async def test_an_unknown_profile_is_refused(writable, db) -> None:
    fake = FakeDecaid()
    message = await refused(server(fake, writable, db), "set_workflow", {
        "fields": {"profileId": "Nothing Like This"}})
    assert "nothing on the tablet" in message
    assert fake.puts() == []


# -------------------------------------------------------------- Listing


async def test_the_tablet_catalogue_marks_what_is_ours(writable, db) -> None:
    fake = FakeDecaid()
    mcp = server(fake, writable, db)
    clone = await call(mcp, "clone_profile", {
        "source": "D-Flow", "title": "Favourite", "overrides": {"temperature_c": 91}})
    listing = (await call(mcp, "list_profiles", {"on_tablet": True}))["profiles"]
    by_id = {p["id"]: p for p in listing}
    assert by_id[clone["id"]]["made_here"] is True
    assert by_id["profile:59036f8846b1ed3b4255"]["default"] is True


async def test_the_creating_tools_do_not_exist_without_write_mode(config, db) -> None:
    async with Client(server(FakeDecaid(), config, db)) as client:
        names = {t.name for t in await client.list_tools()}
    assert not names & {"create_bean", "create_batch", "clone_profile", "update_profile"}
