"""Visualizer-Aera: Feldmapping und Metriken auf den alten Echtdaten.

Ab M8 ist Decaid die Quelle; diese Fixtures bleiben trotzdem im Repo. Sie
pruefen weiterhin gueltige Logik - das Feldmapping des Visualizer-Clients, der
als Community-Upload erhalten bleibt, und die kanalbasierten Metriken, die von
der Quelle unabhaengig sind. Nebenbei dokumentieren sie, wie die Daten vor dem
Umzug aussahen.

Ein Zusatznutzen: diese Bezuege haben keine Zustandsangabe der Maschine, nur
die Rechteckwelle espresso_state_change, die metrics nicht mehr liest. Sie
laufen damit ueber den Heuristikpfad von pi_end - den einzigen realen Datensatz,
auf dem dieser Pfad geprueft wird.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from visualizer_mcp.metrics import compute_metrics, phase_boundaries
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


# ------------------------------------------- Metriken auf den Altdaten


@pytest.fixture
def reference_metrics(reference: dict) -> dict:
    return compute_metrics(
        series_rows_from_detail(reference), dose_g=18.0, yield_g=36.2
    )


def test_channel_metrics_are_source_independent(reference_metrics: dict) -> None:
    """Was aus Druck, Fluss und Waage kommt, haengt nicht an der Quelle.

    Dieselben Werte wie vor dem Umzug - der Wechsel zu Decaid hat an diesen
    Definitionen nichts geaendert.
    """
    assert reference_metrics["peak_pressure_infusion"] == 4.1
    assert reference_metrics["max_pressure_global"] == 5.4
    assert reference_metrics["t_max_pressure_global"] == 22.9
    assert reference_metrics["t_first_drops"] == 5.3
    assert reference_metrics["end_pressure"] == 5.3
    assert reference_metrics["avg_flow_pour"] == 1.92
    assert reference_metrics["temp_basket_mean"] == 87.4
    assert reference_metrics["duration_s"] == 22.9
    assert reference_metrics["ratio"] == pytest.approx(2.011, abs=0.001)


def test_the_heuristic_path_on_real_data(reference_metrics: dict) -> None:
    """Der einzige echte Datensatz, auf dem der Heuristikpfad laeuft.

    Diese Bezuege kennen nur ``espresso_state_change``; ``substate`` und
    ``profile_frame`` gibt es nicht. pi_end faellt damit auf den Druckanker
    zurueck: 0.6 x 5.43 bar = 3.26 bar, erstmals erreicht bei 6.43 s.
    """
    assert reference_metrics["pi_end_source"] == "heuristic"
    assert reference_metrics["pi_end"] == 6.4
    assert any("Heuristik" in w for w in reference_metrics["warnings"]), (
        "eine Naeherung muss als solche gekennzeichnet sein"
    )


def test_phase_boundaries_ignore_the_old_square_wave(reference: dict) -> None:
    """Gegenprobe: die alte Rechteckwelle wird nicht mehr gelesen.

    Das ist Absicht - ``espresso_state_change`` sagt *dass* gewechselt wurde,
    nicht *wozu*. Decaids ``substate`` sagt beides (SPEC ss20.4).
    """
    rows = series_rows_from_detail(reference)
    assert any(r["state_change"] is not None for r in rows), "sonst prueft das nichts"
    assert phase_boundaries(rows) == []


def test_a_broken_scale_was_already_flagged_back_then(reference: dict) -> None:
    payload = load("shot_broken_scale.json")
    metrics = compute_metrics(
        series_rows_from_detail(payload),
        dose_g=float(payload["bean_weight"]),
        yield_g=float(payload["drink_weight"]),
    )
    assert metrics["t_first_drops"] is None
    assert metrics["ratio"] is None
    # Druckmetriken bleiben nutzbar - die Waage betrifft sie nicht.
    assert metrics["peak_pressure_infusion"] is not None
    assert any("Waage" in w for w in metrics["warnings"])
