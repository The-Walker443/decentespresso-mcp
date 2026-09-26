"""Deriving a per-coffee profile from an existing one (SPEC §11.5).

Pure functions over profile dicts, like the guards: no network, no database.
What may be changed, within which limits, and on which profiles - decided here
before anything is sent.

TWO FACTS ABOUT DECAID THAT SHAPE ALL OF THIS

*A profile's id is a hash of its brewing content* (``ProfileRecord.id``,
"calculated from execution-relevant fields"). The title is not part of it. So a
copy that differs only in name *is* the original as far as Decaid is concerned,
and changing a temperature gives the profile a new id. Both follow from the
same rule and both are handled here rather than discovered later.

*The workflow embeds the profile as an object; it holds no reference.*
Selecting a profile means copying it into the workflow, and which record the
workflow is running can only be answered by comparing content - which is what
``same_brew`` is for.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .decaid_profile import BREWING_KEYS
from .writes import ValidationError

#: Hard limits on what an override may set. They bound our writes, not the
#: profiles themselves: Adaptive v3 ships an 11 bar pressurize step and
#: Blooming Espresso a 98 degree reset step, and cloning those is fine as long
#: as nothing *we* set leaves these ranges.
TEMPERATURE_C = (80.0, 96.0)
TARGET_WEIGHT_G = (10.0, 100.0)
MAX_PRESSURE_BAR = 10.0
MAX_FLOW_MLS = 8.0

#: The overrides whose effect was verified against the machine. Everything else
#: is refused with this reason - a profile field that was never written and
#: read back is a field whose effect nobody here knows.
OVERRIDES = {
    "temperature_c": "brew temperature for every step, in C",
    "target_weight_g": "stop-at-weight, in g",
    "main_setpoint": "the pour step's pressure (bar) or flow (ml/s)",
}

#: Written into a clone's metadata. It is how update_profile tells a profile it
#: made from one somebody tuned by hand on the tablet.
MARKER_KEY = "createdBy"
MARKER = "decentespresso-mcp"

#: Decaid takes titles without a documented limit; the tablet UI truncates long
#: ones. This keeps a title readable in a list rather than enforcing an API rule.
MAX_TITLE_CHARS = 60

#: "<roaster> - <bean>", with an en dash, as a person would write it.
TITLE_SEPARATOR = " – "


def default_title(roaster: str | None, bean: str | None) -> str | None:
    """The naming convention, or None when either half is missing."""
    roaster, bean = (roaster or "").strip(), (bean or "").strip()
    if not roaster or not bean:
        return None
    return f"{roaster}{TITLE_SEPARATOR}{bean}"


def check_title(title: Any, taken: list[str]) -> str:
    """A usable, unused title - or a refusal that suggests one.

    Collisions compare case-insensitively: "D-Flow" and "d-flow" side by side
    on the tablet are two entries nobody can tell apart.
    """
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(["title: a non-empty title is expected"])
    text = " ".join(title.split())
    if len(text) > MAX_TITLE_CHARS:
        raise ValidationError([
            f"title: at most {MAX_TITLE_CHARS} characters, this is {len(text)}"
        ])
    lowered = {t.casefold() for t in taken}
    if text.casefold() in lowered:
        raise ValidationError([
            f"title: {text!r} is already taken - try {suggest(text, lowered)!r}"
        ])
    return text


def suggest(title: str, taken_casefolded: set[str]) -> str:
    for n in range(2, 100):
        candidate = f"{title} ({n})"
        if candidate.casefold() not in taken_casefolded:
            return candidate
    return f"{title} (new)"


def main_step(steps: list[dict[str, Any]]) -> int | None:
    """Index of the step whose setpoint counts as "the" setpoint, if any.

    The rule: the last step, provided it is also the longest. That is the pour
    in every profile where the question has an obvious answer - D-Flow's
    127 s "Pouring" at 1.7 ml/s - and it fails exactly where the question has
    none: Blooming Espresso ends on a 1 s "reset temperature" step at zero
    flow, and picking anything there would be a guess dressed as a setting.
    """
    if not steps:
        return None
    durations = [float(s.get("seconds") or 0) for s in steps]
    last = len(steps) - 1
    if durations[last] < max(durations):
        return None
    return last


def uniform_temperature(steps: list[dict[str, Any]]) -> float | None:
    temps = {s.get("temperature") for s in steps}
    if len(temps) != 1:
        return None
    (only,) = temps
    return float(only) if isinstance(only, int | float) else None


def apply_overrides(
    profile: dict[str, Any], overrides: dict[str, Any], *, title: str | None = None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """A changed copy of ``profile`` and what changed, or every reason it cannot.

    Problems are collected rather than raised one at a time, as in writes.py,
    so a person sees all of them in one answer.
    """
    problems: list[str] = []
    result = copy.deepcopy(profile)
    changes: dict[str, dict[str, Any]] = {}
    steps = result.get("steps") or []

    for key in overrides:
        if key not in OVERRIDES:
            problems.append(
                f"{key}: not changeable here. Only these were verified against "
                f"the machine: {', '.join(OVERRIDES)}"
            )

    if "temperature_c" in overrides:
        value = _number(overrides["temperature_c"], "temperature_c", TEMPERATURE_C,
                        problems)
        if value is not None:
            _shift_temperature(steps, value, changes, problems)

    if "target_weight_g" in overrides:
        value = _number(overrides["target_weight_g"], "target_weight_g",
                        TARGET_WEIGHT_G, problems)
        if value is not None:
            changes["target_weight_g"] = {"before": result.get("target_weight"),
                                          "after": value}
            result["target_weight"] = value

    if "main_setpoint" in overrides:
        index = main_step(steps)
        if index is None:
            names = ", ".join(f"{s.get('name')} {s.get('seconds')}s" for s in steps)
            problems.append(
                "main_setpoint: this profile has no step that is both last and "
                f"longest ({names}), so which setpoint is the main one cannot "
                "be said without guessing."
            )
        else:
            step = steps[index]
            pump = step.get("pump")
            limit = MAX_PRESSURE_BAR if pump == "pressure" else MAX_FLOW_MLS
            unit = "bar" if pump == "pressure" else "ml/s"
            value = _number(overrides["main_setpoint"], "main_setpoint",
                            (0.1, limit), problems, unit=unit)
            field = "pressure" if pump == "pressure" else "flow"
            if value is not None:
                changes["main_setpoint"] = {
                    "step": step.get("name"), "unit": unit,
                    "before": step.get(field), "after": value,
                }
                step[field] = value

    if title is not None:
        changes["title"] = {"before": profile.get("title"), "after": title}
        result["title"] = title

    if problems:
        raise ValidationError(problems)
    return result, changes


def _shift_temperature(
    steps: list[dict[str, Any]], value: float,
    changes: dict[str, dict[str, Any]], problems: list[str],
) -> None:
    """Brew temperature: the main step lands on ``value``, the curve keeps its shape.

    A first version set every step to the one value and refused any profile
    whose steps differed. The live instance showed why that was wrong: the
    visible D-Flow runs Filling 88.5, Infusing 88.0, Pouring 88.0 - D-Flow's
    editor has separate fill and pour temperatures, so the half degree is a
    design decision, not noise. Flattening it loses that; refusing it makes the
    most common profile unusable. Shifting keeps both.

    Every step that results must still lie inside the limits, because every one
    of them is a value this server writes.
    """
    if not steps:
        problems.append("temperature_c: the profile has no steps")
        return
    uniform = uniform_temperature(steps)
    index = main_step(steps)
    if uniform is not None:
        reference = uniform
    elif index is not None and isinstance(steps[index].get("temperature"), int | float):
        reference = float(steps[index]["temperature"])
    else:
        temps = ", ".join(f"{s.get('name')} {s.get('temperature')}" for s in steps)
        problems.append(
            "temperature_c: this profile runs a temperature curve and has no step "
            f"that is both last and longest to anchor it on ({temps}). Which "
            "temperature is 'the' temperature cannot be said; change it on the "
            "tablet."
        )
        return

    delta = round(value - reference, 3)
    low, high = TEMPERATURE_C
    shifted = [round(float(s.get("temperature") or reference) + delta, 3) for s in steps]
    outside = [f"{s.get('name')} {t:g}" for s, t in zip(steps, shifted, strict=True)
               if not low <= t <= high]
    if outside:
        problems.append(
            f"temperature_c: shifting the curve by {delta:+g} C puts "
            f"{', '.join(outside)} outside {low:g} to {high:g} C"
        )
        return
    for step, temp in zip(steps, shifted, strict=True):
        step["temperature"] = temp
    changes["temperature_c"] = {"before": reference, "after": value}
    if uniform is None:
        changes["temperature_c"]["curve_shifted_by"] = delta


def changes_brewing(changes: dict[str, Any]) -> bool:
    """Whether the overrides touch anything Decaid's content id is built from."""
    return any(key != "title" for key in changes)


