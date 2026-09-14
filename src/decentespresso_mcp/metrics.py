"""Derived shot metrics (SPEC §8, revision 1.1).

Every definition is deterministic, so values stay comparable across shots. If a
basis is missing the field is ``None`` and the reason is given in ``warnings``.

Units: pressure bar, flow ml/s, weight g, temperature degrees Celsius, time s.
"""

from __future__ import annotations

import logging
import math
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
#:         preinfusion is read off rather than inferred (SPEC §20.5).
METRICS_VERSION = 3

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
        return _empty(["Keine Zeitreihe vorhanden."])

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

    return {
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
    """Berechnet fehlende oder veraltete Metriken. Idempotent, ohne Netzzugriff."""
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
    """Zeitreihe auf ``max_points`` ausduennen (SPEC ss9.1).

    Evenly across **time**, not across the index - with uneven sampling a
    densely sampled stretch would otherwise stay over-represented. The first
    and last point are guaranteed, as is every moment in ``keep_times`` (the
    two pressure maxima).

    These mandatory points take precedence over ``max_points``: if the budget
    is smaller than their number they all come back anyway. Otherwise a
    ``max_points=2`` could cut away the pressure peak even though it is
    described as guaranteed. The curve is thus at most four points longer than
    angefordert.

    Rueckgabe sind parallele Arrays (``t``, ``p``, ``fi``, ``fo``, ``w``,
    ``tb``) rather than a list of objects - that saves about 60 % of the
    characters. Channels
    ohne einen einzigen Messwert fehlen ganz.
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
# SPEC §17: for most questions the *shape* of the curve is enough. It costs
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

    Measured on 2026-09-14: on the first data point ``profileFrame`` still
    holds the value of the preceding shot - so the change at t < 0.5 s is not a
    step change but the cleanup. It is discarded (SPEC
    ss20.5).
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
    """End of preinfusion (SPEC §8 1.1, §20.5).

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
    """Steigung einer Ausgleichsgeraden (kleinste Quadrate), Einheit pro Sekunde."""
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
