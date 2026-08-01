"""Metriken nach SPEC ss8 (Fassung 1.1), gegen den echten Referenz-Shot."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest

from visualizer_mcp.db import Database
from visualizer_mcp.metrics import (
    METRICS_VERSION,
    compute_metrics,
    metrics_for_shot,
    phase_boundaries,
    warm_metrics_cache,
)
from visualizer_mcp.visualizer_client import series_rows_from_detail, shot_row_from_detail

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def detail(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def reference_rows() -> list[dict]:
    return series_rows_from_detail(detail("shot_reference.json"))


@pytest.fixture
def reference() -> dict:
    """Referenz-Shot 6eb25d36 (D-Flow / default), Dosis 18.0 g, Bezug 36.2 g."""
    return compute_metrics(
        series_rows_from_detail(detail("shot_reference.json")),
        dose_g=18.0, yield_g=36.2,
    )


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "metrics.db")
    database.migrate()
    yield database
    database.close()


# ------------------------------------------------------------- Phasenmarken


def test_phase_boundaries_from_state_change(reference_rows: list[dict]) -> None:
    # espresso_state_change ist eine Rechteckwelle; nur die Wechsel zaehlen.
    # Der Wechsel bei t~0.04 ist der Shot-Start und faellt raus.
    # Grenzen kommen ungerundet zurueck - gerundet wird erst die Metrik.
    assert phase_boundaries(reference_rows) == pytest.approx([5.533, 6.208])


def test_boundary_count_matches_steps_minus_one() -> None:
    # Legacy-Profil, von Visualizer als 6 Schritte gedeutet -> 5 Grenzen.
    # Die Abstaende decken sich mit den Schrittdauern (3 s, 1 s, 3 s).
    rows = series_rows_from_detail(detail("shot_recent.json"))
    boundaries = phase_boundaries(rows)
    assert boundaries == pytest.approx([2.024, 4.287, 7.288, 8.278, 11.294], abs=0.001)
    assert len(boundaries) == 6 - 1


def test_no_markers_means_no_boundaries() -> None:
    rows = [{"elapsed": t / 10, "pressure": 1.0, "state_change": None} for t in range(50)]
    assert phase_boundaries(rows) == []


# ------------------------------------------------------- Referenz-Erwartungen


def test_reference_matches_spec_13(reference: dict) -> None:
    # SPEC ss13 (Fassung 1.1) fuer Shot 6eb25d36.
    assert reference["peak_pressure_infusion"] == pytest.approx(4.1, abs=0.15)
    assert reference["end_pressure"] == pytest.approx(5.4, abs=0.15)
    assert reference["duration_s"] == pytest.approx(22.9, abs=0.15)


def test_reference_exact_values(reference: dict) -> None:
    """Pinnt die berechneten Werte, damit Definitionsaenderungen auffallen."""
    assert reference["pi_end"] == 6.2
    assert reference["pi_end_source"] == "state_change"
    assert reference["peak_pressure_infusion"] == 4.1
    assert reference["t_peak"] == 7.2
    assert reference["max_pressure_global"] == 5.4
    assert reference["t_max_pressure_global"] == 22.9
    assert reference["end_pressure"] == 5.3      # Mittel der letzten 2 s = 5.320
    assert reference["duration_s"] == 22.9
    assert reference["ratio"] == 2.011
    assert reference["warnings"] == []


def test_infusion_peak_is_not_the_global_maximum(reference: dict) -> None:
    """Der Grund fuer die Aufteilung in SPEC ss8 1.1.

    Bei D-Flow steigt der Druck zum Schluss an: das globale Maximum liegt auf
    dem letzten Messpunkt und sagt ueber den Puckaufbau nichts aus.
    """
    assert reference["max_pressure_global"] > reference["peak_pressure_infusion"]
    assert reference["t_max_pressure_global"] == reference["duration_s"]
    assert reference["t_peak"] < reference["pi_end"] + 2.0 + 0.05


def test_t_first_drops_follows_the_030_g_threshold(reference: dict) -> None:
    # SPEC ss8: kleinstes elapsed mit weight > 0.3 g -> 5.26 s.
    # Das erste Gewicht ueberhaupt (0.20 g) faellt bei 4.77 s an; SPEC ss13
    # nennt ~4.8 s und meint damit diesen frueheren Zeitpunkt.
    assert reference["t_first_drops"] == 5.3
    rows = series_rows_from_detail(detail("shot_reference.json"))
    first_any = next(r["elapsed"] for r in rows if r["weight"] and r["weight"] > 0)
    assert first_any == pytest.approx(4.77, abs=0.01)


def test_pour_phase_metrics(reference: dict) -> None:
    assert reference["avg_flow_pour"] == 1.92
    assert reference["flow_stability"] == 0.185      # Variationskoeffizient
    assert reference["pressure_trend_pour"] == 0.118  # bar/s, steigend
    assert reference["temp_basket_mean"] == 87.4
    assert reference["temp_basket_std"] == 0.6


def test_dip_is_measured_from_the_infusion_peak(reference: dict) -> None:
    assert reference["pressure_dip_after_peak"] == 0.5


# ------------------------------------------------------------------ Fallback


def _synthetic(*, with_markers: bool) -> list[dict]:
    """Dreieckiger Druckverlauf: 0 -> 10 bar bei t=5, dann konstant."""
    rows = []
    for i in range(101):
        t = i / 10
        pressure = min(10.0, t * 2)
        rows.append({
            "elapsed": t, "pressure": pressure, "flow_out": 2.0, "flow_in": 2.0,
            "weight": max(0.0, (t - 4) * 3), "temp_mix": 90.0, "temp_basket": 88.0,
            "state_change": (1.0 if t < 3.0 else None) if with_markers else None,
        })
    return rows


def test_fallback_heuristic_without_markers() -> None:
    metrics = compute_metrics(_synthetic(with_markers=False))
    # 0.6 x 10 bar = 6 bar, erstmals erreicht bei t = 3.0.
    assert metrics["pi_end"] == 3.0
    assert metrics["pi_end_source"] == "heuristic"
    assert any("Phasenmarken" in w for w in metrics["warnings"])


def test_marker_wins_over_heuristic() -> None:
    metrics = compute_metrics(_synthetic(with_markers=True), dose_g=18.0, yield_g=36.0)
    assert metrics["pi_end"] == 3.0
    assert metrics["pi_end_source"] == "state_change"
    assert metrics["warnings"] == []


def test_empty_series_yields_nulls_and_a_warning() -> None:
    metrics = compute_metrics([])
    assert metrics["duration_s"] is None
    assert metrics["pi_end"] is None
    assert metrics["warnings"]


def test_missing_scale_is_reported() -> None:
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": None, "weight": None,
         "temp_basket": None, "state_change": None}
        for t in range(40)
    ]
    metrics = compute_metrics(rows)
    assert metrics["t_first_drops"] is None
    assert metrics["avg_flow_pour"] is None
    assert any("Waage" in w for w in metrics["warnings"])
    assert any("Korbtemperatur" in w for w in metrics["warnings"])


def test_untared_scale_invalidates_t_first_drops() -> None:
    # Echtfall e9be2f9f: die Tasse stand beim Start auf der Waage (25.1 g).
    # t_first_drops = 0.0 s waere eine Aussage ueber die Tasse, nicht den Bezug.
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": 1.5,
         "weight": 25.1, "temp_basket": 88.0, "state_change": None}
        for t in range(40)
    ]
    rows[0]["weight"] = 0.0     # wie im Echtfall: erst der zweite Punkt zeigt die Tasse
    metrics = compute_metrics(rows)
    assert metrics["t_first_drops"] is None
    assert any("nicht tariert" in w for w in metrics["warnings"])


def test_negative_weight_is_flagged() -> None:
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": 1.5,
         "weight": -2.0 if t == 20 else t * 0.5, "temp_basket": 88.0,
         "state_change": None}
        for t in range(40)
    ]
    metrics = compute_metrics(rows)
    assert any("negativ" in w for w in metrics["warnings"])


def test_non_positive_mean_flow_drops_the_stability_metric() -> None:
    # Ein Variationskoeffizient um einen Mittelwert <= 0 waere negativ und
    # damit sinnlos.
    rows = [
        {"elapsed": t / 10, "pressure": 8.0, "flow_out": -1.0,
         "weight": 0.0, "temp_basket": 88.0, "state_change": None}
        for t in range(40)
    ]
    metrics = compute_metrics(rows)
    assert metrics["avg_flow_pour"] == -1.0
    assert metrics["flow_stability"] is None
    assert any("flow_stability entfaellt" in w for w in metrics["warnings"])


def test_real_shot_with_broken_scale_is_flagged() -> None:
    """e9be2f9f aus dem Archiv - Waage untariert, Bezugsgewicht 0."""
    payload = detail("shot_broken_scale.json")
    metrics = compute_metrics(
        series_rows_from_detail(payload),
        dose_g=float(payload["bean_weight"]),
        yield_g=float(payload["drink_weight"]),
    )
    assert metrics["t_first_drops"] is None
    assert metrics["flow_stability"] is None
    assert metrics["ratio"] is None
    # Druckmetriken bleiben nutzbar - die Waage betrifft sie nicht.
    assert metrics["peak_pressure_infusion"] is not None
    assert metrics["pi_end_source"] == "state_change"
    assert len(metrics["warnings"]) >= 3


def test_ratio_needs_both_dose_and_yield() -> None:
    rows = _synthetic(with_markers=False)
    assert compute_metrics(rows, dose_g=18.0, yield_g=None)["ratio"] is None
    assert compute_metrics(rows, dose_g=18.0, yield_g=36.0)["ratio"] == 2.0


# -------------------------------------------------------------------- Cache


def _store_shot(db: Database, name: str) -> str:
    payload = detail(name)
    db.upsert_shot(
        shot_row_from_detail(payload, "2026-08-01T10:00:00Z"),
        series_rows_from_detail(payload),
    )
    return payload["id"]


def test_metrics_are_cached_and_reused(db: Database) -> None:
    shot_id = _store_shot(db, "shot_reference.json")
    assert db.count_metrics(METRICS_VERSION) == 0

    first = metrics_for_shot(db, shot_id)
    assert first is not None
    assert db.count_metrics(METRICS_VERSION) == 1
    assert metrics_for_shot(db, shot_id) == first


def test_cache_is_dropped_when_the_shot_is_rewritten(db: Database) -> None:
    shot_id = _store_shot(db, "shot_reference.json")
    metrics_for_shot(db, shot_id)
    assert db.count_metrics(METRICS_VERSION) == 1

    _store_shot(db, "shot_reference.json")   # erneuter Upsert
    assert db.count_metrics(METRICS_VERSION) == 0, "Metriken haengen an der Zeitreihe"


def test_stale_version_is_recomputed(db: Database) -> None:
    shot_id = _store_shot(db, "shot_reference.json")
    db.store_metrics(shot_id, METRICS_VERSION - 1, {"veraltet": True})

    assert db.get_cached_metrics(shot_id, METRICS_VERSION) is None
    assert db.shot_ids_without_metrics(METRICS_VERSION) == [shot_id]
    fresh = metrics_for_shot(db, shot_id)
    assert fresh["metrics_version"] == METRICS_VERSION


def test_warm_cache_covers_all_shots(db: Database) -> None:
    _store_shot(db, "shot_reference.json")
    _store_shot(db, "shot_recent.json")

    assert warm_metrics_cache(db) == 2
    assert warm_metrics_cache(db) == 0, "idempotent"
    assert db.count_metrics(METRICS_VERSION) == 2


def test_unknown_shot_yields_none(db: Database) -> None:
    assert metrics_for_shot(db, "gibt-es-nicht") is None