def same_brew(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Would these two brew identically? Title, author and notes do not count.

    Numbers are normalised first: 92 and 92.0 are the same temperature, and the
    workflow's embedded copy and the stored record do not promise to spell
    them the same way.
    """
    if not a or not b:
        return False
    return _brewing(a) == _brewing(b)


def resolve_profile(records: list[dict[str, Any]], reference: str) -> dict[str, Any]:
    """The one record ``reference`` names - by id, or by exact title.

    Refused when it names none or several. A deleted profile is never chosen:
    it is gone from the tablet, and selecting it into the workflow would put
    something on the machine that the person can no longer see anywhere.
    """
    ref = (reference or "").strip()
    live = [r for r in records if r.get("visibility") != "deleted"]
    if ref.startswith("profile:"):
        match = [r for r in live if r.get("id") == ref]
    else:
        match = [r for r in live
                 if str((r.get("profile") or {}).get("title") or "").casefold()
                 == ref.casefold()]
        # Every edit on the tablet leaves the previous version behind as a
        # hidden record with the same title - the live instance holds four
        # "D-Flow"s, one of them visible. A person naming a profile means the
        # one they can see; the hidden ones count only when there is none.
        visible = [r for r in match if r.get("visibility") == "visible"]
        if visible:
            match = visible
    if len(match) == 1:
        return match[0]
    if not match:
        raise ValidationError([
            f"profile: nothing on the tablet is called {ref!r}. "
            "list_profiles(on_tablet=true) shows what is there."
        ])
    ids = ", ".join(f"{r.get('id')} ({r.get('visibility')})" for r in match)
    raise ValidationError([f"profile: {ref!r} names {len(match)} profiles ({ids}) "
                           "- use the id"])


def running_profile(
    embedded: dict[str, Any] | None, records: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The record whose content the workflow's embedded copy matches, if any.

    None is a real answer: a profile edited on the tablet and never saved is
    running without being any record at all.
    """
    return next((r for r in records if same_brew(r.get("profile"), embedded)), None)


def brew_hash(profile: dict[str, Any]) -> str:
    """A stable fingerprint of what a profile brews - ours, not Decaid's id."""
    import hashlib
    return hashlib.sha256(_brewing(profile).encode("utf-8")).hexdigest()


def is_default(record: dict[str, Any]) -> bool:
    return bool(record.get("isDefault"))


def made_here(record: dict[str, Any]) -> bool:
    return (record.get("metadata") or {}).get(MARKER_KEY) == MARKER


def _brewing(profile: dict[str, Any]) -> str:
    return json.dumps(_normalise({k: profile.get(k) for k in BREWING_KEYS}),
                      sort_keys=True, separators=(",", ":"))


def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return round(float(value), 6)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _number(
    raw: Any, name: str, bounds: tuple[float, float], problems: list[str],
    *, unit: str = "",
) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        problems.append(f"{name}: a number is expected")
        return None
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        problems.append(f"{name}: a number is expected")
        return None
    low, high = bounds
    if not low <= value <= high:
        suffix = f" {unit}" if unit else ""
        problems.append(f"{name}: {value:g}{suffix} is outside {low:g} to {high:g}{suffix}")
        return None
    return value
