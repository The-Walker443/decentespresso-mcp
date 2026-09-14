"""MCP tools per SPEC §9.2, against an archive filled with real data."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import (
    BROKEN_ID,
    RECENT_ID,
    REFERENCE_ID,
    corpus,
    decaid_detail,
    store_shot,
    store_shot_with_profile,
)

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.metrics import warm_metrics_cache
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import SyncCoordinator

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"

REFERENCE = REFERENCE_ID   # D-Flow, oldest, Tchibo
RECENT = RECENT_ID         # Default, newest, Bogatz
BROKEN = BROKEN_ID         # scale not tared

#: SPEC §9: response budget per tool response.
BUDGET_BYTES = 15_000

#: Duration of the reference shot - the last curve point.
DURATION_S = 45.6


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _add_test_beans(db: Database) -> None:
    """The archive's two beans together with their batches."""
    synced = "2026-09-14T12:00:00Z"
    db.upsert_beans([
        {"id": "bean-tchibo", "name": "Testsorte", "roaster": "Tchibo",
         "species": None, "processing": None, "decaf": 0, "archived": 0,
         "notes": None, "created_at": None, "updated_at": None,
         "raw_json": "{}", "synced_at": synced},
        {"id": "bean-bogatz", "name": "Espresso Brasil", "roaster": "Bogatz",
         "species": None, "processing": None, "decaf": 0, "archived": 0,
         "notes": None, "created_at": None, "updated_at": None,
         "raw_json": "{}", "synced_at": synced},
    ])
    db.upsert_bean_batches([
        {"id": "batch-tchibo", "bean_id": "bean-tchibo", "roast_date": "2026-07-20",
         "buy_date": None, "freeze_date": None, "unfreeze_date": None, "frozen": 0,
         "archived": 0, "created_at": None, "updated_at": None,
         "raw_json": "{}", "synced_at": synced},
        {"id": "batch-bogatz", "bean_id": "bean-bogatz", "roast_date": "2026-09-01",
         "buy_date": None, "freeze_date": None, "unfreeze_date": None, "frozen": 0,
         "archived": 0, "created_at": None, "updated_at": None,
         "raw_json": "{}", "synced_at": synced},
    ])
    db.link_shots_to_beans()


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "tools.db")
    database.migrate()
    for detail in corpus():
        store_shot_with_profile(database, detail, "2026-09-14T12:00:00Z")
    _add_test_beans(database)
    warm_metrics_cache(database)
    database.set_state("last_sync_at", "2026-08-01T10:00:00Z")
    database.set_state("backfill_completed_at", "2026-08-01T10:00:00Z")
    yield database
    database.close()


@pytest.fixture
def mcp(config: Config, db: Database):
    return build_mcp(config, db)


async def call(mcp, name: str, args: dict[str, Any] | None = None) -> Any:
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


