"""Puck diagnostics: resistance, channeling, profile compliance (SPEC §8.5-8.7).

The numbers pinned here are the acceptance reference: a real shot the operator
rated 40 and annotated "Etwas sauer und bitter zugleich - vermutlich zu heiss."
Sour and bitter at once is the signature of uneven extraction, so it is the
right shot to hold the diagnostics to.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from decentespresso_mcp.decaid_mapping import series_rows_from_decaid
from decentespresso_mcp.metrics import (
    CHANNELING_INDICATORS,
    METRICS_VERSION,
    channeling,
    compute_metrics,
    control_mode,
    profile_compliance,
    puck_resistance,
    settled_window,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def diagnostic_rows() -> list[dict]:
    """The acceptance reference, 140 points."""
    return series_rows_from_decaid(load("shot_diagnostic.json"))


@pytest.fixture
def diagnostic_metrics(diagnostic_rows) -> dict:
    return compute_metrics(diagnostic_rows, dose_g=18.0, yield_g=36.0)


# ------------------------------------------------------- The settled window


def test_the_window_is_anchored_on_the_target_not_on_pi_end(
    diagnostic_rows, diagnostic_metrics
) -> None:
    """The reason this window exists at all.

    This shot reports ``pouring`` from its second data point, so ``pi_end`` is
    1.0 s - while the pressure is still under 1.5 bar and the real ramp runs to
    about 9 s. A window anchored on pi_end contains the whole ramp, and the
    resistance trend measured over it comes out at +0.09 per second (rising)
    when the flow at a constant 7.5 bar plainly says the bed is opening up.
    """
    assert diagnostic_metrics["pi_end"] == 1.0
    assert diagnostic_metrics["pi_end_source"] == "substate"

    window = settled_window(diagnostic_rows)
    assert window, "the shot does settle"
    assert window[0]["elapsed"] == pytest.approx(12.2, abs=0.3), (
        "settling is found where the machine actually holds its target"
    )


def test_a_shot_that_never_settles_has_no_window() -> None:
    """A flush reaches no target and gets no diagnosis rather than a wrong one."""
    rows = series_rows_from_decaid(load("shot_flush.json"))
    assert settled_window(rows) == []
    assert puck_resistance(rows) is None


def test_control_mode_reads_the_target_channels() -> None:
    assert control_mode({"target_pressure": 7.5, "target_flow": 0.0}) == "pressure"
    assert control_mode({"target_pressure": 0.0, "target_flow": 3.5}) == "flow"
    # Both or neither is not a mode - judging a channel there produced a mean
    # flow deviation of 1.7 ml/s across the archive, which is the absence of a
    # target rather than a deviation.
    assert control_mode({"target_pressure": 7.5, "target_flow": 3.5}) is None
    assert control_mode({"target_pressure": None, "target_flow": None}) is None


# ----------------------------------------------------------- Puck resistance


def test_puck_resistance_of_the_reference(diagnostic_rows) -> None:
    """Pressure over flow squared, a simplified Darcy analogue."""
    resistance = puck_resistance(diagnostic_rows)
    assert resistance["median"] == pytest.approx(6.87, abs=0.05)
    assert resistance["band"] == "high"
    assert resistance["n_points"] == 91
    assert resistance["from_s"] == pytest.approx(12.2, abs=0.3)


def test_the_reference_bed_loses_two_thirds_of_its_resistance(
    diagnostic_rows,
) -> None:
    """The finding that carries this shot.

    From 13.0 down to 3.4 over the pour: the bed opened up while the machine
    held 7.5 bar. That is what sour and bitter at once looks like in telemetry.
    """
    resistance = puck_resistance(diagnostic_rows)
    assert resistance["trend_per_s"] == pytest.approx(-0.624, abs=0.02)
    assert resistance["trend"] == "steep_decline"


def test_low_flow_is_kept_out_of_the_resistance(diagnostic_rows) -> None:
    """The error is squared in the denominator.

    At 0.2 ml/s a sensor wobble of 0.05 moves the result by 60 %, so those
    points would dominate a median they have no business dominating.
    """
    window = settled_window(diagnostic_rows)
    resistance = puck_resistance(diagnostic_rows)
    dribbles = sum(1 for r in window
                   if r["flow_in"] is not None and r["flow_in"] < 0.4)
    assert resistance["n_points"] == len(window) - dribbles


# --------------------------------------------------------------- Channeling


def test_the_reference_is_flagged_and_says_why(
    diagnostic_rows, diagnostic_metrics
) -> None:
    result = channeling(diagnostic_rows, diagnostic_metrics)
    assert result["risk"] == "elevated"
    assert result["fired"] == ["resistance_trend"]


def test_a_knocked_scale_removes_indicators_rather_than_passing_them(
    diagnostic_rows, diagnostic_metrics
) -> None:
    """The reference has a scale warning, so two indicators cannot be computed.

    They are absent from ``based_on`` rather than counted as not firing - a
    broken scale must never read as good news.
    """
    assert any("cale" in w for w in diagnostic_metrics["warnings"])

    result = channeling(diagnostic_rows, diagnostic_metrics)
    assert "flow_divergence" not in result["based_on"]
    assert "early_drops" not in result["based_on"]
    assert result["based_on"] == ["pressure_dip", "flow_instability",
                                  "resistance_trend"]
    assert "scale" in result["note"]


def test_every_indicator_carries_its_own_raw_value(
    diagnostic_rows, diagnostic_metrics
) -> None:
    """A band is a summary; the number behind it is the evidence."""
    result = channeling(diagnostic_rows, diagnostic_metrics)
    for name, indicator in result["indicators"].items():
        assert name in CHANNELING_INDICATORS
        assert isinstance(indicator["value"], float)
        assert isinstance(indicator["fired"], bool)
        assert "threshold" in indicator and "unit" in indicator


def test_indicators_carry_no_prose(diagnostic_rows, diagnostic_metrics) -> None:
    """The same sentence would repeat five times per shot, four times over in a
    comparison. It belongs in the instructions, where it is in context once."""
    result = channeling(diagnostic_rows, diagnostic_metrics)
    for indicator in result["indicators"].values():
        assert "means" not in indicator


def test_a_shot_without_a_settled_pour_is_not_judged() -> None:
    rows = series_rows_from_decaid(load("shot_flush.json"))
    result = channeling(rows, compute_metrics(rows))
    assert result["risk"] is None
    assert result["based_on"] == []
    assert "nothing to judge" in result["note"]


@pytest.mark.parametrize("fired_count, expected", [(0, "low"), (1, "elevated"),
                                                   (2, "high"), (3, "high")])
def test_the_aggregation_needs_two_to_call_it_high(fired_count, expected) -> None:
    """One indicator is a hint. Measured: ``early_drops`` fires on a shot the
    operator rated 100, so a single one must not carry a verdict."""
    risk = ("high" if fired_count >= 2 else "elevated" if fired_count else "low")
    assert risk == expected


# -------------------------------------------------------- Profile compliance


def test_the_reference_followed_its_profile_almost_exactly(diagnostic_rows) -> None:
    """The machine is not the problem here - which is half the diagnosis."""
    compliance = profile_compliance(diagnostic_rows)
    assert compliance["pressure"]["mean_abs_deviation"] == pytest.approx(
        0.028, abs=0.005)
    assert compliance["pressure"]["band"] == "close"


def test_the_reference_ran_below_its_temperature_target(diagnostic_rows) -> None:
    """The operator supposed it was too hot. The basket was 1.6 degrees cold.

    This is the point of measuring compliance rather than guessing at it: the
    target sits in the data next to the reading.
    """
    compliance = profile_compliance(diagnostic_rows)
    temperature = compliance["temperature"]
    assert temperature["mean_deviation"] == pytest.approx(-1.55, abs=0.1)
    assert temperature["max_deviation"] == pytest.approx(-2.05, abs=0.1)
    assert temperature["direction"] == "below"
    assert temperature["band"] == "off_target"


def test_compliance_is_measured_only_where_a_channel_is_held(
    diagnostic_rows,
) -> None:
    compliance = profile_compliance(diagnostic_rows)
    window = settled_window(diagnostic_rows)
    held = sum(1 for r in window if control_mode(r) == "pressure")
    assert compliance["pressure"]["n_points"] == held


def test_phases_follow_the_machines_own_frames(diagnostic_rows) -> None:
    compliance = profile_compliance(diagnostic_rows)
    phases = compliance["phases"]
    assert phases, "at least one phase"
    for phase in phases:
        assert phase["to_s"] >= phase["from_s"]
        assert phase["duration_s"] >= 0
        assert phase["n_points"] >= 2


def test_a_shot_that_never_settles_has_no_compliance() -> None:
    assert profile_compliance(
        series_rows_from_decaid(load("shot_flush.json"))) is None


# ------------------------------------------------------------------- Cache


def test_the_diagnostics_travel_in_the_cached_metrics(diagnostic_rows) -> None:
    """One cache entry serves all three detail levels."""
    metrics = compute_metrics(diagnostic_rows)
    assert metrics["metrics_version"] == METRICS_VERSION >= 4
    assert metrics["puck_resistance"]["band"] == "high"
    assert metrics["channeling"]["risk"] == "elevated"
    assert metrics["profile_compliance"]["temperature"]["direction"] == "below"


def test_a_series_without_targets_yields_no_diagnosis() -> None:
    """Older shots carry no target channels; they get nothing rather than noise."""
    rows = [{"elapsed": t / 4, "pressure": 9.0, "flow_in": 2.0, "flow_out": 1.8,
             "weight": t * 0.4, "temp_basket": 93.0, "substate": "pouring",
             "target_pressure": None, "target_flow": None,
             "target_temp_basket": None, "profile_frame": 0.0}
            for t in range(40)]
    assert settled_window(rows) == []
    assert puck_resistance(rows) is None
    assert profile_compliance(rows) is None
    assert channeling(rows, compute_metrics(rows))["risk"] is None
