"""Derived shot metrics (SPEC §8, revision 1.1).

Every definition is deterministic, so values stay comparable across shots. If a
basis is missing the field is ``None`` and the reason is given in ``warnings``.

Units: pressure bar, flow ml/s, weight g, temperature degrees Celsius, time s.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .db import Database

log = logging.getLogger(__name__)

#: Stored alongside the cache. Bump it as soon as a definition changes - the
#: next access then recomputes.
#: 1 -> 2: scale plausibility (an untared scale invalidates t_first_drops, a
#:         mean flow <= 0 invalidates flow_stability).
#: 2 -> 3: source of pi_end. Decaid reports the machine state in plain text
#:         (``substate``) instead of as a square wave, so the end of
#:         preinfusion is read off rather than inferred (SPEC §8.2).
#: 3 -> 4: puck diagnostics. Resistance, the channeling indicators and profile
#:         compliance join the cached result (SPEC §8.5-8.7).
METRICS_VERSION = 4

FIRST_DROPS_WEIGHT_G = 0.3
#: Window within which a tared scale must still read 0.
TARE_CHECK_WINDOW_S = 0.5
PI_END_PRESSURE_FRACTION = 0.6
INFUSION_WINDOW_TAIL_S = 2.0
DIP_WINDOW_S = 4.0
END_WINDOW_S = 2.0

#: The very first change sits at t < 0.1 s and marks the start of the shot,
#: not a phase transition.
START_MARKER_MAX_S = 0.5

#: The states during and after preinfusion, as Decaid reports them.
SUBSTATE_PREINFUSION = "preinfusion"
SUBSTATE_POURING = "pouring"


def compute_metrics(
    series: Sequence[Mapping[str, Any]],
    *,
    dose_g: float | None = None,
    yield_g: float | None = None,
) -> dict[str, Any]:
    """Metrics from a shot's time series.

    ``series`` are rows from ``shot_series`` (ascending by ``elapsed``).
    """
    warnings: list[str] = []
    rows = sorted(series, key=lambda r: r["elapsed"])
    if not rows:
        return _empty(["No time series recorded."])

    times = [float(r["elapsed"]) for r in rows]
    pressure = _channel(rows, "pressure")
    flow_out = _channel(rows, "flow_out")
    weight = _channel(rows, "weight")
    temp_basket = _channel(rows, "temp_basket")

    duration_s = times[-1]

    max_pressure_global, t_max_global = _argmax(times, pressure)
    if max_pressure_global is None:
        warnings.append("No pressure readings - pressure metrics are dropped.")

    boundaries = phase_boundaries(rows)
    if not boundaries:
        warnings.append("No machine phase markers - pi_end from the heuristic.")

    pi_end, pi_end_source = _pi_end(rows, times, pressure, boundaries, max_pressure_global)
    if pi_end is None:
        warnings.append("pi_end not determinable - the pour phase stays open.")

    # --- Infusionsmaximum (SPEC ss8 1.1) --------------------------------------
    peak_pressure_infusion: float | None = None
    t_peak: float | None = None
    if pi_end is not None and max_pressure_global is not None:
        window = _slice(times, 0.0, pi_end + INFUSION_WINDOW_TAIL_S)
        peak_pressure_infusion, t_peak = _argmax(
            [times[i] for i in window], [pressure[i] for i in window]
        )

    pressure_dip_after_peak = None
    if t_peak is not None and peak_pressure_infusion is not None:
        dip_window = _slice(times, t_peak, t_peak + DIP_WINDOW_S)
        lows = [pressure[i] for i in dip_window if pressure[i] is not None]
        if lows:
            pressure_dip_after_peak = peak_pressure_infusion - min(lows)

    # --- Bezugsphase [pi_end, Ende] -------------------------------------------
    pour = _slice(times, pi_end, duration_s) if pi_end is not None else []
    pour_flow = [flow_out[i] for i in pour if flow_out[i] is not None]
    pour_temp = [temp_basket[i] for i in pour if temp_basket[i] is not None]

    avg_flow_pour = _mean(pour_flow)
    flow_stability = _coefficient_of_variation(pour_flow)
    if avg_flow_pour is not None and avg_flow_pour <= 0:
        # A coefficient of variation around a mean <= 0 cannot be interpreted
        # (it turns negative). The cause is almost always a scale that jumped
        # during the pour.
        flow_stability = None
        warnings.append(
            f"Mean pour flow is {avg_flow_pour:.2f} ml/s (<= 0) - "
            "flow_stability is dropped, check the scale readings."
        )
    pressure_trend_pour = _slope(
        [times[i] for i in pour], [pressure[i] for i in pour]
    )
    temp_basket_mean = _mean(pour_temp)
    temp_basket_std = _stdev(pour_temp)
    if not pour_temp:
        warnings.append("No basket temperature during the pour phase.")

    # --- Ende -----------------------------------------------------------------
    end_window = _slice(times, duration_s - END_WINDOW_S, duration_s)
    end_values = [pressure[i] for i in end_window if pressure[i] is not None]
    end_pressure = _mean(end_values)

    t_first_drops = next(
        (t for t, w in zip(times, weight, strict=True)
         if w is not None and w > FIRST_DROPS_WEIGHT_G),
        None,
    )
    measured = [w for w in weight if w is not None]
    tare_offset = max(
        (w for t, w in zip(times, weight, strict=True)
         if w is not None and t <= TARE_CHECK_WINDOW_S),
        default=None,
    )
    if t_first_drops is None:
        warnings.append(
            f"Weight never exceeds {FIRST_DROPS_WEIGHT_G} g - no scale?"
        )
    elif tare_offset is not None and tare_offset > FIRST_DROPS_WEIGHT_G:
        # Nothing can be in the cup in the first half second - the machine
        # preinfuses for seconds. If the scale already shows weight there it
        # was not tared, and t_first_drops would say something about the cup
        # rather than the shot (SPEC §8: a missing basis
        # -> null plus a warning).
        t_first_drops = None
        warnings.append(
            f"Scale already reads {tare_offset:.1f} g in the first second "
            "(not tared) - t_first_drops not determinable."
        )
    if measured and min(measured) < 0:
        warnings.append(
            f"Weight goes negative (min {min(measured):.1f} g) - "
            "the scale was probably knocked; flow-based values are unreliable."
        )

    ratio = round(yield_g / dose_g, 3) if dose_g and yield_g else None
    if ratio is None:
        warnings.append("Dose or yield missing - ratio is dropped.")

    result = {
        "metrics_version": METRICS_VERSION,
        "t_first_drops": _r_time(t_first_drops),
        "pi_end": _r_time(pi_end),
        "pi_end_source": pi_end_source,
        "peak_pressure_infusion": _r_pressure(peak_pressure_infusion),
        "t_peak": _r_time(t_peak),
        "max_pressure_global": _r_pressure(max_pressure_global),
        "t_max_pressure_global": _r_time(t_max_global),
        "pressure_dip_after_peak": _r_pressure(pressure_dip_after_peak),
        "avg_flow_pour": _r_flow(avg_flow_pour),
        "flow_stability": _round(flow_stability, 3),
        "end_pressure": _r_pressure(end_pressure),
        "pressure_trend_pour": _round(pressure_trend_pour, 3),
        "temp_basket_mean": _r_pressure(temp_basket_mean),
        "temp_basket_std": _r_pressure(temp_basket_std),
        "duration_s": _r_time(duration_s),
        "ratio": ratio,
        "n_points": len(rows),
        "warnings": warnings,
    }

    # The diagnostics read the target channels and need the metrics above, so
    # they come last. They travel in the same cache entry: computing them is
    # cheap next to fetching the series, and every detail level is then served
    # from one row.
    result["puck_resistance"] = puck_resistance(rows)
    result["channeling"] = channeling(rows, result)
    result["profile_compliance"] = profile_compliance(rows)
    return result


def metrics_for_shot(db: Database, shot_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Metrics from the cache, otherwise computed and stored (SPEC §8).

    ``None`` if the shot does not exist. The cache expires on its own as soon
    as ``METRICS_VERSION`` rises or the shot is rewritten.
    """
    basics = db.shot_basics(shot_id)
    if basics is None:
        return None

    if not refresh:
        cached = db.get_cached_metrics(shot_id, METRICS_VERSION)
        if cached is not None:
            return cached

    metrics = compute_metrics(
        [dict(row) for row in db.series_for_shot(shot_id)],
        dose_g=basics["dose_g"],
        yield_g=basics["yield_g"],
    )
    db.store_metrics(shot_id, METRICS_VERSION, metrics)
    return metrics