def size_of(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------- list_beans


async def test_list_beans(mcp) -> None:
    beans = (await call(mcp, "list_beans"))["beans"]
    by_roaster = {b["roaster"]: b for b in beans}
    assert set(by_roaster) == {"Tchibo", "Bogatz"}

    tchibo = by_roaster["Tchibo"]
    assert tchibo["name"] == "Testsorte"
    assert tchibo["shot_count"] == 1
    assert tchibo["first_shot"] == tchibo["last_shot"] == "2026-07-31T19:16:00Z"
    assert tchibo["last_grinder_setting"]

    bogatz = by_roaster["Bogatz"]
    assert bogatz["shot_count"] == 2
    # Newest bean first.
    assert beans[0]["roaster"] == "Bogatz"


# ---------------------------------------------------------------- list_shots


async def test_list_shots_returns_compact_rows(mcp) -> None:
    result = await call(mcp, "list_shots")
    assert result["total_matching"] == 3
    assert result["next_cursor"] is None
    assert [s["id"] for s in result["shots"]][0] == RECENT   # newest first

    row = next(s for s in result["shots"] if s["id"] == REFERENCE)
    assert row["bean"] == "Tchibo Testsorte"
    assert row["profile"] == "D-Flow"
    assert row["dose_g"] == 18.0
    assert row["ratio"] == 2.311
    assert row["peak_pressure_infusion"] == 6.6     # not the global maximum
    assert row["peak_pressure_infusion"] < row["duration_s"]
    assert row["warnings"] == 0


async def test_list_shots_flags_unreliable_metrics(mcp) -> None:
    result = await call(mcp, "list_shots")
    broken = next(s for s in result["shots"] if s["id"] == BROKEN)
    assert broken["warnings"] >= 2, "the shot with the broken scale must stand out"


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"bean": "tchibo"}, {REFERENCE}),          # case-insensitive
        ({"bean": "Testsorte"}, {REFERENCE}),       # matches the name too
        ({"roaster": "bogatz"}, {RECENT, BROKEN}),  # the roastery only
        ({"roaster": "Testsorte"}, set()),          # the name does not count here
        ({"profile": "d-flow"}, {REFERENCE}),
        ({"profile": "default"}, {RECENT, BROKEN}),  # substring
    ],
)
async def test_list_shots_filters(mcp, filters: dict, expected: set) -> None:
    result = await call(mcp, "list_shots", filters)
    assert {s["id"] for s in result["shots"]} == expected


async def test_list_shots_date_filters(mcp) -> None:
    only_august = await call(mcp, "list_shots", {"since": "2026-08-01"})
    assert REFERENCE not in {s["id"] for s in only_august["shots"]}

    only_july = await call(mcp, "list_shots", {"until": "2026-08-01"})
    assert {s["id"] for s in only_july["shots"]} == {REFERENCE}


async def test_relative_date_shortcuts_are_accepted(mcp) -> None:
    # The fixtures lie in the past; a narrow window matches nothing, a wide one
    # everything. What is tested is the parsing, not the date.
    assert (await call(mcp, "list_shots", {"since": "1h"}))["total_matching"] == 0
    assert (await call(mcp, "list_shots", {"since": "50y"}))["total_matching"] == 3
    for shortcut in ("12h", "7d", "2w", "1m", "1y"):
        await call(mcp, "list_shots", {"since": shortcut})


async def test_invalid_date_is_a_clear_error(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "list_shots", {"since": "letzte Woche"})
    assert "invalid_argument" in str(excinfo.value)


