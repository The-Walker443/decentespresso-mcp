"""Ratings on Decaid's 0-10 scale (SPEC T46).

decentespresso/decaid#887 turns `annotations.enjoyment` into Decaid's own 0-10
field and rescales the stored ratings in a migration that leaves `updatedAt`
alone. The numbers here come from the live tablet on 2026-09-26 (0.8.6+2801,
DYE2 0.1.15): 216 shots, 88 de1app imports all with `createdAt == timestamp`,
ratings from 20 to 100 plus two native shots at 4.0 and 5.0 - DYE2's raw star
index from before dye2#7.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import httpx
import pytest
from helpers import decaid_detail
from test_sync import FakeDecaid

from decentespresso_mcp.db import Database, default_migrations_dir
from decentespresso_mcp.decaid_client import DecaidClient
from decentespresso_mcp.decaid_mapping import SCALE_10, SCALE_100, enjoyment_of
from decentespresso_mcp.stats import summarise
from decentespresso_mcp.sync import (
    STATE_ENJOYMENT_REREAD,
    STATE_ENJOYMENT_SCALE,
    run_sync,
)
from decentespresso_mcp.writes import ValidationError, validate_fields

NATIVE = "aaaa1111-0000-4000-8000-00000000000"


@pytest.fixture
def db(tmp_path: pathlib.Path):
    database = Database(tmp_path / "scale.db")
    database.migrate()
    yield database
    database.close()


def native(n: int, enjoyment, updated_at="2026-09-16T08:00:00Z"):
    return decaid_detail(f"{NATIVE}{n}", timestamp=f"2026-09-1{n}T07:50:12",
                         updated_at=updated_at, enjoyment=enjoyment)


# ------------------------------------------------------------ The rule


def test_over_ten_is_divided_as_decaid_divides() -> None:
    """Decaid's rescaleDe1appEnjoyment: value * 10 / 100, clamped to 0-10."""
    for raw, archived in ((88.0, 8.8), (61.0, 6.1), (20.0, 2.0), (100.0, 10.0), (150.0, 10.0)):
        assert enjoyment_of(native(1, raw)) == (archived, False)


def test_a_native_one_to_ten_is_kept_and_marked() -> None:
    """The two live shots at 4.0 and 5.0. Dividing them would turn DYE2's
    four and five stars into 0.4 and 0.5; keeping them unmarked would present
    two stars as a verdict. Decaid keeps them too - it cannot tell either.
    """
    assert enjoyment_of(native(1, 4.0)) == (4.0, True)
    assert enjoyment_of(native(1, 10.0)) == (10.0, True), "10 is not over 10"


def test_zero_is_zero_on_both_scales() -> None:
    assert enjoyment_of(native(1, 0.0)) == (0.0, False)


def test_an_untouched_import_is_divided_even_at_ten_or_below() -> None:
    """Decaid's second branch: stamps from the import, no edit since."""
    shot = decaid_detail("de1app-1785525360", enjoyment=8.0)
    shot.update(timestamp="2026-08-01T05:32:50.000Z",
                createdAt="2026-09-01T10:00:00.000Z", updatedAt="2026-09-01T10:00:00.000Z")
    assert enjoyment_of(shot) == (0.8, False)
    shot["updatedAt"] = "2026-09-02T10:00:00.000Z"           # edited since
    assert enjoyment_of(shot) == (8.0, True)


def test_the_live_imports_are_never_untouched() -> None:
    """All 88 carry createdAt == timestamp (schema 5 backfilled it), so on
    this tablet only the over-ten branch applies - as in Decaid's own run."""
    shot = decaid_detail("de1app-1785525360", enjoyment=8.0)
    shot.update(timestamp="2026-08-01T05:32:50.000Z", createdAt="2026-08-01T05:32:50.000Z",
                updatedAt="2026-08-01T05:32:50.000Z")
    assert enjoyment_of(shot) == (8.0, True)


def test_on_a_ten_point_tablet_the_value_is_taken_as_it_is() -> None:
    assert enjoyment_of(native(1, 4.0), SCALE_10) == (4.0, False)
    assert enjoyment_of(native(1, 8.5), SCALE_10) == (8.5, False)


def test_the_import_zero_rule_still_comes_first() -> None:
    shot = decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50", enjoyment=0.0)
    assert enjoyment_of(shot) == (None, False)
    assert enjoyment_of(shot, SCALE_10) == (None, False)


# ------------------------------------------------------- The migration


