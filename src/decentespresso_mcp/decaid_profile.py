"""Profilversionen aus dem Workflow-JSON (SPEC ss20.5).

Decaid liefert das Bruehprofil als JSON mit dem Bezug mit - es muss nicht mehr
getrennt geholt und aus TCL geparst werden. Damit entfaellt die haeufigste
Fehlerquelle der Visualizer-Aera: ein Profil, das die API nicht herausgibt
(422), oder eines, dessen TCL der Parser nicht versteht.

``tcl_profile.py`` bleibt fuer den Altbestand lesbar, wird aber nicht mehr
benutzt.

Zwei Hashes wie bisher (SPEC ss5):

``version_hash``  Identitaet einer Profilversion. Aendert sich, sobald sich
                  irgendetwas am Profil aendert - auch Notizen.
``semantic_hash`` Gruppiert Versionen, die gleich bruehen. Nur die Schritte und
                  die Ziele gehen ein; Titel, Autor und Notizen nicht. Zwei
                  Versionen, die sich allein in einer Notiz unterscheiden,
                  landen damit in derselben Gruppe.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Schluessel, die das Bruehverhalten bestimmen. Alles andere ist Beschriftung.
BREWING_KEYS = (
    "steps",
    "target_weight",
    "target_volume",
    "target_volume_count_start",
    "tank_temperature",
    "beverage_type",
)


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def version_hash(profile: dict[str, Any]) -> str:
    return _digest(profile)


def semantic_hash(profile: dict[str, Any]) -> str:
    return _digest({k: profile.get(k) for k in BREWING_KEYS})


def profile_version(profile: dict[str, Any]) -> dict[str, Any]:
    """``profile``-Objekt eines Workflows -> Angaben fuer ``db.upsert_profile``."""
    steps = profile.get("steps") or []
    #: Feldnamen wie bisher, damit die Tools unveraendert bleiben. Decaids
    #: Profil-JSON traegt dieselben Angaben, nur anders benannt - und anders
    #: als beim TCL-Parser kann es hier nicht scheitern, deshalb ist
    #: ``parse_ok`` immer wahr.
    parsed = {
        "title": profile.get("title"),
        "type": _kind(steps),
        "author": profile.get("author"),
        "beverage_type": profile.get("beverage_type"),
        "target_weight_g": profile.get("target_weight"),
        "target_volume_ml": profile.get("target_volume"),
        "target_temp_c": _headline_temperature(steps),
        "parse_ok": True,
        "steps": [
            {
                "name": step.get("name"),
                "mode": step.get("pump"),
                "target": step.get("pressure") if step.get("pump") == "pressure"
                          else step.get("flow"),
                "temp_c": step.get("temperature"),
                "duration_s": step.get("seconds"),
                "exit": _exit(step.get("exit")),
                "transition": step.get("transition"),
                "limiter": step.get("limiter"),
            }
            for step in steps
        ],
    }
    return {
        "name": str(profile.get("title") or "ohne Titel"),
        "version_hash": version_hash(profile),
        "semantic_hash": semantic_hash(profile),
        "raw_json": json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
        "parsed_json": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
        "profile_notes": (profile.get("notes") or None),
    }


def _kind(steps: list[dict[str, Any]]) -> str:
    """Grobe Einordnung wie bisher: mehr als ein Schritt heisst "advanced"."""
    modes = {step.get("pump") for step in steps}
    if len(steps) > 1 or len(modes) > 1:
        return "advanced"
    return "pressure" if "pressure" in modes else "flow"


def _headline_temperature(steps: list[dict[str, Any]]) -> float | None:
    """Die Temperatur des ersten Schritts - was die Maschine anzeigt."""
    for step in steps:
        temperature = step.get("temperature")
        if temperature is not None:
            return float(temperature)
    return None


def _exit(exit_condition: Any) -> dict[str, Any] | None:
    """``{type, condition, value}`` -> ``{type: "<was>_<wann>", value: <wert>}``.

    Dieselbe Form, die der TCL-Parser geliefert hat, damit die Tool-Antworten
    gleich bleiben. Ohne Bedingung ``None`` statt eines leeren Objekts.
    """
    if not isinstance(exit_condition, dict) or not exit_condition.get("type"):
        return None
    kind = exit_condition.get("type")
    when = exit_condition.get("condition")
    return {
        "type": f"{kind}_{when}" if when else str(kind),
        "value": exit_condition.get("value"),
    }
