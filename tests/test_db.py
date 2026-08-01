from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest

from visualizer_mcp.db import Database
from visualizer_mcp.visualizer_client import series_rows_from_detail, shot_row_from_detail

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SYNCED_AT = "2026-08-01T10:00:00Z"


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def reference() -> dict:
    return json.loads((FIXTURES / "shot_reference.json").read_text(encoding="utf-8"))


def test_migrations_are_idempotent(tmp_path: pathlib.Path) -> None:
    first = Database(tmp_path / "m.db")
    applied = first.migrate()
    assert "001_init.sql" in applied
    assert first.migrate() == []          # zweiter Lauf tut nichts
    first.close()

    reopened = Database(tmp_path / "m.db")
    assert reopened.migrate() == []       # auch nach Neustart nichts
    reopened.close()


def test_schema_has_expected_tables(db: Database) -> None:
    names = {
        row["name"]
        for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"shots", "shot_series", "profiles", "sync_state"} <= names


def test_insert_then_reinsert_does_not_duplicate(db: Database, reference: dict) -> None:
    shot = shot_row_from_detail(reference, SYNCED_AT)
    series = series_rows_from_detail(reference)

    assert db.upsert_shot(shot, series) is True      # neu
    assert db.count_shots() == 1
    assert db.count_series_points() == 94

    assert db.upsert_shot(shot, series) is False     # bekannt
    assert db.count_shots() == 1
    assert db.count_series_points() == 94, "Zeitreihe darf sich nicht verdoppeln"


def test_upsert_updates_mutable_fields(db: Database, reference: dict) -> None:
    db.upsert_shot(shot_row_from_detail(reference, SYNCED_AT), [])

    changed = dict(reference)
    changed["espresso_notes"] = "schmeckt jetzt besser"
    changed["espresso_enjoyment"] = 88
    changed["updated_at"] = reference["updated_at"] + 60
    db.upsert_shot(shot_row_from_detail(changed, "2026-08-02T10:00:00Z"), [])

    row = db._conn.execute("SELECT * FROM shots WHERE id = ?", (reference["id"],)).fetchone()
    assert row["notes"] == "schmeckt jetzt besser"
    assert row["enjoyment"] == 88
    assert row["updated_at"] == reference["updated_at"] + 60
    assert db.count_shots() == 1


def test_known_shot_versions(db: Database, reference: dict) -> None:
    db.upsert_shot(shot_row_from_detail(reference, SYNCED_AT), [])
    assert db.known_shot_versions() == {reference["id"]: reference["updated_at"]}


def test_series_cascade_on_delete(db: Database, reference: dict) -> None:
    db.upsert_shot(shot_row_from_detail(reference, SYNCED_AT),
                   series_rows_from_detail(reference))
    db._conn.execute("DELETE FROM shots WHERE id = ?", (reference["id"],))
    db._conn.commit()
    assert db.count_series_points() == 0


def test_state_roundtrip(db: Database) -> None:
    assert db.get_state("nope") is None
    db.set_state("cursor", "42")
    assert db.get_state("cursor") == "42"
    db.set_state("cursor", "43")
    assert db.get_state("cursor") == "43"
    db.set_json_state("result", {"new": 2})
    assert db.get_json_state("result") == {"new": 2}


def test_errors_are_capped_at_20(db: Database) -> None:
    for i in range(30):
        db.record_errors([f"fehler {i}"])
    stored = db.get_json_state("last_errors")
    assert len(stored) == 20
    assert stored[-1]["error"] == "fehler 29"
    assert stored[0]["error"] == "fehler 10"


def test_shot_span_and_max_updated(db: Database, reference: dict) -> None:
    db.upsert_shot(shot_row_from_detail(reference, SYNCED_AT), [])
    recent = json.loads((FIXTURES / "shot_recent.json").read_text(encoding="utf-8"))
    db.upsert_shot(shot_row_from_detail(recent, SYNCED_AT), [])

    oldest, newest = db.shot_span()
    assert oldest == "2026-07-31T19:16:00Z"
    assert newest > oldest
    assert db.max_updated_at() == max(reference["updated_at"], recent["updated_at"])
