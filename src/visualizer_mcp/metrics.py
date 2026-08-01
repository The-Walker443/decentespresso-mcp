"""Abgeleitete Shot-Metriken (SPEC ss8, Fassung 1.1).

Alle Definitionen sind deterministisch, damit Werte ueber Shots hinweg
vergleichbar bleiben. Fehlt eine Grundlage, ist das Feld ``None`` und der Grund
steht in ``warnings``.

Einheiten: Druck bar, Fluss ml/s, Gewicht g, Temperatur Grad Celsius, Zeit s.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - nur fuer Typpruefer
    from .db import Database

log = logging.getLogger(__name__)

#: Wird mit dem Cache gespeichert. Hochzaehlen, sobald sich eine Definition
#: aendert - dann rechnet der naechste Zugriff neu.
#: 1 -> 2: Waagen-Plausibilitaet (untarierte Waage macht t_first_drops
#:         ungueltig, Mittelfluss <= 0 macht flow_stability ungueltig).
METRICS_VERSION = 2

FIRST_DROPS_WEIGHT_G = 0.3
#: Fenster, in dem eine tarierte Waage noch 0 anzeigen muss.
TARE_CHECK_WINDOW_S = 0.5
PI_END_PRESSURE_FRACTION = 0.6
INFUSION_WINDOW_TAIL_S = 2.0
DIP_WINDOW_S = 4.0
END_WINDOW_S = 2.0

#: Der allererste Wechsel von espresso_state_change liegt bei t < 0.1 s und
#: markiert den Shot-Start, keinen Phasenwechsel.
START_MARKER_MAX_S = 0.5


def compute_metrics(
    series: Sequence[Mapping[str, Any]],
    *,
    dose_g: float | None = None,
    yield_g: float | None = None,
) -> dict[str, Any]:
    """Metriken aus der Zeitreihe eines Shots.

    ``series`` sind Zeilen aus ``shot_series`` (aufsteigend nach ``elapsed``).
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
        warnings.append("Keine Druckwerte - druckbasierte Metriken entfallen.")

    boundaries = phase_boundaries(rows)
    if not boundaries:
        warnings.append("Keine Phasenmarken in state_change - pi_end per Heuristik.")

    pi_end, pi_end_source = _pi_end(times, pressure, boundaries, max_pressure_global)
    if pi_end is None:
        warnings.append("pi_end nicht bestimmbar - Bezugsphase bleibt offen.")

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
        # Ein Variationskoeffizient um einen Mittelwert <= 0 ist nicht
        # interpretierbar (er wird negativ). Ursache ist praktisch immer eine
        # Waage, die waehrend des Bezugs gesprungen ist.
        flow_stability = None
        warnings.append(
            f"Mittlerer Bezugsfluss ist {avg_flow_pour:.2f} ml/s (<= 0) - "
            "flow_stability entfaellt, Waagenwerte pruefen."
        )
    pressure_trend_pour = _slope(
        [times[i] for i in pour], [pressure[i] for i in pour]
    )
    temp_basket_mean = _mean(pour_temp)
    temp_basket_std = _stdev(pour_temp)
    if not pour_temp:
        warnings.append("Keine Korbtemperatur in der Bezugsphase.")

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
            f"Gewicht ueberschreitet nie {FIRST_DROPS_WEIGHT_G} g - keine Waage?"
        )
    elif tare_offset is not None and tare_offset > FIRST_DROPS_WEIGHT_G:
        # In der ersten halben Sekunde kann noch nichts in der Tasse sein - die
        # Maschine praeinfundiert sekundenlang. Zeigt die Waage dort schon
        # Gewicht, war sie nicht tariert, und t_first_drops waere eine Aussage
        # ueber die Tasse statt ueber den Bezug (SPEC ss8: fehlende Grundlage
        # -> null plus Warnung).
        t_first_drops = None
        warnings.append(
            f"Waage zeigt in der ersten Sekunde bereits {tare_offset:.1f} g "
            "(nicht tariert) - t_first_drops nicht bestimmbar."
        )
    if measured and min(measured) < 0:
        warnings.append(
            f"Gewicht wird negativ (min {min(measured):.1f} g) - "
            "Waage vermutlich angestossen; flussbasierte Werte unsicher."
        )

    ratio = round(yield_g / dose_g, 3) if dose_g and yield_g else None
    if ratio is None:
        warnings.append("Dosis oder Bezugsgewicht fehlt - ratio entfaellt.")

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
    """Metriken aus dem Cache, sonst berechnen und ablegen (SPEC ss8).

    ``None``, wenn der Shot nicht existiert. Der Cache verfaellt automatisch,
    sobald ``METRICS_VERSION`` steigt oder der Shot neu geschrieben wurde.
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


#: SPEC ss9.1 - Kanalnamen in der kompakten Kurvenausgabe. Kurz, weil jeder
#: Buchstabe pro Messpunkt einmal im Antwortbudget landet.
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

    Gleichmaessig ueber die **Zeit**, nicht ueber den Index - bei ungleichen
    Abtastabstaenden bliebe sonst ein dicht abgetasteter Abschnitt
    ueberrepraesentiert. Garantiert enthalten sind erster und letzter Punkt
    sowie jeder Zeitpunkt aus ``keep_times`` (die beiden Druckmaxima).

    Rueckgabe sind parallele Arrays (``t``, ``p``, ``fi``, ``fo``, ``w``,
    ``tb``) statt einer Objektliste - das spart rund 60 % Zeichen. Kanaele
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

    if len(ordered) <= budget:
        chosen = range(len(ordered))
    else:
        span = times[-1] - times[0]
        slots = max(0, budget - len(mandatory))
        picked = set(mandatory)
        if slots and span > 0:
            for step in range(slots):
                target = times[0] + span * step / max(1, slots - 1)
                picked.add(min(range(len(times)), key=lambda i: abs(times[i] - target)))
        # Die Rasterpunkte koennen auf Pflichtpunkte fallen; dann bleibt die
        # Auswahl kleiner als das Budget, nie groesser.
        chosen = sorted(picked)[:budget]

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


def phase_boundaries(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    """Phasengrenzen aus ``state_change``.

    ``espresso_state_change`` ist keine Phasennummer, sondern eine Rechteckwelle:
    sie springt bei jedem Phasenwechsel zwischen zwei Sentinelwerten hin und her
    (im Rohformat -10000000 und +10000000; den negativen bildet der Client auf
    ``None`` ab). Nicht der Wert traegt Information, sondern der **Wechsel**.
    An Echtdaten geprueft: die Zahl der Wechsel ist ``Schritte - 1``, und die
    Zeitpunkte decken sich mit den Schrittdauern des Profils.

    Der erste Wechsel liegt bei t < 0.1 s und markiert den Shot-Start; er wird
    verworfen.
    """
    boundaries: list[float] = []
    previous: Any = _UNSET
    for row in rows:
        state = row.get("state_change")
        if previous is not _UNSET and state != previous:
            boundaries.append(float(row["elapsed"]))
        previous = state
    return [t for t in boundaries if t > START_MARKER_MAX_S]


def _pi_end(
    times: Sequence[float],
    pressure: Sequence[float | None],
    boundaries: Sequence[float],
    max_pressure_global: float | None,
) -> tuple[float | None, str | None]:
    """Ende der Praeinfusion (SPEC ss8 1.1).

    Primaer eine echte Phasengrenze aus ``state_change``. Welche das ist, laesst
    sich aus den Marken allein nicht bestimmen - sie sagen *dass* gewechselt
    wurde, nicht *wozu*. Deshalb dient der Druckanstieg als Anker: genommen wird
    die letzte Phasengrenze vor dem Moment, in dem der Druck erstmals
    ``0.6 x max_pressure_global`` erreicht. Der zurueckgegebene Wert ist damit
    immer ein von der Maschine gemeldeter Phasenwechsel, kein Schwellwert.

    Ohne Marken (oder wenn keine vor dem Anker liegt) faellt es auf den
    Ankerzeitpunkt selbst zurueck - die Heuristik der urspruenglichen SPEC.
    """
    if max_pressure_global is None or max_pressure_global <= 0:
        return (boundaries[0], "state_change") if boundaries else (None, None)

    threshold = PI_END_PRESSURE_FRACTION * max_pressure_global
    anchor = next(
        (t for t, p in zip(times, pressure, strict=True) if p is not None and p >= threshold),
        None,
    )
    if anchor is None:
        return (boundaries[0], "state_change") if boundaries else (None, None)

    earlier = [t for t in boundaries if t <= anchor]
    if earlier:
        return max(earlier), "state_change"
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
