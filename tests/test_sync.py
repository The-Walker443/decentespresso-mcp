"""Sync-Logik gegen einen Fake-Client: Dedupe, Updates, Fehlerisolation."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest

from visualizer_mcp.db import Database
from visualizer_mcp.sync import STATE_BACKFILL_DONE, STATE_CURSOR, run_sync
from visualizer_mcp.visualizer_client import ShotNotFound

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    """Duck-typed Ersatz fuer VisualizerClient - zaehlt Abrufe mit."""

    def __init__(self, details: list[dict], *, failing: set[str] | None = None) -> None:
        self.details = {d["id"]: d for d in details}
        self.failing = failing or set()
        self.detail_calls: list[str] = []
        self.list_calls = 0

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