def warm_metrics_cache(db: Database) -> int:
    """Computes missing or stale metrics. Idempotent, without network access."""
    pending = db.shot_ids_without_metrics(METRICS_VERSION)
    for shot_id in pending:
        metrics_for_shot(db, shot_id, refresh=True)
    if pending:
        log.info("metrics computed", extra={"fields": {"shots": len(pending)}})
    return len(pending)


#: SPEC §9.1 - channel names in the compact curve output. Short, because every
#: letter lands in the response budget once per data point.
CURVE_CHANNELS = {
    "p": "pressure",
    "fi": "flow_in",
    "fo": "flow_out",
    "w": "weight",
    "tb": "temp_basket",
}

CURVE_ROUNDING = {"t": 2, "p": 2, "fi": 2, "fo": 2, "w": 1, "tb": 1}

MAX_CURVE_POINTS = 400


def downsample_curve(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_points: int = 120,
    keep_times: Sequence[float | None] = (),
) -> dict[str, list[float | None]]:
    """Thins the time series down to ``max_points`` (SPEC ss9.1).

    Evenly across **time**, not across the index - with uneven sampling a
    densely sampled stretch would otherwise stay over-represented. The first
    and last point are guaranteed, as is every moment in ``keep_times`` (the
    two pressure maxima).

    These mandatory points take precedence over ``max_points``: if the budget
    is smaller than their number they all come back anyway. Otherwise a
    ``max_points=2`` could cut away the pressure peak even though it is
    described as guaranteed. The curve is therefore at most four points
    longer than asked for.

    What comes back are parallel arrays (``t``, ``p``, ``fi``, ``fo``,
    ``w``, ``tb``) rather than a list of objects - that saves about 60 % of
    the characters. Channels without a single reading are left out entirely.
    """
    if not rows:
        return {"t": []}
    ordered = sorted(rows, key=lambda r: r["elapsed"])
    times = [float(r["elapsed"]) for r in ordered]
    budget = max(2, min(int(max_points), MAX_CURVE_POINTS))

    mandatory = {0, len(ordered) - 1}
    for wanted in keep_times:
        if wanted is not None:
            mandatory.add(min(range(len(times)), key=lambda i: abs(times[i] - wanted)))

    # Mandatory points must never drop out - the budget is raised if need be.
    effective = max(budget, len(mandatory))

    if len(ordered) <= effective:
        chosen = range(len(ordered))
    else:
        span = times[-1] - times[0]
        slots = max(0, effective - len(mandatory))
        picked = set(mandatory)
        if slots and span > 0:
            for step in range(slots):
                target = times[0] + span * step / max(1, slots - 1)
                picked.add(min(range(len(times)), key=lambda i: abs(times[i] - target)))
        # Grid points can coincide with mandatory ones; the selection then stays
        # smaller than the budget, never larger.
        chosen = sorted(picked)[:effective]

    curve: dict[str, list[float | None]] = {
        "t": [_round(times[i], CURVE_ROUNDING["t"]) for i in chosen]
    }
    for key, column in CURVE_CHANNELS.items():
        values = [
            _round(None if ordered[i].get(column) is None else float(ordered[i][column]),
                   CURVE_ROUNDING[key])
            for i in chosen
        ]
        if any(v is not None for v in values):
            curve[key] = values
    return curve