def test_the_migration_converts_the_archive_by_the_same_rule(tmp_path: pathlib.Path) -> None:
    """An archive filled before 005 holds raw 0-100 values. After it, each row
    must equal what ingestion would now produce - otherwise the archive would
    disagree with itself depending on when a shot was read.
    """
    before = tmp_path / "migrations"
    before.mkdir()
    for sql in sorted(default_migrations_dir().glob("*.sql")):
        if sql.name < "005":
            (before / sql.name).write_text(sql.read_text(encoding="utf-8"), encoding="utf-8")
    path = tmp_path / "archive.db"
    old = Database(path, before)
    old.migrate()
    shots = [native(1, 88.0), native(2, 5.0), native(3, 0.0), native(4, None)]
    untouched = decaid_detail("de1app-1785525360", enjoyment=8.0)
    untouched.update(timestamp="2026-08-01T05:32:50.000Z",
                     createdAt="2026-09-01T10:00:00.000Z", updatedAt="2026-09-01T10:00:00.000Z")
    shots.append(untouched)
    conn = sqlite3.connect(path)
    for shot in shots:
        raw = (shot.get("annotations") or {}).get("enjoyment")
        conn.execute("INSERT INTO shots (id, started_at, time_source, enjoyment, raw_json, "
                     "synced_at) VALUES (?, '2026-09-14T08:00:00Z', 'utc', ?, ?, 'x')",
                     (shot["id"], raw, json.dumps(shot)))
    conn.commit()
    conn.close()
    old.close()

    new = Database(path)
    assert new.migrate() == ["005_enjoyment_scale.sql"]
    for shot in shots:
        row = new.get_shot_row(shot["id"])
        expected, ambiguous = enjoyment_of(shot)
        if shot["id"].startswith("de1app-") and expected is None:
            continue                  # the import-zero rule ran at ingestion
        assert (row["enjoyment"], bool(row["enjoyment_ambiguous"])) == (expected, ambiguous), \
            shot["id"]
    new.close()


# ------------------------------------------------------ The transition


class ScaledDecaid(FakeDecaid):
    """The sync stand-in, able to run Decaid's schema-6 migration."""

    def __init__(self, details):
        super().__init__(details)
        self.version = "0.8.6+2801"

    async def info(self):
        # Both, as the live /info sends them (T2).
        return {**await super().info(), "fullVersion": self.version,
                "buildNumber": self.version.partition("+")[2]}

    def upgrade(self, overrides: dict[str, float] | None = None) -> None:
        """Rescale as #887 does - without touching updatedAt. ``overrides``
        stands for any value the tablet ends up with that the archive's own
        conversion did not predict."""
        self.version, self.rates_on_ten = "0.8.7+2840", True
        for shot in self.details.values():
            value = (shot.get("annotations") or {}).get("enjoyment")
            if value is not None and value > 10:
                shot["annotations"]["enjoyment"] = min(value / 10, 10.0)
            if overrides and shot["id"] in overrides:
                shot["annotations"]["enjoyment"] = overrides[shot["id"]]


async def test_a_change_without_updated_at_is_still_picked_up(db: Database) -> None:
    """The reason for the re-read. The cursor compares updatedAt, which the
    migration leaves standing, so without it a value that changed on the
    tablet would never reach the archive.
    """
    fake = ScaledDecaid([native(1, 80.0), native(2, 61.0)])
    await run_sync(fake, db, full=True)
    assert db.get_shot_row(f"{NATIVE}1")["enjoyment"] == 8.0
    assert db.get_state(STATE_ENJOYMENT_SCALE) == SCALE_100

    fake.upgrade(overrides={f"{NATIVE}2": 7.0})
    fake.detail_calls.clear()
    result = await run_sync(fake, db)

    assert db.get_state(STATE_ENJOYMENT_SCALE) == SCALE_10
    assert db.get_shot_row(f"{NATIVE}2")["enjoyment"] == 7.0
    assert db.get_shot_row(f"{NATIVE}1")["enjoyment"] == 8.0, "the rule predicted it"
    assert result.annotations_reread == 1
    assert fake.detail_calls == [], "the list carries the annotations"


async def test_the_re_read_runs_once(db: Database) -> None:
    fake = ScaledDecaid([native(1, 80.0)])
    await run_sync(fake, db, full=True)
    fake.upgrade()
    await run_sync(fake, db)
    assert db.get_state(STATE_ENJOYMENT_REREAD)
    fake.details[f"{NATIVE}1"]["annotations"]["enjoyment"] = 3.0      # again unseen
    assert (await run_sync(fake, db)).annotations_reread is None


async def test_an_ambiguous_rating_stays_marked_across_the_switch(db: Database) -> None:
    """Decaid leaves 5.0 as it is; read on 0-10 it would pass for canonical."""
    fake = ScaledDecaid([native(1, 5.0)])
    await run_sync(fake, db, full=True)
    assert db.get_shot_row(f"{NATIVE}1")["enjoyment_ambiguous"] == 1
    fake.upgrade()
    await run_sync(fake, db)
    row = db.get_shot_row(f"{NATIVE}1")
    assert (row["enjoyment"], row["enjoyment_ambiguous"]) == (5.0, 1)


