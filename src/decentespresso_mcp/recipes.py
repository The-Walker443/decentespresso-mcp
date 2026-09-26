"""Recipes: ours in SQLite, DYE2's read from its plugin store (SPEC §11.6).

Pure functions, like the guards and the profile forge.

THE ONE RULE THAT SHAPES THIS. DYE2's KV contract (docs/KV_CONTRACT.md in
decentespresso/dye2) makes DYE2 the single writer of its keys; every other
consumer reads. The store checks no ownership and offers no ETag and no
field-level write, so a second writer does not fail - it silently clobbers a
concurrent DYE2 edit. Nothing here writes those keys, and there is no code path
that could.

Streamline, the contract's own reference consumer, does not keep to it: its
recipe auto-save reads the array, patches one item and writes the whole array
back (dyeStrip.js, saveItemFields). That is noted upstream, not copied.
"""

from __future__ import annotations

import json
from typing import Any

from .profile_forge import TITLE_SEPARATOR, default_title
from .writes import ValidationError

DYE2_NAMESPACE = "dye2.reaplugin"
DYE2_RECIPES = "recipes"

SOURCES = ("all", "mine", "dye2")

#: Shown instead of applying a DYE2 recipe that predates its `workflow` field.
#: The contract allows deriving one from the legacy fields "or skip apply";
#: deriving means reimplementing DYE2's own mapping from dashboardVariables,
#: which it may change, so this skips and says why.
LEGACY_NOTE = ("written by an older DYE2 without a ready-made workflow - apply it "
               "on the tablet, or open and save it once in DYE2")


def default_name(context: dict[str, Any]) -> str | None:
    """"<roaster> - <bean>" from the workflow, the same convention as profiles."""
    return default_title(context.get("coffeeRoaster"), context.get("coffeeName"))


def own_view(row: dict[str, Any]) -> dict[str, Any]:
    """Our recipe, shaped after the contract's items so both sources read alike."""
    return _compact({
        "source": "mine",
        "name": row["name"],
        "beanName": row.get("bean_name"),
        "beanRoaster": row.get("bean_roaster"),
        "beanBatchId": row.get("bean_batch_id"),
        "profileTitle": row.get("profile_title"),
        "pinProfile": bool(row.get("pin_profile")),
        "dashboardVariables": _compact({
            "dose": row.get("dose_g"),
            "drink": row.get("yield_g"),
            "ratio": _ratio(row.get("dose_g"), row.get("yield_g")),
            "grind": row.get("grind"),
            "grinderModel": row.get("grinder_model"),
        }),
        "capturedAt": row.get("captured_at"),
        "updatedAt": row.get("updated_at"),
    })


def dye2_view(item: dict[str, Any]) -> dict[str, Any]:
    """A DYE2 recipe as the contract describes it, with its fallbacks honoured.

    `title` is optional and falls back to `name`, then to "Recipe <id>".
    `workflow` is optional too: without it the recipe is listed but not applied.
    """
    variables = item.get("dashboardVariables") or {}
    view = _compact({
        "source": "dye2",
        "id": item.get("id"),
        "name": dye2_name(item),
        "beanName": item.get("beanName"),
        "profileTitle": item.get("profileTitle"),
        "dashboardVariables": _compact({
            "dose": variables.get("dose"),
            "drink": variables.get("drink"),
            "ratio": _ratio(variables.get("dose"), variables.get("drink")),
            "grind": variables.get("grind"),
            "grinderModel": variables.get("grinderModel"),
        }),
        "capturedAt": item.get("capturedAt"),
        "applicable": isinstance(item.get("workflow"), dict),
    })
    if not view["applicable"]:
        view["why_not"] = LEGACY_NOTE
    return view


def profile_reference_only(workflow: dict[str, Any] | None) -> str | None:
    """What a DYE2 `workflow.profile` that carries no profile amounts to.

    Measured (T44): seven of eight favourites on the live instance store
    `profile: {id: null, title: "D-Flow"}` - a title, no steps, mostly no id -
    and DYE2's one recipe stores no profile at all. Decaid merges a PUT into the
    workflow (T13), so such a stub only renames whatever profile is running.
    The item is still applied as stored; this says what that did.
    """
    profile = (workflow or {}).get("profile")
    if not isinstance(profile, dict) or profile.get("steps"):
        return None
    title = profile.get("title")
    return (f"DYE2 stores this profile only as the name {title!r}, without its "
            "steps. The machine keeps brewing on the profile it was already "
            "running; to change that, set_workflow with a profileId.")