async def test_pagination_walks_all_shots(mcp) -> None:
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        page = await call(mcp, "list_shots", {"limit": 2, "cursor": cursor})
        seen += [s["id"] for s in page["shots"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 3
    assert len(set(seen)) == 3, "no duplicates across pages"


async def test_bad_cursor_is_rejected(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "list_shots", {"cursor": "irgendwas"})
    assert "invalid_argument" in str(excinfo.value)


# ------------------------------------------------------------------ get_shot


async def test_get_shot_by_id(mcp) -> None:
    result = await call(mcp, "get_shot", {"id": REFERENCE})

    assert result["shot"]["bean_roaster"] == "Tchibo"
    assert result["shot"]["time_source"] == "utc"
    assert result["shot"]["dose_g"] == 18.0
    assert result["metrics"]["pi_end"] == 21.1
    # Decaid reports the phase in plain text - read off, not inferred.
    assert result["metrics"]["pi_end_source"] == "substate"
    assert result["metrics"]["peak_pressure_infusion"] == 6.6
    assert result["metrics"]["max_pressure_global"] == 9.0
    assert result["profile"]["title"] == "D-Flow"
    assert result["profile"]["steps"]

    # SPEC §17.1: shape always, point arrays only on request.
    assert result["curve_shape"]["segments"]
    assert "curve" not in result

    # Cache internals do not belong in the response.
    assert "metrics_version" not in result["metrics"]
    assert "n_points" not in result["metrics"]


async def test_get_shot_latest_without_coordinator(mcp) -> None:
    result = await call(mcp, "get_shot", {"id": "latest"})
    assert result["shot"]["id"] == RECENT
    assert "freshness" not in result, "ohne Verbindung gibt es keinen Frische-Check"


async def test_get_shot_latest_with_bean_filter(mcp) -> None:
    result = await call(mcp, "get_shot", {"id": "latest", "bean": "Tchibo"})
    assert result["shot"]["id"] == REFERENCE


async def test_get_shot_unknown_id(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "get_shot", {"id": "does-not-exist"})
    assert "shot_not_found" in str(excinfo.value)


async def test_shape_is_much_smaller_than_the_point_arrays(mcp) -> None:
    """The reasoning behind the default change in SPEC §17.1."""
    lean = await call(mcp, "get_shot", {"id": REFERENCE})
    full = await call(mcp, "get_shot", {"id": REFERENCE, "include_curve": True})

    assert "curve" not in lean
    assert "curve" in full
    assert size_of(lean) < size_of(full)
    # The shape alone costs a fraction of the point arrays.
    assert size_of(lean["curve_shape"]) * 3 < size_of(full["curve"])


async def test_a_broken_scale_is_reported_not_guessed(mcp) -> None:
    result = await call(mcp, "get_shot", {"id": BROKEN})
    assert result["metrics"]["t_first_drops"] is None
    assert any("tared" in w for w in result["metrics"]["warnings"])


async def test_shot_without_profile_reports_none(mcp, db) -> None:
    detail = decaid_detail("de1app-1785599999", timestamp="2026-08-05T05:32:50")
    detail["workflow"]["profile"] = {}
    store_shot(db, detail)
    result = await call(mcp, "get_shot", {"id": "de1app-1785599999"})
    assert result["profile"] is None


# ------------------------------------------------------------------- Kurven


async def test_curve_is_compact_arrays(mcp) -> None:
    curve = (await call(
        mcp, "get_shot", {"id": REFERENCE, "include_curve": True}
    ))["curve"]
    assert set(curve) <= {"t", "p", "fi", "fo", "w", "tb"}
    lengths = {len(v) for v in curve.values()}
    assert len(lengths) == 1, "alle Kanaele muessen gleich lang sein"


async def test_curve_respects_max_points_and_keeps_the_peaks(mcp) -> None:
    metrics = await call(mcp, "get_shot_metrics", {"id": REFERENCE})
    for max_points in (10, 25, 60, 120):
        curve = (await call(
            mcp,
            "get_shot",
            {"id": REFERENCE, "include_curve": True, "max_points": max_points},
        ))["curve"]
        assert len(curve["t"]) <= max_points

        # SPEC §9.1: the first and last point and both pressure maxima remain.
        assert curve["t"][0] == 0.0
        assert curve["t"][-1] == pytest.approx(DURATION_S, abs=0.01)
        assert metrics["t_peak"] == pytest.approx(
            min(curve["t"], key=lambda t: abs(t - metrics["t_peak"])), abs=0.051
        )
        assert max(curve["p"]) == pytest.approx(metrics["max_pressure_global"], abs=0.05)


async def test_max_points_is_capped_at_400(mcp) -> None:
    curve = (await call(
        mcp, "get_shot", {"id": REFERENCE, "include_curve": True, "max_points": 9999}
    ))["curve"]
    assert len(curve["t"]) <= 400


@pytest.mark.parametrize("max_points", [1, 2, 3])
async def test_tiny_budget_still_keeps_the_mandatory_points(mcp, max_points: int) -> None:
    """Mandatory points take precedence over max_points.

    Otherwise a max_points=2 would cut away the pressure peak even though it is
    described as guaranteed. That makes the curve at most four points long.
    """
    metrics = await call(mcp, "get_shot_metrics", {"id": REFERENCE})
    curve = (await call(
        mcp,
        "get_shot",
        {"id": REFERENCE, "include_curve": True, "max_points": max_points},
    ))["curve"]

    assert len(curve["t"]) <= 4
    assert curve["t"][0] == 0.0
    assert curve["t"][-1] == pytest.approx(DURATION_S, abs=0.01)
    for wanted in (metrics["t_peak"], metrics["t_max_pressure_global"]):
        # Metric times are rounded to 0.1 s, curve times to 0.01 s - the nearest
        # point may therefore be up to 0.05 s off.
        assert min(abs(t - wanted) for t in curve["t"]) <= 0.051


async def test_missing_channels_are_omitted(db: Database, config: Config) -> None:
    detail = decaid_detail("de1app-1785588888", timestamp="2026-08-06T05:32:50")
    for point in detail["measurements"]:
        point["machine"]["groupTemperature"] = None
    store_shot(db, detail)

    curve = (await call(
        build_mcp(config, db), "get_shot",
        {"id": "de1app-1785588888", "include_curve": True}
    ))["curve"]
    assert "tb" not in curve
    assert "p" in curve


# ----------------------------------------------------------- get_shot_metrics


async def test_get_shot_metrics(mcp) -> None:
    metrics = await call(mcp, "get_shot_metrics", {"id": REFERENCE})
    assert metrics["id"] == REFERENCE
    assert metrics["end_pressure"] == 8.5
    assert metrics["duration_s"] == DURATION_S
    assert metrics["warnings"] == []


async def test_get_shot_metrics_unknown_id(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "get_shot_metrics", {"id": "does-not-exist"})
    assert "shot_not_found" in str(excinfo.value)


# ------------------------------------------------------------- compare_shots


async def test_compare_shots(mcp) -> None:
    result = await call(mcp, "compare_shots", {"ids": [REFERENCE, RECENT]})

    assert result["reference"] == REFERENCE
    assert len(result["shots"]) == 2
    assert len(result["deltas"]) == 1

    delta = result["deltas"][0]
    assert delta["id"] == RECENT
    reference, other = result["shots"]
    assert delta["vs_reference"]["dose_g"] == pytest.approx(
        other["dose_g"] - reference["dose_g"]
    )
    assert delta["vs_reference"]["peak_pressure_infusion"] == pytest.approx(
        other["peak_pressure_infusion"] - reference["peak_pressure_infusion"], abs=0.001
    )
    assert "curve" not in reference


async def test_compare_skips_deltas_for_null_fields(mcp) -> None:
    result = await call(mcp, "compare_shots", {"ids": [REFERENCE, BROKEN]})
    # t_first_drops is null on the broken shot - a delta would be invented.
    assert "t_first_drops" not in result["deltas"][0]["vs_reference"]
    assert "peak_pressure_infusion" in result["deltas"][0]["vs_reference"]


async def test_compare_splits_the_point_budget_across_shots(mcp) -> None:
    """Four shots at 60 points each blow the response budget (measured 18 kB)."""
    two = await call(
        mcp, "compare_shots", {"ids": [REFERENCE, RECENT], "include_curves": True}
    )
    three = await call(
        mcp,
        "compare_shots",
        {"ids": [REFERENCE, RECENT, BROKEN], "include_curves": True},
    )

    assert all(len(e["curve"]["t"]) <= 50 for e in two["shots"])
    assert all(len(e["curve"]["t"]) <= 34 for e in three["shots"])
    assert size_of(three) < size_of(two) * 2, "mehr Shots kosten unterproportional"


@pytest.mark.parametrize("count", [1, 5])
async def test_compare_rejects_wrong_number_of_ids(mcp, count: int) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "compare_shots", {"ids": [REFERENCE] * count})
    assert "invalid_argument" in str(excinfo.value)


async def test_compare_rejects_unknown_id(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "compare_shots", {"ids": [REFERENCE, "does-not-exist"]})
    assert "shot_not_found" in str(excinfo.value)


# ------------------------------------------------------ Profile-Tools


async def test_list_profiles_groups_versions(mcp) -> None:
    profiles = (await call(mcp, "list_profiles"))["profiles"]
    by_name = {p["name"]: p for p in profiles}
    assert set(by_name) == {"D-Flow", "Default"}
    assert by_name["D-Flow"]["versions"][0]["shot_count"] == 1
    assert by_name["Default"]["versions"][0]["shot_count"] == 2
    assert len(by_name["D-Flow"]["versions"][0]["version_hash"]) == 8


async def test_get_profile_by_shot(mcp) -> None:
    profile = await call(mcp, "get_profile", {"shot_id": REFERENCE})
    assert profile["title"] == "D-Flow"
    assert profile["type"] == "advanced"
    assert profile["target_weight_g"] == 42.0
    assert [s["name"] for s in profile["steps"]] == ["Filling", "Infusing", "Pouring"]
    assert profile["steps"][0]["exit"] == {"type": "pressure_over", "value": 1.5}


async def test_get_profile_by_name_and_hash(mcp) -> None:
    by_name = await call(mcp, "get_profile", {"name": "d-flow"})
    by_hash = await call(mcp, "get_profile", {"version_hash": by_name["version_hash"]})
    assert by_hash["version_hash"] == by_name["version_hash"]


async def test_get_profile_needs_exactly_one_argument(mcp) -> None:
    for args in ({}, {"shot_id": REFERENCE, "name": "Default"}):
        with pytest.raises(ToolError) as excinfo:
            await call(mcp, "get_profile", args)
        assert "invalid_argument" in str(excinfo.value)


async def test_get_profile_for_shot_without_one(mcp, db) -> None:
    detail = decaid_detail("de1app-1785577777", timestamp="2026-08-07T05:32:50")
    detail["workflow"]["profile"] = {}
    store_shot(db, detail)
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "get_profile", {"shot_id": "de1app-1785577777"})
    assert "profile_not_found" in str(excinfo.value)


