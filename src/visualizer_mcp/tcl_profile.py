"""Parser fuer DE1-Profile im Tcl-Format (SPEC ss7.1).

Kein Regex-Gefrickel: die Datei *ist* eine Tcl-Liste, also parst sie ein
Tcl-Interpreter aus der stdlib. ``Tcl()`` braucht kein Display und im
python:3.12-slim-Image kein zusaetzliches ``tk``-Paket.

Die Versionierung haengt am sha256 des **normalisierten** Roh-TCL (SPEC ss5);
``raw_tcl`` wird unveraendert gespeichert.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from tkinter import Tcl, TclError
from typing import Any

log = logging.getLogger(__name__)

#: settings_profile_type -> Profiltyp. Gegen die JSON-Ausgabe von Visualizer
#: verifiziert: settings_2a -> "pressure", settings_2c -> "advanced".
#: settings_2b -> "flow" folgt der DE1-Konvention, liegt aber (noch) nicht als
#: Echtdatenbeleg vor.
PROFILE_TYPE_BY_SETTINGS = {
    "settings_2a": "pressure",
    "settings_2b": "flow",
    "settings_2c": "advanced",
}

#: exit_type -> Feld, in dem der Schwellwert steht.
EXIT_VALUE_FIELD = {
    "pressure_over": "exit_pressure_over",
    "pressure_under": "exit_pressure_under",
    "flow_over": "exit_flow_over",
    "flow_under": "exit_flow_under",
}

# Tcl-Interpreter sind nicht thread-safe; ein Lock ist billiger als eine
# Interpreter-Instanz pro Aufruf zu verbieten.
_TCL_LOCK = threading.Lock()


def normalize_tcl(raw: str) -> str:
    """Vereinheitlicht Zeilenenden und Randweissraum (SPEC ss6.4).

    Zweck ist ein stabiler Hash: dieselbe Profilversion soll denselben Hash
    liefern, egal ob sie mit CRLF oder LF durch die Leitung kam.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def version_hash(raw: str) -> str:
    """sha256 des normalisierten TCL, hex."""
    return hashlib.sha256(normalize_tcl(raw).encode("utf-8")).hexdigest()


def parse_profile(raw: str) -> dict[str, Any]:
    """TCL -> ``parsed_json`` (SPEC ss7.1).

    Wirft nie: bei kaputtem TCL kommt ``parse_ok=False`` zurueck, dazu Titel und
    Notizen aus einem toleranten Zeilenscan, damit der Shot trotzdem nutzbar
    bleibt.
    """
    try:
        fields = _top_level_fields(raw)
    except (TclError, ValueError) as exc:
        log.warning("tcl parse failed", extra={"fields": {"error": type(exc).__name__}})
        return _fallback(raw, f"{type(exc).__name__}: {exc}")

    settings_type = fields.get("settings_profile_type", "")
    profile_type = PROFILE_TYPE_BY_SETTINGS.get(settings_type, settings_type or None)

    try:
        steps = _parse_steps(fields.get("advanced_shot", ""))
    except (TclError, ValueError) as exc:
        log.warning("advanced_shot unparsable", extra={"fields": {"error": type(exc).__name__}})
        return _fallback(raw, f"advanced_shot: {type(exc).__name__}: {exc}")

    # Advanced-Profile fuehren ihr Zielgewicht im _advanced-Feld, Legacy-Profile
    # im Basisfeld - so macht es auch Visualizers eigener Serializer.
    weight_key = (
        "final_desired_shot_weight_advanced"
        if profile_type == "advanced"
        else "final_desired_shot_weight"
    )

    return {
        "title": _text(fields.get("profile_title")) or _text(fields.get("original_profile_title")),
        "author": _text(fields.get("author")),
        "type": profile_type,
        "beverage_type": _text(fields.get("beverage_type")),
        "notes": _text(fields.get("profile_notes")),
        "target_weight_g": _number(fields.get(weight_key)),
        "target_temp_c": _number(fields.get("espresso_temperature")),
        "settings_profile_type": settings_type or None,
        "steps": steps,
        "parse_ok": True,
    }


# ------------------------------------------------------------------ intern


def _top_level_fields(raw: str) -> dict[str, str]:
    with _TCL_LOCK:
        interpreter = Tcl()
        items = list(interpreter.splitlist(raw))
    if len(items) % 2:
        raise ValueError(f"ungerade Anzahl Top-Level-Elemente ({len(items)})")
    return dict(zip(items[::2], items[1::2], strict=True))


def _parse_steps(advanced_shot: str) -> list[dict[str, Any]]:
    """``advanced_shot {{...} {...}}`` -> Liste von Schritten.

    Legacy-Profile (settings_2a/2b) haben hier ``{}``; sie bekommen eine leere
    Liste. Visualizer synthetisiert fuer solche Profile Schritte aus den
    flow_profile_*-Settings - das bilden wir bewusst nicht nach, weil im TCL
    keine Schritte stehen (siehe Testkommentar in test_tcl_profile.py).
    """
    if not advanced_shot.strip():
        return []

    with _TCL_LOCK:
        interpreter = Tcl()
        raw_steps = list(interpreter.splitlist(advanced_shot))
        parsed = [list(interpreter.splitlist(step)) for step in raw_steps]

    steps: list[dict[str, Any]] = []
    for index, items in enumerate(parsed):
        if len(items) % 2:
            raise ValueError(f"Schritt {index}: ungerade Anzahl Elemente")
        step = dict(zip(items[::2], items[1::2], strict=True))
        mode = _text(step.get("pump"))
        target = step.get("pressure") if mode == "pressure" else step.get("flow")
        steps.append({
            "name": _text(step.get("name")),
            "mode": mode,
            "target": _number(target),
            "temp_c": _number(step.get("temperature")),
            "duration_s": _number(step.get("seconds")),
            "transition": _text(step.get("transition")),
            "exit": _parse_exit(step),
        })
    return steps


def _parse_exit(step: dict[str, str]) -> dict[str, Any] | None:
    """``exit_if 0`` heisst: die exit_*-Felder stehen zwar da, gelten aber nicht.

    Visualizer laesst den exit-Block in seiner JSON-Ausgabe dann ebenfalls weg.
    """
    if _number(step.get("exit_if")) != 1:
        return None
    exit_type = _text(step.get("exit_type"))
    if not exit_type:
        return None
    value_field = EXIT_VALUE_FIELD.get(exit_type)
    return {
        "type": exit_type,
        "value": _number(step.get(value_field)) if value_field else None,
    }


def _fallback(raw: str, error: str) -> dict[str, Any]:
    """Toleranter Zeilenscan, wenn der Interpreter aussteigt (SPEC ss7.1)."""
    title = None
    notes = None
    for line in normalize_tcl(raw).split("\n"):
        key, _, value = line.partition(" ")
        text = value.strip().strip("{}").strip()
        if key == "profile_title" and text and not title:
            title = text
        elif key == "original_profile_title" and text and not title:
            title = text
        elif key == "profile_notes" and text and not notes:
            notes = text
    return {
        "title": title,
        "author": None,
        "type": None,
        "beverage_type": None,
        "notes": notes,
        "target_weight_g": None,
        "target_temp_c": None,
        "settings_profile_type": None,
        "steps": [],
        "parse_ok": False,
        "parse_error": error,
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number