def dye2_name(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or f"Recipe {item.get('id')}")


def choose(
    own: list[dict[str, Any]], dye2: list[dict[str, Any]], name: str,
    source: str | None,
) -> tuple[str, dict[str, Any]]:
    """The one recipe ``name`` means, and which source it came from.

    A name both sources use is not guessed between: DYE2's and ours can differ
    in everything, and applying the wrong one quietly changes the machine.
    """
    wanted = " ".join((name or "").split()).casefold()
    mine = [r for r in own if " ".join(r["name"].split()).casefold() == wanted]
    theirs = [r for r in dye2 if " ".join(dye2_name(r).split()).casefold() == wanted]
    if source == "mine":
        theirs = []
    elif source == "dye2":
        mine = []
    if mine and theirs:
        raise ValidationError([
            f"recipe: {name!r} exists as one of mine and as a DYE2 recipe - say "
            "which with source='mine' or source='dye2'"])
    if mine:
        return "mine", mine[0]
    if len(theirs) == 1:
        return "dye2", theirs[0]
    if len(theirs) > 1:
        ids = ", ".join(str(r.get("id")) for r in theirs)
        raise ValidationError([f"recipe: DYE2 holds {len(theirs)} recipes named "
                               f"{name!r} (ids {ids}) - rename one in DYE2"])
    raise ValidationError([f"recipe: nothing called {name!r}"
                           + (f" in {source}" if source else "")
                           + " - list_recipes shows what there is"])


def row_from_workflow(
    name: str, context: dict[str, Any], record: dict[str, Any], *,
    bean_id: str | None, pin: bool,
) -> dict[str, Any]:
    """A recipe row from what the machine is set to right now."""
    profile = record.get("profile") or {}
    return {
        "name": name,
        "bean_id": bean_id,
        "bean_name": context.get("coffeeName"),
        "bean_roaster": context.get("coffeeRoaster"),
        "bean_batch_id": context.get("beanBatchId"),
        "profile_title": profile.get("title"),
        "profile_snapshot": json.dumps(profile, ensure_ascii=False,
                                       separators=(",", ":")),
        "pin_profile": pin,
        "dose_g": context.get("targetDoseWeight"),
        "yield_g": context.get("targetYield"),
        "grind": context.get("grinderSetting"),
        "grinder_model": context.get("grinderModel"),
    }


def context_patch(row: dict[str, Any]) -> dict[str, Any]:
    """What applying our recipe writes into the workflow context.

    Only what the recipe holds. A field it never captured is left as it is on
    the machine rather than cleared - a recipe saved without a grinder model
    is not a statement that there is none.
    """
    patch = {
        "beanBatchId": row.get("bean_batch_id"),
        "targetDoseWeight": row.get("dose_g"),
        "targetYield": row.get("yield_g"),
        "grinderSetting": row.get("grind"),
        "grinderModel": row.get("grinder_model"),
    }
    return {k: v for k, v in patch.items() if v is not None}


def suggest_name(name: str, taken: list[str]) -> str:
    lowered = {t.casefold() for t in taken}
    for n in range(2, 100):
        candidate = f"{name} ({n})"
        if candidate.casefold() not in lowered:
            return candidate
    return f"{name} (new)"


def _ratio(dose: Any, drink: Any) -> float | None:
    try:
        return round(float(drink) / float(dose), 2) if dose and drink else None
    except (TypeError, ValueError):
        return None


def _compact(block: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if v not in (None, {}, [])}


__all__ = ["DYE2_NAMESPACE", "DYE2_RECIPES", "SOURCES", "TITLE_SEPARATOR",
           "choose", "context_patch", "default_name", "dye2_name", "dye2_view",
           "own_view", "row_from_workflow", "suggest_name"]