# --------------------------------------------------------------- Antwortbudget


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("status", {}),
        ("list_beans", {}),
        ("list_shots", {}),
        ("list_profiles", {}),
        ("get_shot", {"id": REFERENCE}),
        ("get_shot", {"id": REFERENCE, "max_points": 400}),
        ("get_shot_metrics", {"id": REFERENCE}),
        ("get_profile", {"shot_id": REFERENCE}),
        ("compare_shots", {"ids": [REFERENCE, RECENT]}),
        ("compare_shots", {"ids": [REFERENCE, RECENT, BROKEN], "include_curves": True}),
        # Worst case: the maximum number of shots with everything attached.
        ("compare_shots", {"ids": [REFERENCE, RECENT, BROKEN, REFERENCE],
                           "include_curves": True}),
    ],
)
async def test_response_budget(mcp, tool: str, args: dict) -> None:
    # SPEC ss9: Standardantwort <= ~15 kB.
    payload = await call(mcp, tool, args)
    assert size_of(payload) <= BUDGET_BYTES, f"{tool} blows the budget"


async def test_compact_arrays_beat_object_lists(mcp) -> None:
    """SPEC §9.1 justifies the arrays with roughly 60 % savings.

    Measured on this shot: against a list of objects using the same short keys
    it is about 49 %, against one with spelled-out column names about 70 %. The
    60 % in the spec sits between the two - depending on what one compares
    against.
    """
    curve = (await call(
        mcp, "get_shot", {"id": REFERENCE, "include_curve": True}
    ))["curve"]
    keys = [k for k in curve if k != "t"]

    short_keys = [
        {"t": t, **{k: curve[k][i] for k in keys}} for i, t in enumerate(curve["t"])
    ]
    verbose = {"t": "elapsed", "p": "pressure", "fi": "flow_in",
               "fo": "flow_out", "w": "weight", "tb": "temp_basket"}
    long_keys = [
        {"elapsed": t, **{verbose[k]: curve[k][i] for k in keys}}
        for i, t in enumerate(curve["t"])
    ]

    assert size_of(curve) < size_of(short_keys) * 0.55
    assert size_of(curve) < size_of(long_keys) * 0.40