async def test_rating_again_after_the_switch_clears_the_mark(db: Database) -> None:
    fake = ScaledDecaid([native(1, 5.0)])
    await run_sync(fake, db, full=True)
    fake.upgrade()
    await run_sync(fake, db)
    shot = fake.details[f"{NATIVE}1"]
    shot["annotations"]["enjoyment"] = 8.0
    shot["updatedAt"] = "2026-10-01T08:00:00Z"
    await run_sync(fake, db)
    row = db.get_shot_row(f"{NATIVE}1")
    assert (row["enjoyment"], row["enjoyment_ambiguous"]) == (8.0, 0)


async def test_the_scale_is_asked_once_per_version(db: Database) -> None:
    calls = []
    fake = ScaledDecaid([native(1, 80.0)])
    original = fake.rejects_enjoyment_over_ten

    async def counted():
        calls.append(1)
        return await original()

    fake.rejects_enjoyment_over_ten = counted
    await run_sync(fake, db, full=True)
    await run_sync(fake, db)
    assert len(calls) == 1
    fake.version = "0.8.6+2802"
    await run_sync(fake, db)
    assert len(calls) == 2


# ------------------------------------------------------------ The probe


def probe_client(status: int, body: dict) -> tuple[DecaidClient, list]:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=body)

    return DecaidClient("http://10.100.100.171:8080",
                        transport=httpx.MockTransport(handler)), seen


@pytest.mark.parametrize(("status", "body", "answer"), [
    (404, {"error": "Shot not found"}, False),                       # 0.8.6+2801, measured
    (400, {"error": "annotations.enjoyment must be between 0 and 10, got 11"}, True),
    (400, {"error": "something else entirely"}, None),
    (200, {}, None),
])
async def test_the_probe_reads_the_behaviour(status, body, answer) -> None:
    client, seen = probe_client(status, body)
    assert await client.rejects_enjoyment_over_ten() is answer
    (method, path, sent), = seen
    assert method == "PUT" and path.endswith("decentespresso-mcp-scale-probe")
    assert sent == {"annotations": {"enjoyment": 11}}


async def test_without_a_clear_probe_the_build_number_decides(db: Database) -> None:
    """#887 is in every main build from 2836 (commit count at the merge)."""
    fake = ScaledDecaid([native(1, 80.0)])

    async def unclear():
        return None

    fake.rejects_enjoyment_over_ten = unclear
    fake.version = "0.8.7+2836"
    assert (await run_sync(fake, db, full=True)).enjoyment_scale == SCALE_10


# ------------------------------------------------------------ Writing


def test_on_zero_to_hundred_one_to_ten_is_refused() -> None:
    """A 10 written on 0.8.6 stays 10 through Decaid's migration and then
    reads as five stars - refused before it goes out, with the reason."""
    with pytest.raises(ValidationError) as excinfo:
        validate_fields({"enjoyment": 10}, enjoyment_scale=SCALE_100)
    assert "ten times higher" in str(excinfo.value)
    assert validate_fields({"enjoyment": 80}, enjoyment_scale=SCALE_100) == {"enjoyment": 80}
    assert validate_fields({"enjoyment": 0}, enjoyment_scale=SCALE_100) == {"enjoyment": 0}


def test_on_zero_to_ten_the_bounds_are_decaids() -> None:
    assert validate_fields({"enjoyment": 8.5}, enjoyment_scale=SCALE_10) == {"enjoyment": 8.5}
    assert validate_fields({"enjoyment": "7,5"}, enjoyment_scale=SCALE_10) == {"enjoyment": 7.5}
    for bad in (11, 80, -1, 8.55):
        with pytest.raises(ValidationError):
            validate_fields({"enjoyment": bad}, enjoyment_scale=SCALE_10)


# ------------------------------------------------------------ Reading


def test_stats_leave_ambiguous_ratings_out_of_the_mean() -> None:
    """Two stars that may mean four would pull a mean of 8 down to 6.5."""
    from datetime import UTC, datetime, timedelta
    now = datetime(2026, 9, 20, tzinfo=UTC)
    base = {"started_at": "2026-09-18T08:00:00Z", "bean_name": "B", "profile_name": "D-Flow",
            "dose_g": 18.0, "yield_g": 36.0, "ratio": 2.0, "duration_s": 28.0}
    shots = [{**base, "id": "a", "enjoyment": 8.0, "enjoyment_ambiguous": 0},
             {**base, "id": "b", "enjoyment": 5.0, "enjoyment_ambiguous": 1},
             {**base, "id": "c", "enjoyment": None, "enjoyment_ambiguous": 0}]
    result = summarise(shots, since=now - timedelta(days=7), until=now)
    assert result["enjoyment"] == {"rated": 1, "unrated": 1, "ambiguous": 1, "mean": 8.0}
