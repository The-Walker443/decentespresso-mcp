"""Recipes: a bean's most recent real shot - what Beanie restores with a tap (SPEC §11.6).

Pure functions, like the guards and the profile forge.

There is nothing stored. A recipe is derived from the archive the moment it is
asked for, by the rule Beanie's bean picker uses (github.com/giladger/Beanie,
src/controllers/beanWorkflowController.ts and src/domain/beanWorkflow.ts at
a54a625), so that the chat and a tap on the tablet set the machine the same
way:

- the shot: the bean's newest shot that is not a service shot (steam, water,
  flush, cleaning, calibration by beverage type);
- the batch: the bean's newest usable batch by roast date - not the shot's -
  where usable means not under 5 g remaining;
- dose and yield as planned (the shot's workflow), the measured value only
  where the plan has none;
- the profile: the stored profile of the shot's title where there is one,
  otherwise the one the shot embeds - at the shot's brew temperature either
  way; ``targetYield`` 0 when that profile turns the weight stop off.

A change made in the chat but not pulled yet lives in the workflow until the
next shot.
"""

from __future__ import annotations

import copy
from typing import Any

from .profile_forge import TITLE_SEPARATOR, default_title

#: Beanie's SERVICE_BEVERAGE_TYPES (src/domain/shotRecord.ts).
SERVICE_TYPES = frozenset({
    "steam", "water", "hot_water", "hotwater", "hot water", "flush", "rinse",
    "clean", "cleaning", "calibrate", "calibration",
})

#: Beanie's isUsableBatch: a batch with less than this left is used up.
USABLE_REMAINING_G = 5.0


def recipe_name(bean: dict[str, Any]) -> str:
    """"<roaster> – <bean>", the same convention as the per-coffee profiles."""
    return (default_title(bean.get("roaster"), bean.get("name"))
            or str(bean.get("name") or bean.get("id")))


def is_service(workflow: dict[str, Any] | None) -> bool:
    """A steam, water, flush, cleaning or calibration shot - never a recipe."""
    workflow = workflow or {}
    kinds = ((workflow.get("context") or {}).get("finalBeverageType"),
             (workflow.get("profile") or {}).get("beverage_type"))
    return any(k is not None and str(k).lower().strip() in SERVICE_TYPES for k in kinds)


def recipe_shot(shots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest shot that is not a service shot. ``shots`` newest first."""
    return next((s for s in shots if not is_service(s.get("workflow"))), None)


def recipe_batch(batches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Beanie's latestBatch(usable) ?? latestBatch(all), by roast date."""
    def newest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        dated = sorted(items, key=lambda b: str(b.get("roastDate") or ""), reverse=True)
        return dated[0] if dated else None

    def usable(batch: dict[str, Any]) -> bool:
        left = batch.get("weightRemaining")
        return not (isinstance(left, int | float) and left < USABLE_REMAINING_G)

    return newest([b for b in batches if usable(b)]) or newest(batches)


def recipe_workflow(
    bean: dict[str, Any], batch: dict[str, Any] | None, shot: dict[str, Any],
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ``{context, profile}`` Beanie's buildWorkflowUpdate would PUT."""
    workflow = shot.get("workflow") or {}
    context = workflow.get("context") or {}
    annotations = shot.get("annotations") or {}
    embedded = workflow.get("profile") or {}
    temperature = base_temperature(embedded)
    stored = next((r["profile"] for r in profiles
                   if (r.get("profile") or {}).get("title") == embedded.get("title")
                   and r.get("visibility", "visible") == "visible"), None)
    profile = copy.deepcopy(stored or embedded)
    if profile and temperature is not None:
        profile = with_temperature(profile, temperature)
    target_weight = profile.get("target_weight")
    weight_stop_off = isinstance(target_weight, int | float) and target_weight <= 0
    grind = context.get("grinderSetting")
    body: dict[str, Any] = {"context": {
        "beanId": bean.get("id"),
        "coffeeName": bean.get("name"),
        "coffeeRoaster": bean.get("roaster"),
        "beanBatchId": (batch or {}).get("id"),
        "targetDoseWeight": _first_positive(context.get("targetDoseWeight"),
                                            annotations.get("actualDoseWeight")),
        "targetYield": 0 if weight_stop_off else _first_positive(
            context.get("targetYield"), annotations.get("actualYield")),
        "grinderId": context.get("grinderId"),
        "grinderModel": context.get("grinderModel"),
        "grinderSetting": str(grind) if grind is not None else None,
        "finalBeverageType": "espresso",
    }}
    if profile:
        body["profile"] = profile
    return body


def base_temperature(profile: dict[str, Any] | None) -> float | None:
    """Beanie's profileBaseTemperature: the tank target, else the hottest step."""
    if not profile:
        return None
    tank = profile.get("tank_temperature")
    if isinstance(tank, int | float) and tank > 0:
        return float(tank)
    temps = [s["temperature"] for s in profile.get("steps") or []
             if isinstance(s, dict) and isinstance(s.get("temperature"), int | float)]
    return float(max(temps)) if temps else None


def with_temperature(profile: dict[str, Any], target: float) -> dict[str, Any]:
    """Beanie's withProfileTemperature: shift every step, and a real tank target."""
    current = base_temperature(profile)
    if current is None:
        return {**profile, "tank_temperature": target}
    delta = target - current
    if delta == 0:
        return profile
    steps = [({**s, "temperature": s["temperature"] + delta}
              if isinstance(s, dict) and isinstance(s.get("temperature"), int | float) else s)
             for s in profile.get("steps") or []]
    tank = profile.get("tank_temperature")
    tank = (tank + delta if tank > 0 else tank) if isinstance(tank, int | float) else target
    return {**profile, "tank_temperature": tank, "steps": steps}


def _positive(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _first_positive(*values: Any) -> float | None:
    return next((p for p in map(_positive, values) if p is not None), None)


__all__ = ["SERVICE_TYPES", "TITLE_SEPARATOR", "base_temperature", "is_service",
           "recipe_batch", "recipe_name", "recipe_shot", "recipe_workflow",
           "with_temperature"]
