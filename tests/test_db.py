from __future__ import annotations

import pathlib

import pytest
from helpers import decaid_detail, store_shot

from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_mapping import series_rows_from_decaid, shot_row_from_decaid

SYNCED_AT = "2026-09-14T12:00:00Z"
POINTS = 184


@pytest.fixture
def reference() -> dict:
    return decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50",
                         updated_at="2026-09-01T10:00:00Z")


def test_migrations_are_idempotent(tmp_path: pathlib.Path) -> None:
    first = Database(tmp_path / "m.db")
    applied = first.migrate()
    assert "001_decaid_init.sql" in applied
    assert first.migrate() == []          # a second run does nothing
    first.close()

    reopened = Database(tmp_path / "m.db")
    assert reopened.migrate() == []       # nothing after a restart either
    reopened.close()


def test_a_superseded_schema_file_is_refused(tmp_path: pathlib.Path) -> None:
    """Otherwise the CREATE IF NOT EXISTS would do nothing and the old schema stay."""
    old = Database(tmp_path / "alt.db")
    old.migrate()
    old._conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES ('001_init.sql', ?)",
        (SYNCED_AT,),
    )
    old._conn.commit()
    old.close()

    reopened = Database(tmp_path / "alt.db")
    with pytest.raises(RuntimeError, match="superseded schema"):
        reopened.migrate()
    reopened.close()


def test_schema_has_expected_tables(archive: Database) -> None:
    names = {
        row["name"]
        for row in archive._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"shots", "shot_series", "beans", "bean_batches",
            "profiles", "sync_state"} <= names


def test_insert_then_reinsert_does_not_duplicate(archive: Database, reference: dict) -> None:
    shot = shot_row_from_decaid(reference, SYNCED_AT)
    series = series_rows_from_decaid(reference)

    assert archive.upsert_shot(shot, series) is True      # new
    assert archive.count_shots() == 1
    assert archive.count_series_points() == POINTS

    assert archive.upsert_shot(shot, series) is False     # known
    assert archive.count_shots() == 1
    assert archive.count_series_points() == POINTS, "the series must not double"


def test_upsert_updates_mutable_fields(archive: Database, reference: dict) -> None:
    store_shot(archive, reference, SYNCED_AT)

    changed = decaid_detail(reference["id"], timestamp=reference["timestamp"],
                            updated_at="2026-09-02T10:00:00Z",
                            notes="schmeckt jetzt besser", enjoyment=88)
    store_shot(archive, changed, "2026-09-02T12:00:00Z")

    row = archive.get_shot_row(reference["id"])
    assert row["notes"] == "schmeckt jetzt besser"
    assert row["enjoyment"] == 88
    assert row["updated_at"] == "2026-09-02T10:00:00Z"
    assert archive.count_shots() == 1


def test_known_shot_versions(archive: Database, reference: dict) -> None:
    store_shot(archive, reference, SYNCED_AT)
    assert archive.known_shot_versions() == {reference["id"]: "2026-09-01T10:00:00Z"}


def test_series_cascade_on_delete(archive: Database, reference: dict) -> None:
    store_shot(archive, reference, SYNCED_AT)
    archive._conn.execute("DELETE FROM shots WHERE id = ?", (reference["id"],))
    archive._conn.commit()
    assert archive.count_series_points() == 0


def test_a_shot_survives_an_unknown_batch(archive: Database, reference: dict) -> None:
    """Decaid is the source - a deleted batch must not cost us a shot."""
    detail = decaid_detail("de1app-1785525999", timestamp="2026-08-02T05:32:50")
    detail["workflow"]["context"]["beanBatchId"] = "does-not-exist"
    store_shot(archive, detail, SYNCED_AT)

    row = archive.get_shot_row("de1app-1785525999")
    assert row["bean_batch_id"] == "does-not-exist"
    assert row["bean_id"] is None


def test_state_roundtrip(archive: Database) -> None:
    assert archive.get_state("nope") is None
    archive.set_state("cursor", "42")
    assert archive.get_state("cursor") == "42"
    archive.set_state("cursor", "43")
    assert archive.get_state("cursor") == "43"
    archive.set_json_state("result", {"new": 2})
    assert archive.get_json_state("result") == {"new": 2}


def test_errors_are_capped_at_20(archive: Database) -> None:
    for i in range(30):
        archive.record_errors([f"error {i}"])
    stored = archive.get_json_state("last_errors")
    assert len(stored) == 20
    assert stored[-1]["error"] == "error 29"
    assert stored[0]["error"] == "error 10"


def test_shot_span_and_max_updated(archive: Database, reference: dict) -> None:
    store_shot(archive, reference, SYNCED_AT)
    store_shot(archive, decaid_detail("aaaa1111-0000-4000-8000-000000000001",
                                      timestamp="2026-09-13T07:50:12",
                                      updated_at="2026-09-13T08:00:00Z"), SYNCED_AT)

    oldest, newest = archive.shot_span()
    assert oldest == "2026-08-01T05:32:50Z"
    assert newest > oldest
    assert archive.max_updated_at() == "2026-09-13T08:00:00Z"


def test_beans_come_from_decaids_list(archive: Database, reference: dict) -> None:
    """A bean never pulled from shows up too."""
    archive.upsert_beans([{
        "id": "bean-1", "name": "Tugu Kawisari", "roaster": "Roesterei",
        "species": "arabica", "processing": "washed", "decaf": 0, "archived": 0,
        "notes": None, "created_at": None, "updated_at": None,
        "raw_json": "{}", "synced_at": SYNCED_AT,
    }])
    beans = archive.list_beans()
    assert [b["bean_name"] for b in beans] == ["Tugu Kawisari"]
    assert beans[0]["shot_count"] == 0
    assert beans[0]["grinder_settings"] == []


def test_grinder_settings_keep_their_commas(archive: Database, reference: dict) -> None:
    """"4,2" is one setting, not two - hence no GROUP_CONCAT."""
    archive.upsert_beans([{
        "id": "bean-1", "name": "Tugu Kawisari", "roaster": None, "species": None,
        "processing": None, "decaf": None, "archived": None, "notes": None,
        "created_at": None, "updated_at": None, "raw_json": "{}", "synced_at": SYNCED_AT,
    }])
    archive.upsert_bean_batches([{
        "id": "batch-1", "bean_id": "bean-1", "roast_date": "2026-07-20",
        "buy_date": None, "freeze_date": None, "unfreeze_date": None,
        "frozen": 0, "archived": 0, "created_at": None, "updated_at": None,
        "raw_json": "{}", "synced_at": SYNCED_AT,
    }])

    detail = decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50")
    detail["workflow"]["context"]["beanBatchId"] = "batch-1"
    detail["workflow"]["context"]["grinderSetting"] = "4,2"
    store_shot(archive, detail, SYNCED_AT)
    assert archive.link_shots_to_beans() == 1

    beans = archive.list_beans()
    assert beans[0]["shot_count"] == 1
    assert beans[0]["grinder_settings"] == ["4,2"]
    assert beans[0]["last_grinder_setting"] == "4,2"


def test_shots_without_a_known_bean_are_still_findable(archive: Database) -> None:
    detail = decaid_detail("de1app-1785525360", timestamp="2026-08-01T05:32:50")
    detail["workflow"]["context"]["beanBatchId"] = None
    store_shot(archive, detail, SYNCED_AT)

    orphans = archive.orphan_bean_names()
    assert len(orphans) == 1
    assert orphans[0]["shot_count"] == 1
    assert orphans[0]["bean_name"]
