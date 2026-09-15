"""Normalisation of the Decaid data (SPEC §20.4).

Two rules are at the centre here, both arising from the migration in M8
(2/n): the zero rating of the import era and the wholesale conversion to UTC.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime

import pytest

from decentespresso_mcp.decaid_mapping import (
    MACHINE_TZ,
    SOURCE_LOCAL,
    SOURCE_UTC,
    is_import_era,
    normalize_enjoyment,
    series_rows_from_decaid,
    shot_row_from_decaid,
    started_at_utc,
    time_source_of,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
SYNCED = "2026-09-14T12:00:00Z"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def imported(stamp: str, *, enjoyment=None, epoch: int = 1785525360) -> dict:
    """A shot from the de1app import: id with Unix time, timestamp in UTC."""
    return {
        "id": f"de1app-{epoch}",
        "timestamp": stamp,
        "createdAt": stamp + "Z" if not stamp.endswith("Z") else stamp,
        "annotations": {"enjoyment": enjoyment},
    }


def native(stamp: str, *, enjoyment=None, created: str | None = None) -> dict:
    """A shot Decaid recorded itself: timestamp in local time."""
    return {
        "id": "da289cfa-c60c-40ee-ab07-13b5c32b623c",
        "timestamp": stamp,
        "createdAt": created or "2026-09-14T05:51:02.020418Z",
        "annotations": {"enjoyment": enjoyment},
    }


# ------------------------------------------------------- Herkunft eines Bezugs


@pytest.mark.parametrize(("shot_id", "expected"), [
    ("de1app-1785525360", True),
    ("de1app-178552536", True),
    ("da289cfa-c60c-40ee-ab07-13b5c32b623c", False),
    ("de1app-", False),
    ("", False),
])
def test_import_era_is_recognised_by_the_id(shot_id: str, expected: bool) -> None:
    assert is_import_era(shot_id) is expected


def test_time_source_follows_the_id() -> None:
    assert time_source_of(imported("2026-08-01T05:32:50")) == SOURCE_UTC
    assert time_source_of(native("2026-09-14T07:50:12")) == SOURCE_LOCAL


def test_crosscheck_against_created_at_warns(caplog) -> None:
    """If Decaid changes its behaviour that should be noticed, not act silently."""
    import logging

    caplog.set_level(logging.WARNING, logger="decentespresso_mcp.decaid_mapping")
    # Import-Kennung, aber Zeitstempel zwei Stunden neben createdAt.
    odd = imported("2026-08-01T07:32:50")
    odd["createdAt"] = "2026-08-01T05:32:50Z"
    time_source_of(odd)

    assert any("time source crosscheck failed" in r.getMessage() for r in caplog.records)


# ------------------------------------------------- Regel 1: Null-Bewertung


def test_zero_from_the_import_era_is_not_a_rating() -> None:
    # 75 of the 88 imported shots sit like this.
    assert normalize_enjoyment(imported("2026-08-01T05:32:50", enjoyment=0.0)) is None


def test_real_ratings_from_the_import_era_survive() -> None:
    for value in (40.0, 50.0, 80.0, 100.0):
        assert normalize_enjoyment(
            imported("2026-08-01T05:32:50", enjoyment=value)
        ) == value


def test_zero_on_a_native_shot_is_kept() -> None:
    # There 0.0 never occurred as a default - so it would be an input.
    assert normalize_enjoyment(native("2026-09-14T07:50:12", enjoyment=0.0)) == 0.0


def test_missing_rating_stays_missing() -> None:
    assert normalize_enjoyment(native("2026-09-14T07:50:12")) is None
    assert normalize_enjoyment({"id": "x", "annotations": {}}) is None
    assert normalize_enjoyment({"id": "x"}) is None


def test_unreadable_rating_becomes_none() -> None:
    assert normalize_enjoyment(native("2026-09-14T07:50:12", enjoyment="gut")) is None


def test_the_real_distribution_never_yields_phantom_ratings() -> None:
    """Against the real distribution: 75 zeros must not arrive as ratings."""
    page = load("shots_page.json")
    shots = page["items"]
    # The fixture page alone is small; the rule is therefore also replayed
    # against the measured overall distribution.
    synthetic = (
        [imported("2026-08-01T05:32:50", enjoyment=0.0, epoch=1785525360 + i)
         for i in range(75)]
        + [imported("2026-08-28T10:59:06", enjoyment=40.0, epoch=1788000000 + i)
           for i in range(13)]
        + [native("2026-09-14T07:50:12", enjoyment=60.0) for _ in range(11)]
        + [native("2026-09-14T07:50:12") for _ in range(69)]
    )
    ratings = [normalize_enjoyment(s) for s in synthetic]
    assert ratings.count(None) == 75 + 69, "the import-era zeros are missing"
    assert sum(1 for r in ratings if r is not None) == 13 + 11
    assert 0.0 not in [r for r in ratings if r is not None]

    # And the real shots from the fixture do not upset the rule.
    for shot in shots:
        value = normalize_enjoyment(shot)
        assert value is None or value > 0


# ---------------------------------------------- Regel 2: alles in UTC


def test_import_era_timestamp_is_already_utc() -> None:
    iso, source = started_at_utc(imported("2026-08-01T05:32:50"))
    assert source == SOURCE_UTC
    assert iso == "2026-08-01T05:32:50Z"


def test_native_timestamp_is_local_and_gets_converted() -> None:
    iso, source = started_at_utc(native("2026-09-14T07:50:12"))
    assert source == SOURCE_LOCAL
    # Sommerzeit: zwei Stunden zurueck.
    assert iso == "2026-09-14T05:50:12Z"


def test_dst_before_and_after_the_october_change() -> None:
    """In 2026 the clocks go back on 25 October.

    A fixed offset would be wrong on one of the two sides - hence a time zone
    rather than an offset.
    """
    before, source = started_at_utc(native("2026-10-24T09:00:00"))
    assert source == SOURCE_LOCAL
    assert before == "2026-10-24T07:00:00Z", "before the change CEST (+2) applies"

    after, _ = started_at_utc(native("2026-10-26T09:00:00"))
    assert after == "2026-10-26T08:00:00Z", "afterwards CET (+1) applies"


def test_dst_gap_and_ambiguous_hour_are_handled() -> None:
    # Going back: 02:30 exists twice on 25 October. The first reading is taken
    # (still summer time).
    ambiguous, _ = started_at_utc(native("2026-10-25T02:30:00"))
    assert ambiguous == "2026-10-25T00:30:00Z"

    # Going forward in March: 02:30 does not exist. Python computes anyway, and
    # the result must at least be well formed.
    spring, _ = started_at_utc(native("2026-03-29T02:30:00"))
    assert spring.endswith("Z")


def test_utc_and_local_of_the_same_wall_clock_differ() -> None:
    """The heart of the rule: the same wall-clock time, two origins."""
    utc_iso, _ = started_at_utc(imported("2026-08-01T05:32:50"))
    local_iso, _ = started_at_utc(native("2026-08-01T05:32:50"))
    assert utc_iso == "2026-08-01T05:32:50Z"
    assert local_iso == "2026-08-01T03:32:50Z"


def test_unreadable_timestamp_yields_none_but_keeps_the_source() -> None:
    iso, source = started_at_utc(native("kaputt"))
    assert iso is None
    assert source == SOURCE_LOCAL


def test_machine_timezone_is_a_zone_not_an_offset() -> None:
    # A fixed offset would be wrong after the last Sunday in October.
    summer = datetime(2026, 8, 1, 12, tzinfo=MACHINE_TZ)
    winter = datetime(2026, 12, 1, 12, tzinfo=MACHINE_TZ)
    assert summer.utcoffset() != winter.utcoffset()


# ------------------------------------------------------------ Zeilenaufbau


def test_shot_row_from_a_real_detail() -> None:
    detail = load("shot_detail.json")
    row = shot_row_from_decaid(detail, SYNCED)

    assert row["id"] == detail["id"]
    assert row["started_at"].endswith("Z")
    assert row["time_source"] in (SOURCE_UTC, SOURCE_LOCAL)
    assert row["synced_at"] == SYNCED
    assert row["duration_s"] > 0
    assert row["stop_reason"] == detail["stopReason"]
    stored = json.loads(row["raw_json"])
    assert "measurements" not in stored, "the series lives in shot_series"
    assert stored == {k: v for k, v in detail.items() if k != "measurements"}

    context = detail["workflow"]["context"]
    assert row["grinder_setting"] == context["grinderSetting"]
    assert row["target_dose_g"] == context["targetDoseWeight"]
    assert row["bean_batch_id"] == context["beanBatchId"]


def test_ratio_needs_both_weights() -> None:
    detail = load("shot_detail.json")
    row = shot_row_from_decaid(detail, SYNCED)
    assert row["ratio"] == pytest.approx(row["yield_g"] / row["dose_g"], abs=0.001)

    without = json.loads(json.dumps(detail))
    without["annotations"]["actualDoseWeight"] = None
    assert shot_row_from_decaid(without, SYNCED)["ratio"] is None


def test_created_and_updated_are_normalised_to_utc() -> None:
    detail = load("shot_detail.json")
    row = shot_row_from_decaid(detail, SYNCED)
    assert row["created_at"].endswith("Z")
    assert row["updated_at"].endswith("Z")


def test_series_rows_carry_targets_and_phase_markers() -> None:
    detail = load("shot_detail.json")
    rows = series_rows_from_decaid(detail)

    assert len(rows) == len(detail["measurements"])
    assert rows[0]["elapsed"] == 0.0
    assert [r["elapsed"] for r in rows] == sorted(r["elapsed"] for r in rows)

    first = rows[0]
    # Target values per data point - this is what makes compliance measurable
    # rather than estimated.
    assert first["target_pressure"] is not None
    assert first["target_flow"] is not None
    assert first["state"] is not None
    assert "profile_frame" in first


def test_series_maps_flow_channels_apart() -> None:
    """flow_in is the pump, flow_out comes from the scale - as in SPEC §8."""
    detail = load("shot_detail.json")
    rows = series_rows_from_decaid(detail)
    point = detail["measurements"][0]

    assert rows[0]["flow_in"] == point["machine"]["flow"]
    assert rows[0]["flow_out"] == point["scale"]["weightFlow"]
    assert rows[0]["weight"] == point["scale"]["weight"]


def test_duplicate_elapsed_is_dropped() -> None:
    detail = json.loads(json.dumps(load("shot_detail.json")))
    detail["measurements"][2] = json.loads(json.dumps(detail["measurements"][1]))
    rows = series_rows_from_decaid(detail)
    assert len(rows) == len(detail["measurements"]) - 1


def test_empty_measurements_are_harmless() -> None:
    detail = {"id": "de1app-1785525360", "timestamp": "2026-08-01T05:32:50",
              "createdAt": "2026-08-01T05:32:50Z", "measurements": []}
    assert series_rows_from_decaid(detail) == []
    assert shot_row_from_decaid(detail, SYNCED)["duration_s"] is None