# --------------------------------------------------------------- Kurvenform
#
# SPEC §9.1: for most questions the *shape* of the curve is enough. It costs
# about a tenth of the point arrays and can be read without arithmetic.

#: Below this change across a segment the curve counts as steady.
FLAT_THRESHOLD = {"p": 0.2, "fo": 0.15}

#: Maximum deviation from the straight line between start and end point, as a
#: fraction of the range in that segment. Above it the curve is not linear.
LINEARITY_TOLERANCE = 0.15

SHAPE_CHANNELS = {"p": "pressure", "fo": "flow_out"}


def curve_shape(
    rows: Sequence[Mapping[str, Any]], metrics: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A segment-by-segment description of the curve instead of raw data points.

    The segment boundaries are the machine's phase markers - the same source as
    ``pi_end``. If a shot has no markers, ``pi_end`` and the end of the shot
    serve as boundaries; ``source`` records which path was taken.

    Per segment and channel: starting and ending value, direction, and whether
    the curve is linear. ``p`` is pressure in bar, ``fo`` the scale-derived
    Fluss in ml/s.
    """
    ordered = sorted(rows, key=lambda r: r["elapsed"])
    if not ordered:
        return {"segments": [], "markers": {}, "source": "none"}

    times = [float(r["elapsed"]) for r in ordered]
    metrics = metrics or {}

    boundaries = phase_boundaries(ordered)
    if boundaries:
        source = "machine"
    elif metrics.get("pi_end") is not None:
        boundaries = [float(metrics["pi_end"])]
        source = "markers"
    else:
        boundaries = []
        source = "none"

    edges = [times[0], *[b for b in boundaries if times[0] < b < times[-1]], times[-1]]

    segments: list[dict[str, Any]] = []
    for start, end in zip(edges, edges[1:], strict=False):
        window = [i for i, t in enumerate(times) if start <= t <= end]
        if len(window) < 2:
            continue
        segment: dict[str, Any] = {"from": _r_time(start), "to": _r_time(end)}
        for key, column in SHAPE_CHANNELS.items():
            described = _describe(
                [times[i] for i in window],
                [None if ordered[i].get(column) is None else float(ordered[i][column])
                 for i in window],
                key,
            )
            if described is not None:
                segment[key] = described
        segments.append(segment)

    markers = {
        name: metrics.get(name)
        for name in ("t_first_drops", "pi_end", "t_peak")
        if metrics.get(name) is not None
    }
    return {"segments": segments, "markers": markers, "source": source}


def _describe(
    times: Sequence[float], values: Sequence[float | None], channel: str
) -> dict[str, Any] | None:
    """Start, end, direction and linearity of one channel within a segment."""
    pairs = [(t, v) for t, v in zip(times, values, strict=True) if v is not None]
    if len(pairs) < 2:
        return None

    first, last = pairs[0][1], pairs[-1][1]
    delta = last - first
    flat = FLAT_THRESHOLD[channel]
    direction = "steady" if abs(delta) < flat else ("rising" if delta > 0 else "falling")

    digits = 1 if channel == "p" else 2
    return {
        "from": _round(first, digits),
        "to": _round(last, digits),
        "dir": direction,
        "linear": _is_linear(pairs, flat),
    }


def _is_linear(pairs: Sequence[tuple[float, float]], flat: float) -> bool:
    """Does the curve deviate noticeably from the straight line between its ends?

    A flat segment always counts as linear - the tiny range there would
    otherwise make the relative deviation arbitrarily large.
    """
    if len(pairs) < 3:
        return True
    span = max(v for _, v in pairs) - min(v for _, v in pairs)
    if span < flat:
        return True

    (t0, v0), (t1, v1) = pairs[0], pairs[-1]
    if t1 == t0:
        return True
    slope = (v1 - v0) / (t1 - t0)
    deviation = max(abs(v - (v0 + slope * (t - t0))) for t, v in pairs)
    return deviation / span <= LINEARITY_TOLERANCE


def substate_change(rows: Sequence[Mapping[str, Any]], target: str) -> float | None:
    """The first moment at which the machine reports ``target``.

    ``state.substate`` is Decaid's plain-text state of the shot
    (``preparingForShot`` -> ``preinfusion`` -> ``pouring``). That is the
    machine speaking, not a threshold.
    """
    for row in rows:
        if str(row.get("substate") or "") == target:
            return float(row["elapsed"])
    return None


def frame_boundaries(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    """Changes of the profile step number, without the predecessor's leftovers.

    Measured: on the first data point ``profileFrame`` still holds the value of
    the preceding shot, so a change at t < 0.5 s is the cleanup rather than a
    step change. It is discarded (SPEC §8.2).
    """
    boundaries: list[float] = []
    previous: Any = _UNSET
    for row in rows:
        frame = row.get("profile_frame")
        if previous is not _UNSET and frame != previous:
            boundaries.append(float(row["elapsed"]))
        previous = frame
    return [t for t in boundaries if t > START_MARKER_MAX_S]


def phase_boundaries(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    """The machine's phase boundaries, best available source first.

    ``substate`` names the phase, ``profile_frame`` only the step. Where both
    are present the state wins: it says *what* is happening, not merely *that*
    something changed.
    """
    ordered = sorted(rows, key=lambda r: r["elapsed"])
    marks: list[float] = []
    previous: Any = _UNSET
    for row in ordered:
        sub = row.get("substate")
        if sub is None:
            continue
        if previous is not _UNSET and sub != previous:
            marks.append(float(row["elapsed"]))
        previous = sub
    marks = [t for t in marks if t > START_MARKER_MAX_S]
    return marks or frame_boundaries(ordered)


def _pi_end(
    rows: Sequence[Mapping[str, Any]],
    times: Sequence[float],
    pressure: Sequence[float | None],
    boundaries: Sequence[float],
    max_pressure_global: float | None,
) -> tuple[float | None, str | None]:
    """End of preinfusion (SPEC §8 1.1, §8.2).

    Three sources, in this order; ``pi_end_source`` names the one actually
    used:

    ``substate``       The machine reports the change to ``pouring`` in plain
                       text. Read off, not inferred.
    ``profile_frame``  Without a state report, the last step boundary before
                       the pressure anchor. Values before the shot start do
                       not count.
    ``heuristic``      Without either, the moment pressure first reaches
                       ``0.6 x max_pressure_global``. An approximation - not
                       comparable down to a tenth of a second.
    """
    ordered = sorted(rows, key=lambda r: r["elapsed"])
    pouring = substate_change(ordered, SUBSTATE_POURING)
    if pouring is not None and pouring > START_MARKER_MAX_S:
        return pouring, "substate"

    if max_pressure_global is None or max_pressure_global <= 0:
        return (boundaries[0], "profile_frame") if boundaries else (None, None)

    threshold = PI_END_PRESSURE_FRACTION * max_pressure_global
    anchor = next(
        (t for t, p in zip(times, pressure, strict=True) if p is not None and p >= threshold),
        None,
    )
    if anchor is None:
        return (boundaries[0], "profile_frame") if boundaries else (None, None)

    earlier = [t for t in boundaries if t <= anchor]
    if earlier:
        return max(earlier), "profile_frame"
    return anchor, "heuristic"


# ---------------------------------------------------------------- Hilfsmittel

_UNSET = object()


def _channel(rows: Sequence[Mapping[str, Any]], key: str) -> list[float | None]:
    return [None if r.get(key) is None else float(r[key]) for r in rows]


def _slice(times: Sequence[float], start: float, end: float) -> list[int]:
    return [i for i, t in enumerate(times) if start <= t <= end]


def _argmax(
    times: Sequence[float], values: Sequence[float | None]
) -> tuple[float | None, float | None]:
    pairs = [(v, t) for t, v in zip(times, values, strict=True) if v is not None]
    if not pairs:
        return None, None
    value, at = max(pairs, key=lambda pair: pair[0])
    return value, at


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _coefficient_of_variation(values: Sequence[float]) -> float | None:
    mean = _mean(values)
    stdev = _stdev(values)
    if mean is None or stdev is None or mean == 0:
        return None
    return stdev / mean


def _slope(times: Sequence[float], values: Sequence[float | None]) -> float | None:
    """Slope of a least-squares fit, in units per second."""
    pairs = [(t, v) for t, v in zip(times, values, strict=True) if v is not None]
    if len(pairs) < 2:
        return None
    n = len(pairs)
    mean_t = sum(t for t, _ in pairs) / n
    mean_v = sum(v for _, v in pairs) / n
    denominator = sum((t - mean_t) ** 2 for t, _ in pairs)
    if denominator == 0:
        return None
    return sum((t - mean_t) * (v - mean_v) for t, v in pairs) / denominator


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _r_pressure(value: float | None) -> float | None:
    return _round(value, 1)


def _r_flow(value: float | None) -> float | None:
    return _round(value, 2)


def _r_time(value: float | None) -> float | None:
    return _round(value, 1)


def _empty(warnings: list[str]) -> dict[str, Any]:
    keys = (
        "t_first_drops", "pi_end", "pi_end_source", "peak_pressure_infusion", "t_peak",
        "max_pressure_global", "t_max_pressure_global", "pressure_dip_after_peak",
        "avg_flow_pour", "flow_stability", "end_pressure", "pressure_trend_pour",
        "temp_basket_mean", "temp_basket_std", "duration_s", "ratio",
    )
    return {"metrics_version": METRICS_VERSION, **dict.fromkeys(keys),
            "n_points": 0, "warnings": warnings}


# ---------------------------------------------------------------- Diagnostics
#
# Everything below reads the target channels, which is the one thing this data
# allows that a pressure-only log does not: what the machine was *asked* for
# sits next to what it achieved, at every data point. Compliance here is
# measured, not estimated.
#
# The physics and the shape of the bands are informed by the gaggimate-mcp
# project (MIT). The numbers are not theirs: their sampling is 100 ms and ours
# is ~250 ms, so every threshold below was read off this archive's own
# distribution. Where a threshold sits at a percentile, that percentile is the
# justification.

#: How close the pressure must be to its target before the pour counts settled.
SETTLE_TOLERANCE_BAR = 0.6

#: And how long after that the puck is still compacting. Measured: without a
#: delay the robust trend and the plain first-to-last direction agree on only
#: 40 % of shots, because the window still holds the pressure ramp. At 4 s they
#: agree on 85 % and only 7 of 149 shots lose their window; 6 s buys 92 % for
#: one further shot lost, which is not worth the data.
SETTLE_DELAY_S = 4.0

#: Below this a window is too short to say anything with.
MIN_SETTLED_POINTS = 8

#: Flow below this is left out of the resistance. The error is squared in the
#: denominator, so at 0.2 ml/s a sensor wobble of 0.05 moves the result by 60 %.
MIN_RESISTANCE_FLOW = 0.4

#: Puck resistance in bar·s²/ml². These bands are this archive's own quartiles
#: rather than the ones gaggimate-mcp validated: on this grinder and these
#: profiles their scale would call half of all shots "high".
#: Measured here: p50 = 3.9, p90 = 8.5.
RESISTANCE_BANDS = ((1.0, "very_low"), (2.5, "low"), (5.0, "moderate"),
                    (9.0, "high"))

#: Change of resistance across the settled window, per second. A bed that loses
#: resistance while the pressure is held is opening up. Measured: p50 = -0.11,
#: p90 = +0.05, and the steepest 15 % sit below -0.45.
TREND_STEEP_DECLINE = -0.45
TREND_FLAT = 0.15

#: The four channeling indicators. Each threshold is a percentile of this
#: archive, picked so that an indicator marks a minority rather than a mood.
#: Fire rates measured across 165 shots: 4.8, 7.3, 1.2 and 7.3 %.
CHANNELING_DIP_BAR = 0.5          # p95 of pressure_dip_after_peak
CHANNELING_JITTER = 0.10          # p90 of flow jitter while the target is held
CHANNELING_DIVERGENCE = 0.30      # p99 of pump flow minus scale flow
CHANNELING_EARLY_DROPS_S = 5.0    # p90 of pi_end - t_first_drops

#: Temperature. The DE1 is specified to ±1 °C, and 2 °C is where a trained
#: taster reliably notices a difference - so 1 °C is off target and 2 °C is
#: notable.
TEMP_OFF_TARGET_C = 1.0
TEMP_NOTABLE_C = 2.0

#: Profile compliance, per control mode. Pressure measured p50 = 0.09 bar, so a
#: quarter of a bar is already unusual. Flow measured p50 = 0.26 ml/s.
COMPLIANCE_PRESSURE_GOOD = 0.25
COMPLIANCE_PRESSURE_NOTABLE = 0.50
COMPLIANCE_FLOW_GOOD = 0.30
COMPLIANCE_FLOW_NOTABLE = 0.70

#: Names of the indicators, in the order a reader should meet them.
CHANNELING_INDICATORS = (
    "pressure_dip", "flow_instability", "flow_divergence", "early_drops",
    "resistance_trend",
)


def control_mode(row: Mapping[str, Any]) -> str | None:
    """Which channel the machine is holding at this data point.

    A pressure-held stretch leaves the flow target at 0 and the other way
    round. Judging both channels everywhere gave a mean flow deviation of
    1.7 ml/s across the archive, which is not a deviation - it is the absence
    of a target.
    """
    target_p = _num(row.get("target_pressure"))
    target_f = _num(row.get("target_flow"))
    holds_pressure = target_p is not None and target_p > 0.5
    holds_flow = target_f is not None and target_f > 0.1
    if holds_pressure and not holds_flow:
        return "pressure"
    if holds_flow and not holds_pressure:
        return "flow"
    return None


def settled_window(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The stretch in which the machine is holding its pressure target.

    ``pi_end`` marks the end of preinfusion as the machine reports it, and on
    some profiles that is the first second of the shot - the machine calls
    everything ``pouring`` from the start. A window anchored there still
    contains the pressure ramp, and a resistance trend measured over it comes
    out with the wrong sign: on the reference shot +0.09 per second (rising),
    while the flow at a constant 7.5 bar plainly says the bed is opening up.

    Anchoring on the target fixes that, and the target is in the data.
    """
    start = None
    for row in rows:
        pressure = _num(row.get("pressure"))
        target = _num(row.get("target_pressure"))
        if pressure is None or target is None or target <= 1.0:
            continue
        if abs(pressure - target) <= SETTLE_TOLERANCE_BAR:
            start = _num(row.get("elapsed"))
            break
    if start is None:
        return []
    window = [r for r in rows
              if (_num(r.get("elapsed")) or 0.0) >= start + SETTLE_DELAY_S]
    return window if len(window) >= MIN_SETTLED_POINTS else []


def puck_resistance(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Resistance of the coffee bed, as ``pressure / flow²``.

    A simplified Darcy analogue: for flow through a porous bed the pressure
    rises roughly with the square of the flow, so ``P/F²`` stays near-constant
    while the bed does. It is not an absolute physical quantity - it compares
    shots on the same machine, and above all it shows how one shot changes
    within itself.

    Unit bar·s²/ml². Taken over the settled window with the **pump** flow: that
    is the water going into the puck, whereas the scale sees what comes out,
    seconds later and smoothed by the basket.

    ``None`` when the shot never settles - a flush, an abort, or a profile that
    never holds a pressure.
    """
    window = settled_window(rows)
    if not window:
        return None

    points: list[tuple[float, float]] = []
    for row in window:
        pressure = _num(row.get("pressure"))
        flow = _num(row.get("flow_in"))
        elapsed = _num(row.get("elapsed"))
        if pressure is None or flow is None or elapsed is None:
            continue
        if flow < MIN_RESISTANCE_FLOW or pressure <= 0:
            continue
        points.append((elapsed, pressure / (flow * flow)))

    if len(points) < MIN_SETTLED_POINTS:
        return None

    median = statistics.median(value for _t, value in points)
    trend = _theil_sen(points)
    return {
        "median": round(median, 2),
        "band": _band(median, RESISTANCE_BANDS, "very_high"),
        "trend_per_s": None if trend is None else round(trend, 3),
        "trend": _trend_band(trend),
        "from_s": _r_time(points[0][0]),
        "to_s": _r_time(points[-1][0]),
        "n_points": len(points),
    }


def channeling(
    rows: Sequence[Mapping[str, Any]], metrics: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Five independent signs that water found a path around the coffee.

    Each one has its own physical signature and keeps its own raw value; the
    aggregate is a summary, never a verdict. An indicator that could not be
    computed is absent from ``based_on`` rather than counted as passing - a
    knocked scale must not read as good news.

    **Why five and not the four in the brief.** Measured against the operator's
    own notes across 165 shots, the four specified indicators fire on 4 of the
    10 worst-rated shots - and also on a shot rated 100. The resistance trend
    separates them: the three lowest-rated shots all combine a high resistance
    with a steep decline (7.4/-0.25, 18.3/-1.06, 32.7/-1.92), and the two the
    operator labelled "channeling" himself sit at the other extreme, with a
    resistance of 0.6 and 1.7 against a median of 3.9. The four stay - they are
    physically sound and will matter on data where those failure modes occur -
    but leaving the one signal that actually discriminates out of the aggregate
    would make the aggregate worse than its parts.
    """
    metrics = metrics or {}
    window = settled_window(rows)
    if not window:
        # No settled pour, nothing to judge. The pressure dip could still be
        # computed from the metrics alone, but a verdict of "low risk" built on
        # one indicator out of five would claim more than a flush or an abort
        # can support.
        return {
            "risk": None, "fired": [], "based_on": [], "indicators": {},
            "note": "No settled pour - nothing to judge.",
        }

    resistance = puck_resistance(rows)
    scale_unreliable = any(
        "cale" in w or "eight" in w for w in metrics.get("warnings", ())
    )

    indicators: dict[str, dict[str, Any]] = {}

    # (a) A pressure dip right after the infusion peak: the bed gave way and
    #     the pump briefly lost against it.
    dip = _num(metrics.get("pressure_dip_after_peak"))
    if dip is not None:
        indicators["pressure_dip"] = _indicator(dip, CHANNELING_DIP_BAR, "bar")

    # (b) Flow refusing to sit still while the machine holds a constant target.
    #     Judged only inside target-constant stretches, so a profile that ramps
    #     on purpose is not mistaken for an unstable puck.
    jitter = _flow_jitter(window)
    if jitter is not None:
        indicators["flow_instability"] = _indicator(
            jitter, CHANNELING_JITTER, "ml/s")

    # (c) The pump delivering more than the scale sees. Persistently, that is
    #     liquid going somewhere other than the cup - spray past the basket, or
    #     a path around the puck.
    if not scale_unreliable:
        divergence = _flow_divergence(window)
        if divergence is not None:
            indicators["flow_divergence"] = _indicator(
                divergence, CHANNELING_DIVERGENCE, "ml/s")

        # (d) Liquid in the cup before preinfusion was over. Nothing should
        #     come through a properly wetted bed that early.
        pi_end = _num(metrics.get("pi_end"))
        first_drops = _num(metrics.get("t_first_drops"))
        if pi_end is not None and first_drops is not None:
            indicators["early_drops"] = _indicator(
                pi_end - first_drops, CHANNELING_EARLY_DROPS_S, "s")

    # (e) The bed losing resistance while the pressure is held - see the
    #     docstring for why this is in the aggregate.
    if resistance and resistance["trend_per_s"] is not None:
        trend = resistance["trend_per_s"]
        indicators["resistance_trend"] = {
            "value": trend,
            "unit": "per s",
            "threshold": TREND_STEEP_DECLINE,
            "fired": trend <= TREND_STEEP_DECLINE,
        }

    fired = [name for name, ind in indicators.items() if ind["fired"]]
    based_on = [n for n in CHANNELING_INDICATORS if n in indicators]

    if len(fired) >= 2:
        risk = "high"
    elif fired:
        risk = "elevated"
    else:
        risk = "low"

    result: dict[str, Any] = {
        "risk": risk,
        "fired": fired,
        "based_on": based_on,
        "indicators": indicators,
    }
    if scale_unreliable:
        result["note"] = (
            "The scale readings are unreliable for this shot, so the two "
            "indicators that depend on them were not computed."
        )
    return result


def profile_compliance(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """How closely the machine followed what the profile asked for.

    Measured, not estimated: the target sits next to the reading at every data
    point. Each channel is judged only where it is the one being held
    (``control_mode``), and the phases are the machine's own ``profile_frame``.

    Temperature is separate and always judged: the group is meant to hold its
    target throughout, whatever the pressure or flow is doing.
    """
    window = settled_window(rows)
    if not window:
        return None

    pressure_devs = [
        abs(p - t) for r in window
        if control_mode(r) == "pressure"
        and (p := _num(r.get("pressure"))) is not None
        and (t := _num(r.get("target_pressure"))) is not None
    ]
    flow_devs = [
        abs(f - t) for r in window
        if control_mode(r) == "flow"
        and (f := _num(r.get("flow_in"))) is not None
        and (t := _num(r.get("target_flow"))) is not None
    ]
    temp_devs = [
        b - t for r in window
        if (b := _num(r.get("temp_basket"))) is not None
        and (t := _num(r.get("target_temp_basket"))) is not None
    ]

    result: dict[str, Any] = {"phases": _phase_compliance(rows)}

    if pressure_devs:
        mad = _mean(pressure_devs)
        result["pressure"] = {
            # Three decimals, not the usual one: the median shot tracks its
            # target to 0.09 bar, and rounding that to 0.1 would erase the
            # whole scale this band sits on.
            "mean_abs_deviation": _round(mad, 3),
            "band": _band(mad, ((COMPLIANCE_PRESSURE_GOOD, "close"),
                                (COMPLIANCE_PRESSURE_NOTABLE, "loose")), "off"),
            "n_points": len(pressure_devs),
        }
    if flow_devs:
        mad = _mean(flow_devs)
        result["flow"] = {
            "mean_abs_deviation": _round(mad, 3),
            "band": _band(mad, ((COMPLIANCE_FLOW_GOOD, "close"),
                                (COMPLIANCE_FLOW_NOTABLE, "loose")), "off"),
            "n_points": len(flow_devs),
        }
    if temp_devs:
        mean_dev = _mean(temp_devs)
        worst = max(temp_devs, key=abs)
        result["temperature"] = {
            "mean_deviation": _round(mean_dev, 2),
            "max_deviation": _round(worst, 2),
            "band": ("on_target" if abs(mean_dev) < TEMP_OFF_TARGET_C
                     else ("off_target" if abs(mean_dev) < TEMP_NOTABLE_C
                           else "notable")),
            "direction": "below" if mean_dev < 0 else "above",
            "n_points": len(temp_devs),
        }
    return result


def _phase_compliance(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One entry per ``profile_frame`` the machine actually ran through.

    The first frame value belongs to the previous shot (see ``frame_changes``),
    so a frame that only appears in the first half second is dropped.
    """
    phases: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []
    frame = object()

    for row in rows:
        this = row.get("profile_frame")
        if this != frame:
            if current:
                phases.append(current)
            current, frame = [], this
        current.append(row)
    if current:
        phases.append(current)

    out = []
    for group in phases:
        times = [_num(r.get("elapsed")) or 0.0 for r in group]
        if len(group) < 2 or times[-1] < START_MARKER_MAX_S:
            continue
        entry: dict[str, Any] = {
            "frame": group[0].get("profile_frame"),
            "from_s": _r_time(times[0]),
            "to_s": _r_time(times[-1]),
            "duration_s": _r_time(times[-1] - times[0]),
            "substates": sorted({r.get("substate") for r in group
                                 if r.get("substate")}) or None,
            "n_points": len(group),
        }
        modes = {control_mode(r) for r in group} - {None}
        entry["holds"] = sorted(modes) or None

        for channel, key, target_key, rounder in (
            ("pressure", "pressure", "target_pressure", _r_pressure),
            ("flow", "flow_in", "target_flow", _r_flow),
        ):
            values = [v for r in group if (v := _num(r.get(key))) is not None]
            devs = [
                abs(v - t) for r in group
                if control_mode(r) == channel
                and (v := _num(r.get(key))) is not None
                and (t := _num(r.get(target_key))) is not None
            ]
            if values:
                entry[f"{channel}_mean"] = rounder(_mean(values))
            if devs:
                entry[f"{channel}_deviation"] = _round(_mean(devs), 3)

        temps = [v for r in group if (v := _num(r.get("temp_basket"))) is not None]
        if temps:
            entry["temp_basket_mean"] = _round(_mean(temps), 1)
        out.append(entry)
    return out


# ------------------------------------------------------- Diagnostic helpers


def _indicator(value: float, threshold: float, unit: str) -> dict[str, Any]:
    """One indicator, as a number and a verdict.

    Deliberately without a sentence explaining what it means: that sentence is
    the same for every shot and would repeat five times in every response and
    four times over in a comparison. It lives in the server instructions, which
    are in context once.
    """
    return {
        "value": round(value, 3),
        "unit": unit,
        "threshold": threshold,
        "fired": value >= threshold,
    }


def _flow_jitter(window: Sequence[Mapping[str, Any]]) -> float | None:
    """How much the flow moves between neighbouring points while a target is held.

    Restricted to stretches where the machine asks for a constant pressure or a
    constant flow: a profile that ramps on purpose would otherwise read as an
    unstable puck.
    """
    diffs: list[float] = []
    previous_key = None
    previous_flow = None

    for row in window:
        mode = control_mode(row)
        target = _num(row.get("target_pressure") if mode == "pressure"
                      else row.get("target_flow"))
        key = None if mode is None or target is None else (mode, round(target, 2))
        flow = _num(row.get("flow_in"))
        if key is not None and key == previous_key and flow is not None \
                and previous_flow is not None:
            diffs.append(abs(flow - previous_flow))
        previous_key, previous_flow = key, flow

    return _stdev_p(diffs) if len(diffs) >= 4 else None


def _flow_divergence(window: Sequence[Mapping[str, Any]]) -> float | None:
    """Median of pump flow minus scale flow across the settled window.

    Normally slightly negative: the bed releases water it was holding, so the
    scale sees a little more than the pump sent. A persistently *positive*
    value is liquid that never reached the cup.
    """
    pairs = [
        (a, b) for row in window
        if (a := _num(row.get("flow_in"))) is not None
        and (b := _num(row.get("flow_out"))) is not None
    ]
    if len(pairs) < 5:
        return None
    return statistics.median(a - b for a, b in pairs)


def _theil_sen(points: Sequence[tuple[float, float]]) -> float | None:
    """Median of pairwise slopes.

    A least-squares fit on ``P/F²`` is at the mercy of its last few points,
    where a collapsing flow sends the value towards infinity. The median of
    slopes is not. Sampled on a grid so the cost stays linear-ish on a long
    shot.
    """
    n = len(points)
    if n < 4:
        return None
    step = max(1, n // 40)
    slopes = [
        (points[j][1] - points[i][1]) / (points[j][0] - points[i][0])
        for i in range(0, n, step)
        for j in range(i + step, n, step)
        if points[j][0] - points[i][0] > 0.5
    ]
    return statistics.median(slopes) if slopes else None


def _trend_band(trend: float | None) -> str | None:
    if trend is None:
        return None
    if trend <= TREND_STEEP_DECLINE:
        return "steep_decline"
    if trend < -TREND_FLAT:
        return "declining"
    if trend > TREND_FLAT:
        return "rising"
    return "steady"


def _band(value: float, edges: Sequence[tuple[float, str]], above: str) -> str:
    for edge, name in edges:
        if value < edge:
            return name
    return above


def _stdev_p(values: Sequence[float]) -> float | None:
    """Population standard deviation - these are all the differences there are."""
    if not values:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
