"""MCP-Tools nach SPEC ss9.2, gegen einen mit Echtdaten gefuellten Bestand."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.metrics import warm_metrics_cache
from visualizer_mcp.server import build_mcp
from visualizer_mcp.sync import SyncCoordinator
from visualizer_mcp.tcl_profile import parse_profile, semantic_hash, version_hash
from visualizer_mcp.visualizer_client import series_rows_from_detail, shot_row_from_detail

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

REFERENCE = "6eb25d36-ff0c-48d0-8f88-0b05f9c7418a"   # D-Flow, aeltester
RECENT = "36ed21cd-8fc0-489e-8ea4-959d952e04c0"      # Default, neuester
BROKEN = "e9be2f9f-6817-4827-a8c8-9b4a66243f7d"      # Waage kaputt

#: SPEC ss9: Antwortbudget je Tool-Antwort.
BUDGET_BYTES = 15_000


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _add_shot(db: Database, fixture: str, profile_tcl: str | None) -> str:
    payload = load(fixture)
    db.upsert_shot(
        shot_row_from_detail(payload, "2026-08-01T10:00:00Z"),
        series_rows_from_detail(payload),
    )
    if profile_tcl is not None:
        parsed = parse_profile(profile_tcl)
        profile_id, _ = db.upsert_profile(
            name=parsed["title"] or "(ohne Titel)",
            version_hash=version_hash(profile_tcl),
            semantic_hash=semantic_hash(parsed),
            raw_tcl=profile_tcl,
            parsed_json=json.dumps(parsed, ensure_ascii=False),
            profile_notes=parsed.get("notes"),
            seen_at="2026-08-01T10:00:00Z",
        )
        db.link_shot_profile(payload["id"], profile_id)
    return payload["id"]


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "tools.db")
    database.migrate()
    _add_shot(database, "shot_reference.json",
              (FIXTURES / "profile_reference.tcl").read_text(encoding="utf-8"))
    _add_shot(database, "shot_recent.json",
              (FIXTURES / "profile_recent.tcl").read_text(encoding="utf-8"))
    _add_shot(database, "shot_broken_scale.json", None)
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
    by_brand = {b["brand"]: b for b in beans}
    assert set(by_brand) == {"Tchibo", "Bogatz"}

    tchibo = by_brand["Tchibo"]
    assert tchibo["type"] == "Test"
    assert tchibo["shot_count"] == 1
    assert tchibo["first_shot"] == tchibo["last_shot"] == "2026-07-31T19:16:00Z"
    assert tchibo["last_grinder_setting"] == "4"

    bogatz = by_brand["Bogatz"]
    assert bogatz["shot_count"] == 2
    assert bogatz["grinder_settings_used"] == ["4,2"]
    # Neueste Bohne zuerst.
    assert beans[0]["brand"] == "Bogatz"


# ---------------------------------------------------------------- list_shots


async def test_list_shots_returns_compact_rows(mcp) -> None:
    result = await call(mcp, "list_shots")
    assert result["total_matching"] == 3
    assert result["next_cursor"] is None
    assert [s["id"] for s in result["shots"]][0] == RECENT   # neueste zuerst

    row = next(s for s in result["shots"] if s["id"] == REFERENCE)
    assert row["bean"] == "Tchibo Test"
    assert row["profile"] == "D-Flow / default"
    assert row["dose_g"] == 18.0
    assert row["ratio"] == 2.011
    assert row["peak_pressure_infusion"] == 4.1     # nicht das globale Maximum
    assert row["warnings"] == 0


async def test_list_shots_flags_unreliable_metrics(mcp) -> None:
    result = await call(mcp, "list_shots")
    broken = next(s for s in result["shots"] if s["id"] == BROKEN)
    assert broken["warnings"] >= 2, "der Shot mit kaputter Waage muss auffallen"


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"bean": "tchibo"}, {REFERENCE}),          # case-insensitiv
        ({"bean": "Test"}, {REFERENCE}),            # trifft auch die Sorte
        ({"roaster": "bogatz"}, {RECENT, BROKEN}),  # nur die Marke
        ({"roaster": "Test"}, set()),               # Sorte zaehlt hier nicht
        ({"profile": "d-flow"}, {REFERENCE}),
        ({"profile": "default"}, {REFERENCE, RECENT, BROKEN}),  # Teilstring
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
    # Die Fixtures liegen in der Vergangenheit; ein enges Fenster trifft nichts,
    # ein weites alles. Getestet wird das Parsen, nicht das Datum.
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
    assert len(set(seen)) == 3, "keine Dubletten ueber Seiten hinweg"


async def test_bad_cursor_is_rejected(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "list_shots", {"cursor": "irgendwas"})
    assert "invalid_argument" in str(excinfo.value)


# ------------------------------------------------------------------ get_shot


async def test_get_shot_by_id(mcp) -> None:
    result = await call(mcp, "get_shot", {"id": REFERENCE})

    assert result["shot"]["bean_brand"] == "Tchibo"
    assert result["shot"]["dose_g"] == 18.0
    assert result["metrics"]["pi_end"] == 6.2
    assert result["metrics"]["pi_end_source"] == "state_change"
    assert result["metrics"]["peak_pressure_infusion"] == 4.1
    assert result["metrics"]["max_pressure_global"] == 5.4
    assert result["profile"]["title"] == "D-Flow / default"
    assert len(result["profile"]["steps"]) == 3
    assert "curve" in result

    # Cache-Interna gehoeren nicht in die Antwort.
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
        await call(mcp, "get_shot", {"id": "gibt-es-nicht"})
    assert "shot_not_found" in str(excinfo.value)


async def test_get_shot_without_curve_is_smaller(mcp) -> None:
    with_curve = await call(mcp, "get_shot", {"id": REFERENCE})
    without = await call(mcp, "get_shot", {"id": REFERENCE, "include_curve": False})
    assert "curve" not in without
    assert size_of(without) < size_of(with_curve)


async def test_shot_without_profile_reports_none(mcp) -> None:
    result = await call(mcp, "get_shot", {"id": BROKEN})
    assert result["profile"] is None
    assert result["metrics"]["t_first_drops"] is None
    assert any("tariert" in w for w in result["metrics"]["warnings"])


# ------------------------------------------------------------------- Kurven


async def test_curve_is_compact_arrays(mcp) -> None:
    curve = (await call(mcp, "get_shot", {"id": REFERENCE}))["curve"]
    assert set(curve) <= {"t", "p", "fi", "fo", "w", "tb"}
    lengths = {len(v) for v in curve.values()}
    assert len(lengths) == 1, "alle Kanaele muessen gleich lang sein"


async def test_curve_respects_max_points_and_keeps_the_peaks(mcp) -> None:
    metrics = await call(mcp, "get_shot_metrics", {"id": REFERENCE})
    for max_points in (10, 25, 60, 120):
        curve = (await call(
            mcp, "get_shot", {"id": REFERENCE, "max_points": max_points}
        ))["curve"]
        assert len(curve["t"]) <= max_points

        # SPEC ss9.1: erster und letzter Punkt sowie beide Druckmaxima bleiben.
        assert curve["t"][0] == 0.0
        assert curve["t"][-1] == pytest.approx(22.95, abs=0.01)
        assert metrics["t_peak"] == pytest.approx(
            min(curve["t"], key=lambda t: abs(t - metrics["t_peak"])), abs=0.05
        )
        assert max(curve["p"]) == pytest.approx(metrics["max_pressure_global"], abs=0.05)


async def test_max_points_is_capped_at_400(mcp) -> None:
    curve = (await call(mcp, "get_shot", {"id": REFERENCE, "max_points": 9999}))["curve"]
    assert len(curve["t"]) <= 400


async def test_missing_channels_are_omitted(db: Database, config: Config) -> None:
    payload = load("shot_reference.json")
    rows = series_rows_from_detail(payload)
    for row in rows:
        row["temp_basket"] = None
    db.upsert_shot(shot_row_from_detail(payload, "2026-08-01T10:00:00Z"), rows)

    curve = (await call(build_mcp(config, db), "get_shot", {"id": REFERENCE}))["curve"]
    assert "tb" not in curve
    assert "p" in curve


# ----------------------------------------------------------- get_shot_metrics


async def test_get_shot_metrics(mcp) -> None:
    metrics = await call(mcp, "get_shot_metrics", {"id": REFERENCE})
    assert metrics["id"] == REFERENCE
    assert metrics["end_pressure"] == 5.3
    assert metrics["duration_s"] == 22.9
    assert metrics["warnings"] == []


async def test_get_shot_metrics_unknown_id(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "get_shot_metrics", {"id": "gibt-es-nicht"})
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
    # t_first_drops ist beim kaputten Shot null - eine Differenz waere erfunden.
    assert "t_first_drops" not in result["deltas"][0]["vs_reference"]
    assert "peak_pressure_infusion" in result["deltas"][0]["vs_reference"]


async def test_compare_with_curves_uses_60_points(mcp) -> None:
    result = await call(
        mcp, "compare_shots", {"ids": [REFERENCE, RECENT], "include_curves": True}
    )
    for entry in result["shots"]:
        assert len(entry["curve"]["t"]) <= 60


@pytest.mark.parametrize("count", [1, 5])
async def test_compare_rejects_wrong_number_of_ids(mcp, count: int) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "compare_shots", {"ids": [REFERENCE] * count})
    assert "invalid_argument" in str(excinfo.value)


async def test_compare_rejects_unknown_id(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "compare_shots", {"ids": [REFERENCE, "gibt-es-nicht"]})
    assert "shot_not_found" in str(excinfo.value)


# ------------------------------------------------------ Profile-Tools


async def test_list_profiles_groups_versions(mcp) -> None:
    profiles = (await call(mcp, "list_profiles"))["profiles"]
    by_name = {p["name"]: p for p in profiles}
    assert set(by_name) == {"D-Flow / default", "Default"}
    assert by_name["D-Flow / default"]["versions"][0]["shot_count"] == 1
    assert len(by_name["D-Flow / default"]["versions"][0]["version_hash"]) == 8


async def test_get_profile_by_shot(mcp) -> None:
    profile = await call(mcp, "get_profile", {"shot_id": REFERENCE})
    assert profile["title"] == "D-Flow / default"
    assert profile["type"] == "advanced"
    assert profile["target_weight_g"] == 36
    assert [s["name"] for s in profile["steps"]] == ["Filling", "Infusing", "Pouring"]
    assert profile["steps"][0]["exit"] == {"type": "pressure_over", "value": 2.1}
    assert profile["steps"][1]["exit"] is None      # exit_if 0


async def test_get_profile_by_name_and_hash(mcp) -> None:
    by_name = await call(mcp, "get_profile", {"name": "d-flow"})
    by_hash = await call(mcp, "get_profile", {"version_hash": by_name["version_hash"]})
    assert by_hash["version_hash"] == by_name["version_hash"]


async def test_get_profile_needs_exactly_one_argument(mcp) -> None:
    for args in ({}, {"shot_id": REFERENCE, "name": "Default"}):
        with pytest.raises(ToolError) as excinfo:
            await call(mcp, "get_profile", args)
        assert "invalid_argument" in str(excinfo.value)


async def test_get_profile_for_shot_without_one(mcp) -> None:
    with pytest.raises(ToolError) as excinfo:
        await call(mcp, "get_profile", {"shot_id": BROKEN})
    assert "profile_not_found" in str(excinfo.value)


async def test_legacy_profile_reports_empty_steps(mcp) -> None:
    profile = await call(mcp, "get_profile", {"shot_id": RECENT})
    assert profile["type"] == "pressure"
    assert profile["steps"] == []


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
    ],
)
async def test_response_budget(mcp, tool: str, args: dict) -> None:
    # SPEC ss9: Standardantwort <= ~15 kB.
    payload = await call(mcp, tool, args)
    assert size_of(payload) <= BUDGET_BYTES, f"{tool} sprengt das Budget"


async def test_compact_arrays_beat_object_lists(mcp) -> None:
    """SPEC ss9.1 begruendet die Arrays mit rund 60 % Ersparnis.

    An diesem Shot gemessen: gegen eine Objektliste mit denselben Kurz-
    schluesseln sind es rund 49 %, gegen eine mit sprechenden Spaltennamen rund
    70 %. Die 60 % der SPEC liegen dazwischen - je nachdem, womit man
    vergleicht.
    """
    curve = (await call(mcp, "get_shot", {"id": REFERENCE}))["curve"]
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
            from visualizer_mcp.sync import SyncResult

            self.calls.append(full)
            return SyncResult(new_shots=1, mode="incremental")

    coordinator = FakeCoordinator()
    result = await call(build_mcp(config, db, coordinator), "sync_now")

    assert coordinator.calls == [False], "sync_now laeuft inkrementell, nicht als Backfill"
    assert result["new_shots"] == 1


# ----------------------------------------------------- Frische-Check (latest)


class RecordingCoordinator(SyncCoordinator):
    """SyncCoordinator ohne Netz - zaehlt, wann ein Abgleich ausgeloest wurde."""

    def __init__(self, db: Database) -> None:
        super().__init__(client=None, db=db)  # type: ignore[arg-type]
        self.runs = 0

    async def run(self, *, full: bool | None = None):
        from visualizer_mcp.sync import SyncResult

        self.runs += 1
        return SyncResult(new_shots=0, mode="incremental")


async def test_latest_skips_sync_when_fresh(config: Config, db: Database) -> None:
    from visualizer_mcp.db import utc_now_iso

    db.set_state("last_sync_at", utc_now_iso())
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "get_shot", {"id": "latest"})

    assert coordinator.runs == 0
    assert result["freshness"]["synced"] is False
    assert "aktuell" in result["freshness"]["note"]


async def test_latest_syncs_when_stale(config: Config, db: Database) -> None:
    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "get_shot", {"id": "latest"})

    assert coordinator.runs == 1, "aelter als 2 Minuten -> Abgleich"
    assert result["freshness"]["synced"] is True


async def test_latest_survives_a_failing_sync(config: Config, db: Database) -> None:
    from visualizer_mcp.visualizer_client import Unreachable

    class FailingCoordinator(RecordingCoordinator):
        async def run(self, *, full: bool | None = None):
            self.runs += 1
            raise Unreachable("Visualizer antwortet nicht.")

    db.set_state("last_sync_at", "2020-01-01T00:00:00Z")
    coordinator = FailingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "get_shot", {"id": "latest"})

    # Archiv schlaegt Fehlermeldung: die Daten sind da, nur vielleicht alt.
    assert result["shot"]["id"] == RECENT
    assert result["freshness"]["synced"] is False
    assert "visualizer_unreachable" in result["freshness"]["note"]


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
    from visualizer_mcp.db import utc_now_iso

    db.set_state("last_sync_at", utc_now_iso())
    coordinator = RecordingCoordinator(db)

    result = await call(build_mcp(config, db, coordinator), "list_shots")

    assert coordinator.runs == 0
    assert result["freshness"]["synced"] is False


async def test_list_shots_does_not_sync_while_paginating(
    config: Config, db: Database
) -> None:
    # Ein Abgleich mitten in der Paginierung koennte die Treffermenge unter dem
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
