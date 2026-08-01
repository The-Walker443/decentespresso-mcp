"""Feldmapping gegen echte, anonymisierte API-Responses (tests/fixtures/)."""

from __future__ import annotations

import json
import pathlib

import pytest

from visualizer_mcp.visualizer_client import (
    STATE_CHANGE_NONE,
    series_rows_from_detail,
    shot_row_from_detail,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SYNCED_AT = "2026-08-01T10:00:00Z"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def reference() -> dict:
    """Referenz-Shot aus SPEC ss13 (6eb25d36..., D-Flow / default)."""
    return load("shot_reference.json")


def test_metadata_mapping(reference: dict) -> None:
    row = shot_row_from_detail(reference, SYNCED_AT)
    assert row["id"] == "6eb25d36-ff0c-48d0-8f88-0b05f9c7418a"
    assert row["started_at"] == "2026-07-31T19:16:00Z"  # .000Z abgeschnitten
    assert row["bean_brand"] == "Tchibo"
    assert row["bean_type"] == "Test"
    assert row["profile_name"] == "D-Flow / default"
    assert row["dose_g"] == 18.0        # bean_weight, kam als String "18.0"
    assert row["yield_g"] == 36.2       # drink_weight
    assert row["duration_s"] == pytest.approx(22.949)
    assert row["ratio"] == pytest.approx(2.011, abs=0.001)
    assert row["updated_at"] == reference["updated_at"]
    assert row["synced_at"] == SYNCED_AT


def test_unmeasured_values_become_null(reference: dict) -> None:
    # Visualizer liefert "0" fuer nicht erfasste TDS/EY - eine TDS von 0 % gibt
    # es nicht, als Zahl gespeichert wuerde sie jede Auswertung verzerren.
    assert reference["drink_tds"] == "0"
    row = shot_row_from_detail(reference, SYNCED_AT)
    assert row["drink_tds"] is None
    assert row["drink_ey"] is None


def test_rating_is_kept_when_present(reference: dict) -> None:
    assert reference["espresso_enjoyment"] == 40
    assert shot_row_from_detail(reference, SYNCED_AT)["enjoyment"] == 40


def test_zero_rating_means_unrated() -> None:
    recent = load("shot_recent.json")
    assert recent["espresso_enjoyment"] == 0
    assert shot_row_from_detail(recent, SYNCED_AT)["enjoyment"] is None


def test_raw_json_is_complete(reference: dict) -> None:
    row = shot_row_from_detail(reference, SYNCED_AT)
    assert json.loads(row["raw_json"]) == reference


def test_missing_dose_yields_no_ratio(reference: dict) -> None:
    detail = dict(reference)
    detail.pop("bean_weight")
    row = shot_row_from_detail(detail, SYNCED_AT)
    assert row["dose_g"] is None
    assert row["ratio"] is None


def test_notes_are_mapped(reference: dict) -> None:
    row = shot_row_from_detail(reference, SYNCED_AT)
    assert row["notes"] == "test"                # espresso_notes
    assert row["bean_notes"] == "alte bohnen"


def test_absent_optional_fields_do_not_crash(reference: dict) -> None:
    # private_notes und metadata fehlen im Response komplett, wenn sie leer sind.
    assert "private_notes" not in reference
    assert "metadata" not in reference
    row = shot_row_from_detail(reference, SYNCED_AT)
    assert row["private_notes"] is None

    recent = load("shot_recent.json")
    assert recent["espresso_notes"] is None
    assert shot_row_from_detail(recent, SYNCED_AT)["notes"] is None


def test_series_mapping(reference: dict) -> None:
    rows = series_rows_from_detail(reference)
    assert len(rows) == len(reference["timeframe"]) == 94

    first = rows[0]
    assert first["shot_id"] == reference["id"]
    assert first["elapsed"] == float(reference["timeframe"][0])
    assert first["pressure"] == float(reference["data"]["espresso_pressure"][0])
    assert first["flow_in"] == float(reference["data"]["espresso_flow"][0])
    assert first["flow_out"] == float(reference["data"]["espresso_flow_weight"][0])
    assert first["weight"] == float(reference["data"]["espresso_weight"][0])
    assert first["temp_basket"] == float(reference["data"]["espresso_temperature_basket"][0])

    # Alle Werte kamen als Strings und muessen jetzt Zahlen sein.
    assert all(isinstance(r["elapsed"], float) for r in rows)
    assert all(r["pressure"] is None or isinstance(r["pressure"], float) for r in rows)


def test_state_change_sentinel_becomes_null(reference: dict) -> None:
    raw = [float(v) for v in reference["data"]["espresso_state_change"]]
    assert STATE_CHANGE_NONE in raw, "Fixture sollte den Sentinel enthalten"
    rows = series_rows_from_detail(reference)
    assert all(r["state_change"] != STATE_CHANGE_NONE for r in rows)
    assert any(r["state_change"] is not None for r in rows), "echte Marken bleiben erhalten"


def test_shorter_channel_is_padded_with_null(reference: dict) -> None:
    detail = json.loads(json.dumps(reference))
    detail["data"]["espresso_weight"] = detail["data"]["espresso_weight"][:5]
    rows = series_rows_from_detail(detail)
    assert len(rows) == 94
    assert rows[10]["weight"] is None
    assert rows[0]["weight"] is not None


def test_duplicate_elapsed_is_dropped(reference: dict) -> None:
    # elapsed ist Teil des Primaerschluessels - Duplikate wuerden den Insert sprengen.
    detail = json.loads(json.dumps(reference))
    detail["timeframe"][3] = detail["timeframe"][2]
    rows = series_rows_from_detail(detail)
    assert len(rows) == 93
    assert len({r["elapsed"] for r in rows}) == 93


def test_second_fixture_maps_too() -> None:
    recent = load("shot_recent.json")
    row = shot_row_from_detail(recent, SYNCED_AT)
    assert row["profile_name"] == "Default"
    assert row["bean_brand"] == "Bogatz"
    assert row["grinder_setting"] == "4,2"   # Komma-Dezimal bleibt Freitext
    assert len(series_rows_from_detail(recent)) == len(recent["timeframe"])
