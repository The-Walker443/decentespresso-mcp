"""Profile versions from the workflow JSON (SPEC §20.5).

Decaid ships the brewing profile as JSON alongside the shot - it no longer
has to be fetched separately and parsed from TCL. That removes the most
common source of failure in the Visualizer era: a profile the API would not
hand out (422), or one whose TCL the parser did not understand.

``tcl_profile.py`` stays readable for the archived era but is no longer used.

Two hashes, as before (SPEC §5):

``version_hash``  Identity of one profile version. Changes as soon as
                  anything about the profile changes - notes included.
``semantic_hash`` Groups versions that brew alike. Only the steps and the
                  targets go in; title, author and notes do not. Two versions
                  differing only in a note therefore land in the same group.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Keys that determine brewing behaviour. Everything else is labelling.
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
    """A workflow's ``profile`` object -> arguments for ``db.upsert_profile``."""
    steps = profile.get("steps") or []
    #: Field names as before, so the tools stay unchanged. Decaid's profile
    #: JSON carries the same information under different names - and unlike
    #: the TCL parser this cannot fail, which is why ``parse_ok`` is always
    #: true.
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
        "name": str(profile.get("title") or "untitled"),
        "version_hash": version_hash(profile),
        "semantic_hash": semantic_hash(profile),
        "raw_json": json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
        "parsed_json": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
        "profile_notes": (profile.get("notes") or None),
    }


def _kind(steps: list[dict[str, Any]]) -> str:
    """Rough classification as before: more than one step means "advanced"."""
    modes = {step.get("pump") for step in steps}
    if len(steps) > 1 or len(modes) > 1:
        return "advanced"
    return "pressure" if "pressure" in modes else "flow"


def _headline_temperature(steps: list[dict[str, Any]]) -> float | None:
    """The temperature of the first step - what the machine displays."""
    for step in steps:
        temperature = step.get("temperature")
        if temperature is not None:
            return float(temperature)
    return None


def _exit(exit_condition: Any) -> dict[str, Any] | None:
    """``{type, condition, value}`` -> ``{type: "<what>_<when>", value: <value>}``.

    The same shape the TCL parser produced, so tool responses stay identical.
    Without a condition it is ``None`` rather than an empty object.
    """
    if not isinstance(exit_condition, dict) or not exit_condition.get("type"):
        return None
    kind = exit_condition.get("type")
    when = exit_condition.get("condition")
    return {
        "type": f"{kind}_{when}" if when else str(kind),
        "value": exit_condition.get("value"),
    }