# ------------------------------------------------------------------ sync_now


async def test_sync_now_without_connection(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "sync_now")
    assert "sync_unavailable" in str(excinfo.value)


async def test_sync_now_runs_the_coordinator(config: Config, db: Database) -> None:
    class FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        async def run(self, *, full: bool | None = None):
            from decentespresso_mcp.sync import SyncResult

            self.calls.append(full)
            return SyncResult(new_shots=1, mode="incremental")

    coordinator = FakeCoordinator()
    result = await call(build_mcp(config, db, coordinator), "sync_now")

    assert coordinator.calls == [False], "sync_now runs incrementally, not as a backfill"
    assert result["new_shots"] == 1


# ----------------------------------------------------- Frische-Check (latest)


class RecordingCoordinator(SyncCoordinator):
    """SyncCoordinator ohne Netz - zaehlt, wann ein Abgleich ausgeloest wurde."""

    def __init__(self, db: Database) -> None:
        super().__init__(client=None, db=db)  # type: ignore[arg-type]
        self.runs = 0

    async def run(self, *, full: bool | None = None):
        from decentespresso_mcp.sync import SyncResult

        self.runs += 1
        return SyncResult(new_shots=0, mode="incremental")


async def test_latest_skips_sync_when_fresh(config: Config, db: Database) -> None:
    from decentespresso_mcp.db import utc_now_iso

    db.set_state("last_sync_at", utc_now_iso())
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "get_shot", {"id": "latest"})

    assert coordinator.runs == 0
    assert result["freshness"]["synced"] is False
    assert "current" in result["freshness"]["note"]


