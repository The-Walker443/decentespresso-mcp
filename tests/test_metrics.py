"""Metrics per SPEC §8, against the real reference shot from Decaid.

The numbers here come from a recorded response of Decaid 0.8.5 and supersede
an earlier set of expectations. What matters in substance is the origin of
``pi_end``: Decaid reports the machine state in plain text, so the end of
preinfusion is read off rather than inferred.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest
from helpers import break_the_scale, corpus, decaid_detail, store_shot

from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_mapping import series_rows_from_decaid
from decentespresso_mcp.metrics import (
    METRICS_VERSION,
    compute_metrics,
    curve_shape,
    frame_boundaries,
    metrics_for_shot,
    phase_boundaries,
    warm_metrics_cache,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"

DOSE_G = 18.0
YIELD_G = 41.6


def reference_detail() -> dict:
    return corpus()[0]


@pytest.fixture
def reference_rows() -> list[dict]:
    return series_rows_from_decaid(reference_detail())


@pytest.fixture
def reference() -> dict:
    """Referenzbezug (D-Flow), Dosis 18.0 g, Bezug 41.6 g, 184 Messpunkte."""
    return compute_metrics(
        series_rows_from_decaid(reference_detail()),
        dose_g=DOSE_G, yield_g=YIELD_G,
    )


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "metrics.db")
    database.migrate()
    yield database
    database.close()


# ----------------------------------------------------------- Phase markers


def test_phase_boundaries_come_from_the_substate(reference_rows: list[dict]) -> None:
    # preparingForShot -> preinfusion (0.99 s) -> pouring (21.11 s).
    # Boundaries come back unrounded - only the metric gets rounded.
    assert phase_boundaries(reference_rows) == pytest.approx(
        [0.99, 21.105], abs=0.001
    )


def test_the_profile_frame_is_the_fallback(reference_rows: list[dict]) -> None:
    """Without a state report the step changes remain - minus the leftover.

    Measured: on the first data point profileFrame still holds the value of
    the previous shot. That change does not count.
    """
    assert frame_boundaries(reference_rows) == pytest.approx(
        [0.99, 3.734, 21.105], abs=0.001
    )

    without_substate = [{**r, "substate": None} for r in reference_rows]
    assert phase_boundaries(without_substate) == frame_boundaries(reference_rows)


def test_no_markers_means_no_boundaries() -> None:
    rows = [{"elapsed": t / 10, "pressure": 1.0, "substate": None,
             "profile_frame": None} for t in range(50)]
    assert phase_boundaries(rows) == []


# --------------------------------------------------- Reference expectations


def test_reference_matches_spec_13(reference: dict) -> None:
    # SPEC ss13, am Decaid-Referenzbezug neu bestimmt.
    assert reference["peak_pressure_infusion"] == pytest.approx(6.6, abs=0.15)
    assert reference["end_pressure"] == pytest.approx(8.5, abs=0.15)
    assert reference["duration_s"] == pytest.approx(45.6, abs=0.15)


def test_reference_exact_values(reference: dict) -> None:
    """Pins the computed values so definition changes get noticed."""
    assert reference["pi_end"] == 21.1
    assert reference["pi_end_source"] == "substate"
    assert reference["peak_pressure_infusion"] == 6.6
    assert reference["t_peak"] == 23.1
    assert reference["max_pressure_global"] == 9.0
    assert reference["t_max_pressure_global"] == 25.8
    assert reference["end_pressure"] == 8.5      # mean of the last 2 s
    assert reference["duration_s"] == 45.6
    assert reference["ratio"] == 2.311
    assert reference["warnings"] == []


def test_infusion_peak_is_not_the_global_maximum(reference: dict) -> None:
    """The reason for the split in SPEC §8 1.1.

    With D-Flow the pressure keeps rising after preinfusion: the global maximum
    sits beyond the infusion window and says nothing about how the puck was
    built.
    """
    assert reference["max_pressure_global"] > reference["peak_pressure_infusion"]
    assert reference["t_max_pressure_global"] > reference["pi_end"] + 2.0
    assert reference["t_peak"] < reference["pi_end"] + 2.0 + 0.05


def test_t_first_drops_follows_the_030_g_threshold(reference: dict) -> None:
    # SPEC §8: smallest elapsed with weight > 0.3 g. The threshold rather than
    # the first deflection at all, because the scale is noisy.
    assert reference["t_first_drops"] == 14.2
    rows = series_rows_from_decaid(reference_detail())
    first_any = next(r["elapsed"] for r in rows if r["weight"] and r["weight"] > 0)
    assert first_any < reference["t_first_drops"]


def test_pour_phase_metrics(reference: dict) -> None:
    assert reference["avg_flow_pour"] == 1.47
    assert reference["flow_stability"] == 0.254      # Variationskoeffizient
    assert reference["pressure_trend_pour"] == 0.077  # bar/s, steigend
    assert reference["temp_basket_mean"] == 94.2
    assert reference["temp_basket_std"] == 0.5


def test_dip_is_measured_from_the_infusion_peak(reference: dict) -> None:
    # With this profile the pressure does not drop after the infusion peak.
    assert reference["pressure_dip_after_peak"] == 0.0


# ---------------------------------------------------------------- Kurvenform


def test_curve_shape_segments_follow_the_phase_markers(reference_rows, reference) -> None:
    shape = curve_shape(reference_rows, reference)

    assert shape["source"] == "machine"
    # Zwei Zustandswechsel (0.99, 21.11) -> drei Abschnitte.
    assert len(shape["segments"]) == 3
    assert [s["from"] for s in shape["segments"]] == [0.0, 1.0, 21.1]
    assert shape["segments"][-1]["to"] == 45.6

    # Abschnitte schliessen lueckenlos aneinander an.
    for earlier, later in zip(shape["segments"], shape["segments"][1:], strict=False):
        assert earlier["to"] == later["from"]


def test_curve_shape_describes_direction_and_linearity(reference_rows, reference) -> None:
    shape = curve_shape(reference_rows, reference)
    first, _, last = shape["segments"]

    # Before the pour the pressure is still at rest.
    assert first["p"]["from"] == 0.0
    assert first["p"]["dir"] == "steady"

    # Bezugsphase: Druck steigt bis 8.5 bar.
    assert last["p"]["dir"] == "rising"
    assert last["p"]["to"] == 8.5
    assert isinstance(last["p"]["linear"], bool)
    assert last["fo"]["dir"] in {"rising", "falling", "steady"}


def test_curve_shape_carries_the_markers(reference_rows, reference) -> None:
    markers = curve_shape(reference_rows, reference)["markers"]
    assert markers["pi_end"] == reference["pi_end"] == 21.1
    assert markers["t_peak"] == reference["t_peak"] == 23.1
    assert markers["t_first_drops"] == reference["t_first_drops"] == 14.2


def test_curve_shape_falls_back_to_markers_without_machine_phases() -> None:
    rows = _without_machine_phases(series_rows_from_decaid(reference_detail()))
    metrics = compute_metrics(rows, dose_g=DOSE_G, yield_g=YIELD_G)

    shape = curve_shape(rows, metrics)
    assert shape["source"] == "markers"
    # Without machine markers pi_end splits the shot into two segments.
    assert len(shape["segments"]) == 2
    assert shape["segments"][0]["to"] == metrics["pi_end"]


def test_curve_shape_without_anything_is_one_segment() -> None:
    rows = [
        {"elapsed": t / 10, "pressure": 6.0, "flow_out": 2.0, "substate": None,
         "profile_frame": None}
        for t in range(30)
    ]
    shape = curve_shape(rows, {})
    assert shape["source"] == "none"
    assert len(shape["segments"]) == 1
    assert shape["markers"] == {}


def test_curve_shape_flags_flat_and_linear() -> None:
    # Konstanter Druck, linear steigender Fluss.
    rows = [
        {"elapsed": t / 10, "pressure": 9.0, "flow_out": t / 10, "substate": None,
         "profile_frame": None}
        for t in range(40)
    ]
    segment = curve_shape(rows, {})["segments"][0]
    assert segment["p"]["dir"] == "steady"
    assert segment["p"]["linear"] is True
    assert segment["fo"]["dir"] == "rising"
    assert segment["fo"]["linear"] is True


def test_curve_shape_detects_a_curved_course() -> None:
    # Halbe Sinuswelle: gleiche Endpunkte, aber deutlich gekruemmt.
    import math

    rows = [
        {"elapsed": t / 10, "pressure": 5 * math.sin(math.pi * t / 39),
         "flow_out": None, "substate": None,
         "profile_frame": None}
        for t in range(40)
    ]
    segment = curve_shape(rows, {})["segments"][0]
    assert segment["p"]["dir"] == "steady", "start and end sit at the same height"
    assert segment["p"]["linear"] is False, "the path between them is not"


def test_curve_shape_omits_channels_without_data() -> None:
    rows = [
        {"elapsed": t / 10, "pressure": 6.0, "flow_out": None, "substate": None,
         "profile_frame": None}
        for t in range(30)
    ]
    segment = curve_shape(rows, {})["segments"][0]
    assert "p" in segment
    assert "fo" not in segment


def test_curve_shape_handles_an_empty_series() -> None:
    shape = curve_shape([], {})
    assert shape == {"segments": [], "markers": {}, "source": "none"}


# ------------------------------------------------------------------ Fallback


def _synthetic(*, with_markers: bool) -> list[dict]:
    """Triangular pressure curve: 0 -> 10 bar at t=5, then constant.

    With markers the machine reports the change to ``pouring`` at t=3.0.
    """
    rows = []
    for i in range(101):
        t = i / 10
        pressure = min(10.0, t * 2)
        rows.append({
            "elapsed": t, "pressure": pressure, "flow_out": 2.0, "flow_in": 2.0,
            "weight": max(0.0, (t - 4) * 3), "temp_mix": 90.0, "temp_basket": 88.0,
            "profile_frame": None,
            "substate": (("preinfusion" if t < 3.0 else "pouring")
                         if with_markers else None),
        })
    return rows


def _without_machine_phases(rows: list[dict]) -> list[dict]:
    """What a shot would look like whose firmware reports no phases."""
    return [{**r, "substate": None, "state": None, "profile_frame": None}
            for r in rows]


def test_heuristic_fallback_on_a_real_series_without_markers() -> None:
    """SPEC §15: a test of its own for the heuristic path.

    The same real shot, only without any phase report from the machine - which
    is what it would look like if the firmware provided neither state nor step
    number. The path stays covered even though ``substate`` has always applied
    in the archive so far.
    """
    rows = _without_machine_phases(series_rows_from_decaid(reference_detail()))
    assert phase_boundaries(rows) == []

    metrics = compute_metrics(rows, dose_g=DOSE_G, yield_g=YIELD_G)
    assert metrics["pi_end_source"] == "heuristic"
    # 0.6 x 9.0 bar = 5.4 bar, first reached at t = 22.8 - later than the
    # actual phase change.
    assert metrics["pi_end"] == pytest.approx(22.8, abs=0.05)
    assert any("phase markers" in w for w in metrics["warnings"])

    # The difference from the marker version is considerable - which is exactly
    # why the source is reported and not merely the value.
    with_markers = compute_metrics(
        series_rows_from_decaid(reference_detail()), dose_g=DOSE_G, yield_g=YIELD_G
    )
    assert with_markers["pi_end"] == 21.1
    assert with_markers["pi_end_source"] == "substate"
    assert metrics["pi_end"] != with_markers["pi_end"]


def test_fallback_heuristic_without_markers() -> None:
    metrics = compute_metrics(_synthetic(with_markers=False))
    # 0.6 x 10 bar = 6 bar, erstmals erreicht bei t = 3.0.
    assert metrics["pi_end"] == 3.0
    assert metrics["pi_end_source"] == "heuristic"
    assert any("phase markers" in w for w in metrics["warnings"])


def test_marker_wins_over_heuristic() -> None:
    metrics = compute_metrics(_synthetic(with_markers=True), dose_g=18.0, yield_g=36.0)
    assert metrics["pi_end"] == 3.0
    assert metrics["pi_end_source"] == "substate"
    assert metrics["warnings"] == []


def test_empty_series_yields_nulls_and_a_warning() -> None:
    metrics = compute_metrics([])
    assert metrics["duration_s"] is None
    assert metrics["pi_end"] is None
    assert metrics["warnings"]


def test_missing_scale_is_reported() -> None:
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": None, "weight": None,
         "temp_basket": None, "substate": None,
         "profile_frame": None}
        for t in range(40)
    ]
    metrics = compute_metrics(rows)
    assert metrics["t_first_drops"] is None
    assert metrics["avg_flow_pour"] is None
    assert any("cale" in w for w in metrics["warnings"])
    assert any("basket temperature" in w for w in metrics["warnings"])


def test_untared_scale_invalidates_t_first_drops() -> None:
    # Real case e9be2f9f: the cup sat on the scale at the start (25.1 g).
    # t_first_drops = 0.0 s would say something about the cup, not the shot.
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": 1.5,
         "weight": 25.1, "temp_basket": 88.0, "substate": None,
         "profile_frame": None}
        for t in range(40)
    ]
    rows[0]["weight"] = 0.0     # as in the real case: only the second point shows the cup
    metrics = compute_metrics(rows)
    assert metrics["t_first_drops"] is None
    assert any("not tared" in w for w in metrics["warnings"])


def test_negative_weight_is_flagged() -> None:
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": 1.5,
         "weight": -2.0 if t == 20 else t * 0.5, "temp_basket": 88.0,
         "substate": None,
         "profile_frame": None}
        for t in range(40)
    ]
    metrics = compute_metrics(rows)
    assert any("negativ" in w for w in metrics["warnings"])


def test_non_positive_mean_flow_drops_the_stability_metric() -> None:
    # A coefficient of variation around a mean <= 0 would be negative and thus
    # meaningless.
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": -1.0,
         "weight": 0.0, "temp_basket": 88.0, "substate": None,
         "profile_frame": None}
        for t in range(40)
    ]
    metrics = compute_metrics(rows)
    assert metrics["avg_flow_pour"] == -1.0
    assert metrics["flow_stability"] is None
    assert any("flow_stability" in w for w in metrics["warnings"])


def test_real_shot_with_broken_scale_is_flagged() -> None:
    """Untared scale: the pressure metrics survive, the scale values do not."""
    payload = break_the_scale(reference_detail())
    metrics = compute_metrics(
        series_rows_from_decaid(payload), dose_g=DOSE_G, yield_g=None,
    )
    assert metrics["t_first_drops"] is None
    assert metrics["flow_stability"] is None
    assert metrics["ratio"] is None
    # The pressure metrics stay usable - the scale does not affect them.
    assert metrics["peak_pressure_infusion"] is not None
    assert metrics["pi_end_source"] == "substate"
    assert len(metrics["warnings"]) >= 2


def test_ratio_needs_both_dose_and_yield() -> None:
    rows = _synthetic(with_markers=False)
    assert compute_metrics(rows, dose_g=18.0, yield_g=None)["ratio"] is None
    assert compute_metrics(rows, dose_g=18.0, yield_g=36.0)["ratio"] == 2.0


# -------------------------------------------------------------------- Cache


def _store_shot(db: Database, detail: dict | None = None) -> str:
    return store_shot(db, detail or reference_detail())


def test_metrics_are_cached_and_reused(db: Database) -> None:
    shot_id = _store_shot(db)
    assert db.count_metrics(METRICS_VERSION) == 0

    first = metrics_for_shot(db, shot_id)
    assert first is not None
    assert db.count_metrics(METRICS_VERSION) == 1
    assert metrics_for_shot(db, shot_id) == first


def test_cache_is_dropped_when_the_shot_is_rewritten(db: Database) -> None:
    shot_id = _store_shot(db)
    metrics_for_shot(db, shot_id)
    assert db.count_metrics(METRICS_VERSION) == 1

    _store_shot(db)   # erneuter Upsert
    assert db.count_metrics(METRICS_VERSION) == 0, "metrics hang on the series"


def test_stale_version_is_recomputed(db: Database) -> None:
    shot_id = _store_shot(db)
    db.store_metrics(shot_id, METRICS_VERSION - 1, {"veraltet": True})

    assert db.get_cached_metrics(shot_id, METRICS_VERSION) is None
    assert db.shot_ids_without_metrics(METRICS_VERSION) == [shot_id]
    fresh = metrics_for_shot(db, shot_id)
    assert fresh["metrics_version"] == METRICS_VERSION


def test_warm_cache_covers_all_shots(db: Database) -> None:
    _store_shot(db)
    _store_shot(db, decaid_detail("de1app-1785599000",
                                           timestamp="2026-08-03T05:32:50"))

    assert warm_metrics_cache(db) == 2
    assert warm_metrics_cache(db) == 0, "idempotent"
    assert db.count_metrics(METRICS_VERSION) == 2


def test_unknown_shot_yields_none(db: Database) -> None:
    assert metrics_for_shot(db, "does-not-exist") is None


# ----------------------------------------- The heuristic path on real data


def heuristic_detail() -> dict:
    """A real shot that genuinely has no usable phase markers.

    Pulled from the archive on 2026-09-15: 241 data points, zero phase
    boundaries. Its failure mode is the more interesting one - the machine does
    report a state, but the same one throughout: ``pouring`` on every single
    point, with ``profileFrame`` stuck at 0. There is no transition to find, so
    neither of the first two sources has anything to say.
    """
    import json

    return json.loads(
        (FIXTURES / "shot_heuristic.json").read_text(encoding="utf-8")
    )


def test_a_real_shot_without_markers_uses_the_heuristic() -> None:
    """The genuine article, not a reference shot with its markers stripped.

    Five shots in the archive land here. Without a real one under test the
    fallback would only ever be exercised against data that was doctored to
    reach it.
    """
    rows = series_rows_from_decaid(heuristic_detail())
    assert len(rows) == 241
    # A state is reported, but it never changes - so there is no boundary.
    assert {r["substate"] for r in rows} == {"pouring"}
    assert {r["profile_frame"] for r in rows} == {0.0}
    assert phase_boundaries(rows) == []

    metrics = compute_metrics(rows)
    assert metrics["pi_end_source"] == "heuristic"
    assert metrics["pi_end"] == 4.3
    assert metrics["duration_s"] == 59.8
    assert any("phase markers" in w for w in metrics["warnings"])


def test_the_heuristic_still_yields_usable_pressure_metrics() -> None:
    """A missing marker costs the phase boundary, not the whole analysis."""
    metrics = compute_metrics(series_rows_from_decaid(heuristic_detail()))
    assert metrics["max_pressure_global"] is not None
    assert metrics["peak_pressure_infusion"] is not None
    assert metrics["temp_basket_mean"] is not None


def test_curve_shape_of_a_markerless_shot_says_where_it_came_from() -> None:
    """One segment, and ``source`` admits there was nothing to split on."""
    shape = curve_shape(series_rows_from_decaid(heuristic_detail()))
    assert shape["source"] == "none"
    assert len(shape["segments"]) == 1
