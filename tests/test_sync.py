"""Sync with Decaid (SPEC §20.4).

The stand-in client counts *how often* something was fetched - the statements
that give the sync its point hang on that: the list suffices for the decision,
details cost, and a tablet that is switched off is not an error.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from helpers import decaid_detail, store_shot

from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_client import DecaidError, DecaidUnreachable, ShotPage
from decentespresso_mcp.sync import (
    MAX_DETAILS_PER_RUN,
    STATE_BACKFILL_DONE,
    STATE_LAST_REACHABLE,
    SyncCoordinator,
    run_sync,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def listing(detail: dict) -> dict:
    """How a shot appears in the list: everything except the measurements."""
    return {k: v for k, v in detail.items() if k != "measurements"}


class FakeDecaid:
    """Duck-typed stand-in for DecaidClient - counts the requests."""

    def __init__(self, details: list[dict], *, unreachable: bool = False,
                 fail_on: set[str] | None = None, page_size: int = 100) -> None:
        self.details = {d["id"]: d for d in details}
        self.unreachable = unreachable
        self.fail_on = fail_on or set()
        self.page_size = page_size
        self.detail_calls: list[str] = []
        self.page_calls = 0

    async def info(self):
        self._check()
        return load("info.json")

    async def beans(self):
        self._check()
        return load("beans.json")

    async def bean_batches(self, bean_id=None):
        self._check()
        return load("bean_batches.json")

    async def list_shots(self, *, limit=100, offset=0, order="desc", bean_id=None):
        self._check()
        self.page_calls += 1
        items = [listing(d) for d in self.details.values()]
        window = items[offset: offset + min(limit, self.page_size)]
        return ShotPage(items=window, total=len(items), limit=limit, offset=offset)

    async def get_shot(self, shot_id: str):
        self._check()
        self.detail_calls.append(shot_id)
        if shot_id in self.fail_on:
            raise DecaidError(f"kaputt: {shot_id}")
        # A copy: the real client returns fresh JSON on every request, and
        # write_shot compares before against after.
        return json.loads(json.dumps(self.details[shot_id]))

    async def update_shot(self, shot_id, patch):
        self.details[shot_id].setdefault("annotations", {}).update(
            patch.get("annotations") or {}
        )
        return self.details[shot_id]

    async def aclose(self):
        pass

    def _check(self):
        if self.unreachable:
            raise DecaidUnreachable("Tablet aus")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "shots.db")
    database.migrate()
    yield database
    database.close()


def three_shots() -> list[dict]:
    return [
        decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50",
                      updated_at="2026-09-01T10:00:00Z"),
        decaid_detail("aaaa1111-0000-4000-8000-000000000001",
                      timestamp="2026-09-13T07:50:12",
                      updated_at="2026-09-13T08:00:00Z"),
        decaid_detail("aaaa1111-0000-4000-8000-000000000002",
                      timestamp="2026-09-14T07:50:12",
                      updated_at="2026-09-14T08:00:00Z"),
    ]


# --------------------------------------------------------- A basic run


async def test_backfill_stores_everything(db):
    client = FakeDecaid(three_shots())
    result = await run_sync(client, db, full=True)

    assert result.new_shots == 3
    assert result.updated == 0
    assert db.count_shots() == 3
    assert result.series_points == 3 * 184
    assert not result.errors


async def test_beans_and_batches_come_along(db):
    client = FakeDecaid(three_shots())
    result = await run_sync(client, db, full=True)

    beans, batches = db.count_beans()
    assert beans == result.beans > 0
    assert batches == result.bean_batches > 0


async def test_shots_get_their_bean_from_the_batch(db):
    """The shot names only its batch - the bean has to be resolved."""
    client = FakeDecaid(three_shots())
    await run_sync(client, db, full=True)

    row = db.get_shot_row("de1app-1785525360")
    assert row["bean_batch_id"], "Testdaten ohne Charge pruefen nichts"
    batch = db.batch_row(row["bean_batch_id"])
    assert batch is not None
    assert row["bean_id"] == batch["bean_id"]


async def test_second_run_fetches_no_details(db):
    """The heart of the frugality: the list alone decides."""
    client = FakeDecaid(three_shots())
    await run_sync(client, db, full=True)
    assert len(client.detail_calls) == 3

    client.detail_calls.clear()
    result = await run_sync(client, db)

    assert client.detail_calls == []
    assert result.unchanged == 3
    assert result.new_shots == result.updated == 0


async def test_changed_shot_is_refetched(db):
    client = FakeDecaid(three_shots())
    await run_sync(client, db, full=True)
    client.detail_calls.clear()

    changed = client.details["aaaa1111-0000-4000-8000-000000000001"]
    changed["updatedAt"] = "2026-09-20T09:00:00Z"
    changed["annotations"]["espressoNotes"] = "nachtraeglich notiert"

    result = await run_sync(client, db)

    assert client.detail_calls == ["aaaa1111-0000-4000-8000-000000000001"]
    assert result.updated == 1
    assert result.unchanged == 2
    assert db.get_shot_row(changed["id"])["notes"] == "nachtraeglich notiert"


async def test_late_edit_on_an_old_shot_is_found(db):
    """The list is sorted by shot time, not by modification time.

    Paging that stops early would never see a note added today to a shot from
    August - which is exactly why the list is read in full.
    """
    client = FakeDecaid(three_shots())
    await run_sync(client, db, full=True)
    client.detail_calls.clear()

    oldest = client.details["de1app-1785525360"]
    oldest["updatedAt"] = "2026-09-20T09:00:00Z"

    result = await run_sync(client, db)
    assert client.detail_calls == ["de1app-1785525360"]
    assert result.updated == 1


# ------------------------------------------------------- Tablet off


async def test_unreachable_tablet_is_not_an_error(db):
    client = FakeDecaid(three_shots(), unreachable=True)
    result = await run_sync(client, db)

    assert result.waiting_for_tablet is True
    assert result.errors == []
    assert db.count_shots() == 0


async def test_unreachable_tablet_leaves_the_archive_untouched(db):
    client = FakeDecaid(three_shots())
    await run_sync(client, db, full=True)

    client.unreachable = True
    result = await run_sync(client, db)

    assert result.waiting_for_tablet is True
    assert db.count_shots() == 3, "the archive stays as it is"


async def test_tablet_disappearing_mid_run_keeps_what_arrived(db):
    class Flaky(FakeDecaid):
        async def get_shot(self, shot_id):
            if len(self.detail_calls) >= 1:
                self.unreachable = True
            return await super().get_shot(shot_id)

    client = Flaky(three_shots())
    result = await run_sync(client, db, full=True)

    assert result.waiting_for_tablet is True
    assert db.count_shots() == 1, "the one fetched shot stays"
    assert not db.get_state(STATE_BACKFILL_DONE), "the backfill does not count as done"


async def test_reachable_run_records_the_time(db):
    client = FakeDecaid(three_shots())
    await run_sync(client, db, full=True)
    assert db.get_state(STATE_LAST_REACHABLE)


# ------------------------------------------------- Individual failures


async def test_one_broken_shot_does_not_stop_the_run(db):
    client = FakeDecaid(three_shots(),
                        fail_on={"aaaa1111-0000-4000-8000-000000000001"})
    result = await run_sync(client, db, full=True)

    assert db.count_shots() == 2
    assert len(result.errors) == 1
    assert "aaaa1111" in result.errors[0]


async def test_errors_keep_the_backfill_open(db):
    client = FakeDecaid(three_shots(), fail_on={"de1app-1785525360"})
    await run_sync(client, db, full=True)
    assert not db.get_state(STATE_BACKFILL_DONE)


# ----------------------------------------------------------- Profiles


async def test_profile_comes_from_the_workflow(db):
    """No second request, no TCL - the profile ships with the shot."""
    client = FakeDecaid(three_shots())
    result = await run_sync(client, db, full=True)

    assert result.new_profile_versions == 1, "the same profile three times, one version"
    assert result.profiles_linked == 3
    assert db.count_profiles() == 1

    row = db.get_shot_row("de1app-1785525360")
    assert row["profile_id"] is not None
    profile = db.get_profile_row(row["profile_id"])
    assert profile["source"] == "decaid"
    assert json.loads(profile["parsed_json"])["steps"]


async def test_a_changed_profile_becomes_a_new_version(db):
    shots = three_shots()
    shots[2]["workflow"]["profile"]["steps"][0]["temperature"] = 95.5
    client = FakeDecaid(shots)
    result = await run_sync(client, db, full=True)

    assert result.new_profile_versions == 2
    assert db.count_profiles() == 2


async def test_a_shot_without_a_profile_only_warns(db):
    shots = three_shots()
    shots[0]["workflow"]["profile"] = {}
    client = FakeDecaid(shots)
    result = await run_sync(client, db, full=True)

    assert db.count_shots() == 3, "the shot is archived regardless"
    assert result.errors == []
    assert any("no profile" in w for w in result.warnings)


# -------------------------------------------------------- Cap per run


async def test_a_large_backfill_is_split_across_runs(db):
    many = [
        decaid_detail(f"de1app-{1785525360 + i}",
                      timestamp="2026-08-01T05:32:50",
                      updated_at="2026-09-01T10:00:00Z")
        for i in range(MAX_DETAILS_PER_RUN + 5)
    ]
    client = FakeDecaid(many)
    first = await run_sync(client, db, full=True)

    assert first.pending == 5
    assert db.count_shots() == MAX_DETAILS_PER_RUN
    assert not db.get_state(STATE_BACKFILL_DONE), "not done while anything is outstanding"

    second = await run_sync(client, db)
    assert second.pending == 0
    assert db.count_shots() == len(many)
    assert db.get_state(STATE_BACKFILL_DONE)


async def test_pagination_covers_every_page(db):
    many = [
        decaid_detail(f"de1app-{1785525360 + i}", timestamp="2026-08-01T05:32:50",
                      updated_at="2026-09-01T10:00:00Z")
        for i in range(7)
    ]
    client = FakeDecaid(many, page_size=3)
    result = await run_sync(client, db, full=True)

    assert client.page_calls == 3, "7 shots at 3 per page"
    assert result.new_shots == 7


# ------------------------------------------------------- Coordinator


async def test_coordinator_starts_with_a_backfill(db):
    client = FakeDecaid(three_shots())
    result = await SyncCoordinator(client, db).run()
    assert result.mode == "backfill"


async def test_coordinator_switches_to_incremental(db):
    client = FakeDecaid(three_shots())
    coordinator = SyncCoordinator(client, db)
    await coordinator.run()
    assert (await coordinator.run()).mode == "incremental"


async def test_ensure_fresh_skips_a_recent_run(db):
    client = FakeDecaid(three_shots())
    coordinator = SyncCoordinator(client, db)
    await coordinator.run()
    assert await coordinator.ensure_fresh(max_age_s=3600) is None


async def test_ensure_fresh_syncs_when_stale(db):
    client = FakeDecaid(three_shots())
    coordinator = SyncCoordinator(client, db)
    await coordinator.run()
    assert await coordinator.ensure_fresh(max_age_s=0) is not None


async def test_write_shot_reads_back(db):
    client = FakeDecaid(three_shots())
    coordinator = SyncCoordinator(client, db)
    await coordinator.run()

    before, after = await coordinator.write_shot(
        "aaaa1111-0000-4000-8000-000000000002", {"espressoNotes": "schmeckt"}
    )
    assert after["espressoNotes"] == "schmeckt"
    assert before.get("espressoNotes") != "schmeckt"
    # The read-back carries the archive forward too.
    assert db.get_shot_row("aaaa1111-0000-4000-8000-000000000002")["notes"] == "schmeckt"


# ------------------------------------------ Normalisation in the archive


async def test_import_era_zeros_never_reach_the_archive(db):
    """The binding rule from M8 (3/n), here against the finished archive."""
    shots = [
        decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50",
                      updated_at="2026-09-01T10:00:00Z", enjoyment=0.0),
        decaid_detail("de1app-1785525999", timestamp="2026-08-02T05:32:50",
                      updated_at="2026-09-01T10:00:00Z", enjoyment=80.0),
        decaid_detail("aaaa1111-0000-4000-8000-000000000001",
                      timestamp="2026-09-13T07:50:12",
                      updated_at="2026-09-13T08:00:00Z", enjoyment=None),
    ]
    await run_sync(FakeDecaid(shots), db, full=True)

    assert db.get_shot_row("de1app-1785525360")["enjoyment"] is None
    assert db.get_shot_row("de1app-1785525999")["enjoyment"] == 80.0
    assert db.get_shot_row("aaaa1111-0000-4000-8000-000000000001")["enjoyment"] is None


async def test_both_time_sources_land_as_utc(db):
    shots = [
        decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50",
                      updated_at="2026-09-01T10:00:00Z"),
        decaid_detail("aaaa1111-0000-4000-8000-000000000001",
                      timestamp="2026-08-01T05:32:50",
                      updated_at="2026-09-13T08:00:00Z"),
    ]
    await run_sync(FakeDecaid(shots), db, full=True)

    imported = db.get_shot_row("de1app-1785525360")
    native = db.get_shot_row("aaaa1111-0000-4000-8000-000000000001")

    assert imported["time_source"] == "utc"
    assert imported["started_at"] == "2026-08-01T05:32:50Z"
    assert native["time_source"] == "local_berlin"
    assert native["started_at"] == "2026-08-01T03:32:50Z"


async def test_store_shot_helper_matches_the_sync_path(db, tmp_path):
    """The test helper has to store the same thing a real run does."""
    detail = decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50")
    store_shot(db, detail)
    direct = dict(db.get_shot_row("de1app-1785525360"))

    other = Database(tmp_path / "andere.db")
    other.migrate()
    await run_sync(FakeDecaid([detail]), other, full=True)
    synced = dict(other.get_shot_row("de1app-1785525360"))
    other.close()

    ignore = {"synced_at", "profile_id", "bean_id"}
    assert {k: v for k, v in direct.items() if k not in ignore} == \
           {k: v for k, v in synced.items() if k not in ignore}
