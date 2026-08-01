"""Sync-Logik gegen einen Fake-Client: Dedupe, Updates, Fehlerisolation."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest

from visualizer_mcp.db import Database
from visualizer_mcp.sync import (
    STATE_BACKFILL_DONE,
    STATE_CURSOR,
    STATE_NO_PROFILE,
    run_sync,
)
from visualizer_mcp.visualizer_client import ShotNotFound

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ADVANCED_TCL = (FIXTURES / "profile_reference.tcl").read_text(encoding="utf-8")
LEGACY_TCL = (FIXTURES / "profile_recent.tcl").read_text(encoding="utf-8")


class FakeClient:
    """Duck-typed Ersatz fuer VisualizerClient - zaehlt Abrufe mit."""

    def __init__(
        self,
        details: list[dict],
        *,
        failing: set[str] | None = None,
        profiles: dict[str, str] | None = None,
        profile_missing: set[str] | None = None,
    ) -> None:
        self.details = {d["id"]: d for d in details}
        self.failing = failing or set()
        self.profiles = profiles if profiles is not None else {
            d["id"]: ADVANCED_TCL for d in details
        }
        self.profile_missing = profile_missing or set()
        self.detail_calls: list[str] = []
        self.profile_calls: list[str] = []
        self.list_calls = 0

    async def get_profile_tcl(self, shot_id: str) -> str:
        self.profile_calls.append(shot_id)
        if shot_id in self.profile_missing:
            raise ShotNotFound(f"Nicht gefunden: /shots/{shot_id}/profile")
        return self.profiles.get(shot_id, ADVANCED_TCL)

    def _rows(self) -> list[dict]:
        return [
            {"id": d["id"], "clock": 0, "updated_at": d["updated_at"]}
            for d in self.details.values()
        ]

    async def iter_all_shot_rows(self, *, items: int = 100) -> list[dict]:
        self.list_calls += 1
        return self._rows()

    async def list_shots(self, *, page=1, items=100, updated_after=None, etag=None):
        from visualizer_mcp.visualizer_client import Page

        self.list_calls += 1
        rows = [r for r in self._rows()
                if updated_after is None or r["updated_at"] > updated_after]
        return Page(rows=rows, count=len(rows), page=1, pages=1)

    async def get_shot(self, shot_id: str, *, etag=None) -> dict:
        self.detail_calls.append(shot_id)
        if shot_id in self.failing:
            raise ShotNotFound(f"Nicht gefunden: /shots/{shot_id}")
        return self.details[shot_id]


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "sync.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def details() -> list[dict]:
    return [load("shot_reference.json"), load("shot_recent.json")]


async def test_backfill_stores_everything(db: Database, details: list[dict]) -> None:
    client = FakeClient(details)
    result = await run_sync(client, db, full=True)

    assert result.mode == "backfill"
    assert result.new_shots == 2
    assert result.updated == 0
    assert result.errors == []
    assert db.count_shots() == 2
    expected_points = sum(len(d["timeframe"]) for d in details)
    assert db.count_series_points() == expected_points
    assert result.series_points == expected_points
    assert db.get_state(STATE_BACKFILL_DONE) is not None


async def test_second_run_is_a_noop(db: Database, details: list[dict]) -> None:
    # SPEC ss13: zweifacher Lauf derselben Daten -> keine Duplikate.
    client = FakeClient(details)
    await run_sync(client, db, full=True)
    before = db.count_series_points()

    client.detail_calls.clear()
    second = await run_sync(client, db, full=True)

    assert second.new_shots == 0
    assert second.updated == 0
    assert second.unchanged == 2
    assert client.detail_calls == [], "bekannte Shots duerfen nicht erneut geladen werden"
    assert db.count_shots() == 2
    assert db.count_series_points() == before


async def test_changed_shot_is_refetched_and_updated(db: Database, details: list[dict]) -> None:
    client = FakeClient(details)
    await run_sync(client, db, full=True)

    changed = dict(details[0])
    changed["espresso_notes"] = "nachtraeglich notiert"
    changed["updated_at"] = details[0]["updated_at"] + 300
    client.details[changed["id"]] = changed

    result = await run_sync(client, db, full=True)
    assert result.updated == 1
    assert result.new_shots == 0
    row = db._conn.execute(
        "SELECT notes FROM shots WHERE id = ?", (changed["id"],)
    ).fetchone()
    assert row["notes"] == "nachtraeglich notiert"


async def test_incremental_uses_cursor(db: Database, details: list[dict]) -> None:
    client = FakeClient(details)
    await run_sync(client, db, full=True)
    cursor = db.get_state(STATE_CURSOR)
    assert cursor is not None and int(cursor) > 0

    newest = max(d["updated_at"] for d in details)
    fresh = dict(details[0])
    fresh["id"] = "11111111-1111-4111-8111-111111111111"
    fresh["updated_at"] = newest + 1000
    client.details[fresh["id"]] = fresh

    result = await run_sync(client, db)
    assert result.mode == "incremental"
    assert result.new_shots == 1
    assert db.count_shots() == 3


async def test_one_broken_shot_does_not_abort_the_run(db: Database, details: list[dict]) -> None:
    broken = details[0]["id"]
    client = FakeClient(details, failing={broken})

    result = await run_sync(client, db, full=True)

    assert result.new_shots == 1, "der intakte Shot muss trotzdem ankommen"
    assert len(result.errors) == 1
    assert broken in result.errors[0]
    assert db.count_shots() == 1

    stored = db.get_json_state("last_errors")
    assert len(stored) == 1 and broken in stored[0]["error"]


async def test_cursor_is_not_advanced_when_errors_occurred(
    db: Database, details: list[dict]
) -> None:
    # Sonst bliebe der fehlgeschlagene Shot fuer immer ungeholt.
    client = FakeClient(details, failing={details[0]["id"]})
    await run_sync(client, db, full=True)

    assert db.get_state(STATE_CURSOR) is None
    assert db.get_state(STATE_BACKFILL_DONE) is None

    healed = FakeClient(details)
    result = await run_sync(healed, db, full=True)
    assert result.errors == []
    assert db.count_shots() == 2
    assert db.get_state(STATE_CURSOR) is not None


async def test_empty_account_is_handled(db: Database) -> None:
    result = await run_sync(FakeClient([]), db, full=True)
    assert result.new_shots == 0
    assert result.errors == []
    assert db.count_shots() == 0


# ------------------------------------------------------------------ Profile


async def test_identical_profiles_are_deduplicated(db: Database, details: list[dict]) -> None:
    # Beide Shots liefen mit demselben Profil -> eine Version, zwei Verknuepfungen.
    result = await run_sync(FakeClient(details), db, full=True)

    assert result.new_profile_versions == 1
    assert result.profiles_linked == 2
    assert db.count_profiles() == 1
    assert db.shot_ids_without_profile() == []

    overview = db.profile_overview()
    assert len(overview) == 1
    assert overview[0]["name"] == "D-Flow / default"
    assert overview[0]["shot_count"] == 2


async def test_different_profiles_get_separate_versions(
    db: Database, details: list[dict]
) -> None:
    client = FakeClient(details, profiles={
        details[0]["id"]: ADVANCED_TCL,
        details[1]["id"]: LEGACY_TCL,
    })
    result = await run_sync(client, db, full=True)

    assert result.new_profile_versions == 2
    assert db.count_profiles() == 2
    assert {p["name"] for p in db.profile_overview()} == {"D-Flow / default", "Default"}


async def test_changed_profile_creates_a_new_version_and_old_shot_keeps_the_old_one(
    db: Database, details: list[dict]
) -> None:
    # SPEC ss5/Abnahme 4: alter Shot bleibt an der alten Profilversion haengen.
    first = FakeClient([details[0]])
    await run_sync(first, db, full=True)
    old_profile_id = db._conn.execute(
        "SELECT profile_id FROM shots WHERE id = ?", (details[0]["id"],)
    ).fetchone()["profile_id"]

    changed_tcl = ADVANCED_TCL.replace("espresso_pressure 6.0", "espresso_pressure 7.5")
    both = FakeClient(details, profiles={
        details[0]["id"]: ADVANCED_TCL,
        details[1]["id"]: changed_tcl,
    })
    result = await run_sync(both, db, full=True)

    assert result.new_profile_versions == 1
    assert db.count_profiles() == 2

    rows = {
        r["id"]: r["profile_id"]
        for r in db._conn.execute("SELECT id, profile_id FROM shots")
    }
    assert rows[details[0]["id"]] == old_profile_id, "alter Shot darf nicht umgehaengt werden"
    assert rows[details[1]["id"]] != old_profile_id


async def test_profiles_are_fetched_only_once_per_shot(
    db: Database, details: list[dict]
) -> None:
    client = FakeClient(details)
    await run_sync(client, db, full=True)
    assert sorted(client.profile_calls) == sorted(d["id"] for d in details)

    client.profile_calls.clear()
    await run_sync(client, db, full=True)
    assert client.profile_calls == [], "verknuepfte Shots duerfen nicht erneut abgefragt werden"


async def test_shot_without_profile_is_remembered_not_retried(
    db: Database, details: list[dict]
) -> None:
    missing = details[0]["id"]
    client = FakeClient(details, profile_missing={missing})

    result = await run_sync(client, db, full=True)
    assert result.profiles_linked == 1
    assert any(missing in w for w in result.warnings)
    assert result.errors == [], "fehlendes Profil ist kein blockierender Fehler"
    assert db.get_json_state(STATE_NO_PROFILE) == [missing]

    client.profile_calls.clear()
    await run_sync(client, db, full=True)
    assert client.profile_calls == []


async def test_unparsable_profile_is_still_stored_and_linked(
    db: Database, details: list[dict]
) -> None:
    broken = "advanced_shot {{kaputt\nprofile_title {Kaputtes Profil}\n"
    client = FakeClient(details, profiles={d["id"]: broken for d in details})

    result = await run_sync(client, db, full=True)

    assert result.profiles_linked == 2
    assert db.count_profiles() == 1
    assert any("parse_ok=false" in w for w in result.warnings)
    assert result.errors == [], "kaputtes TCL darf den Cursor nicht blockieren"
    # Der Cursor laeuft weiter, obwohl das Profil nicht parsebar war.
    assert db.get_state(STATE_CURSOR) is not None

    row = db._conn.execute("SELECT raw_tcl, parsed_json, name FROM profiles").fetchone()
    assert row["raw_tcl"] == broken
    assert json.loads(row["parsed_json"])["parse_ok"] is False
    assert row["name"] == "Kaputtes Profil"


async def test_profiles_are_backfilled_for_shots_synced_before_m2(
    db: Database, details: list[dict]
) -> None:
    # Zustand nach M1: Shots da, profile_id NULL.
    from visualizer_mcp.visualizer_client import series_rows_from_detail, shot_row_from_detail

    for detail in details:
        db.upsert_shot(shot_row_from_detail(detail, "2026-08-01T10:00:00Z"),
                       series_rows_from_detail(detail))
    db.set_state("backfill_completed_at", "2026-08-01T10:00:00Z")
    assert len(db.shot_ids_without_profile()) == 2

    client = FakeClient(details)
    result = await run_sync(client, db, full=True)

    assert client.detail_calls == [], "Details muessen dafuer nicht erneut geladen werden"
    assert result.profiles_linked == 2
    assert db.shot_ids_without_profile() == []