async def test_latest_syncs_when_stale(config: Config, db: Database) -> None:
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "get_shot", {"id": "latest"})

    assert coordinator.runs == 1, "aelter als 2 Minuten -> Abgleich"
    assert result["freshness"]["synced"] is True


async def test_latest_survives_a_failing_sync(config: Config, db: Database) -> None:
    from decentespresso_mcp.decaid_client import DecaidError

    class FailingCoordinator(RecordingCoordinator):
        async def run(self, *, full: bool | None = None):
            self.runs += 1
            raise DecaidError("Decaid is not responding.")

    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = FailingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "get_shot", {"id": "latest"})

    # Archive beats error message: the data is there, just perhaps old.
    assert result["shot"]["id"] == RECENT
    assert result["freshness"]["synced"] is False
    assert "Sync failed" in result["freshness"]["note"]


async def test_id_other_than_latest_never_syncs(config: Config, db: Database) -> None:
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = RecordingCoordinator(db)

    await call(build_mcp(config, db, coordinator), "get_shot", {"id": REFERENCE})
    assert coordinator.runs == 0


async def test_list_shots_first_page_syncs_when_stale(config: Config, db: Database) -> None:
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "list_shots", {"limit": 2})

    assert coordinator.runs == 1
    assert result["freshness"]["synced"] is True


async def test_list_shots_first_page_skips_sync_when_fresh(
    config: Config, db: Database
) -> None:
    from decentespresso_mcp.db import utc_now_iso

    db.set_state("last_sync_at", utc_now_iso())
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "list_shots")

    assert coordinator.runs == 0
    assert result["freshness"]["synced"] is False


async def test_list_shots_does_not_sync_while_paginating(
    config: Config, db: Database
) -> None:
    # A sync mid-pagination could shift the result set under the
    # Cursor verschieben.
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = RecordingCoordinator(db)
    mcp = build_mcp(config, db, coordinator)

    first = await call(mcp, "list_shots", {"limit": 2})
    assert coordinator.runs == 1
    assert first["next_cursor"] is not None

    second = await call(mcp, "list_shots", {"limit": 2, "cursor": first["next_cursor"]})
    assert coordinator.runs == 1, "Folgeseiten loesen keinen Abgleich aus"
    assert "freshness" not in second


async def test_list_shots_without_coordinator_has_no_freshness(mcp) -> None:
    assert "freshness" not in await call(mcp, "list_shots")
