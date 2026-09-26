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
from dataclasses import dataclass
from typing import Any

from .profile_forge import TITLE_SEPARATOR, default_title
from .writes import ValidationError

DYE2_NAMESPACE = "dye2.reaplugin"
DYE2_RECIPES = "recipes"
DYE2_FAVOURITES = "autoFavourites"

SOURCES = ("all", "mine", "dye2", "dye2_favs")

#: copyMask group -> the context fields it governs. Taken one to one from
#: DYE2's buildFavouriteWorkflow (dye2-plugin/src/utils/dev-api.ts), so that
#: respecting the mask means what it means in DYE2. A group is on unless it is
#: explicitly false ("absent => on", KV_CONTRACT.md).
MASK_FIELDS: dict[str, tuple[str, ...]] = {
    "dose": ("targetDoseWeight",),
    "drink": ("targetYield",),
    "grindSetting": ("grinderSetting", "extras.rpm"),
    "grinder": ("grinderId", "grinderModel"),
    "basket": ("extras.basketId", "extras.basketName"),
    "beans": ("beanBatchId", "coffeeName", "coffeeRoaster"),
    "roastDate": ("roastDate",),
    "barista": ("baristaName",),
    "drinker": ("drinkerName",),
    "note": ("extras.note",),
}

#: Snapshot field -> context field, for a favourite written before DYE2 stored
#: a ready-made `workflow` (the contract's legacy path). Same source as above.
_SNAPSHOT_FIELDS = {
    "dose": "targetDoseWeight", "drink": "targetYield", "grindSetting": "grinderSetting",
    "grinderId": "grinderId", "grinderModel": "grinderModel",
    "beanBatchId": "beanBatchId", "coffeeName": "coffeeName",
    "coffeeRoaster": "coffeeRoaster", "roastDate": "roastDate",
    "barista": "baristaName", "drinker": "drinkerName",
}
_SNAPSHOT_GROUP = {
    "dose": "dose", "drink": "drink", "grindSetting": "grindSetting",
    "grinderId": "grinder", "grinderModel": "grinder", "beanBatchId": "beans",
    "coffeeName": "beans", "coffeeRoaster": "beans", "roastDate": "roastDate",
    "barista": "barista", "drinker": "drinker",
}

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


# ------------------------------------------------------------- Favourites


def favourite_name(fav: dict[str, Any]) -> str:
    """The contract's fallbacks: title, then subtitle, then a derived label.

    Five of the eight live favourites have an empty title and no subtitle, so
    the label is derived as DYE2 does ("roaster · coffee"), and they collide -
    which is why a favourite can also be named by its id.
    """
    if fav.get("title"):
        return str(fav["title"])
    if fav.get("subtitle"):
        return str(fav["subtitle"])
    snap = fav.get("snapshot") or {}
    context = (fav.get("workflow") or {}).get("context") or {}
    roaster = snap.get("coffeeRoaster") or context.get("coffeeRoaster")
    coffee = snap.get("coffeeName") or context.get("coffeeName")
    label = " · ".join(p for p in (roaster, coffee) if p)
    return label or str(fav.get("beverage") or f"Favourite {fav.get('id')}")


def profile_kind(profile: Any) -> str:
    """'full' carries steps; 'reference' is an id without them (T44); 'name'
    is a title and nothing else; 'none' is absent."""
    if not isinstance(profile, dict):
        return "none"
    if profile.get("steps"):
        return "full"
    return "reference" if profile.get("id") else "name"


def favourite_workflow(fav: dict[str, Any]) -> tuple[dict[str, Any], Any, list[str]]:
    """``(context, profile, groups masked off)`` - what applying the favourite sends.

    DYE2 already applies the copyMask when it builds `workflow`; applying it
    here again is a no-op for such items and keeps an item whose mask was
    changed afterwards honest. A favourite without `workflow` is derived from
    snapshot and mask the way DYE2's builder does - the contract's legacy path.
    """
    mask = fav.get("copyMask") or {}
    off = sorted(k for k, v in mask.items() if v is False)
    workflow = fav.get("workflow")
    if isinstance(workflow, dict):
        context = json.loads(json.dumps(workflow.get("context") or {}))
        profile = workflow.get("profile")
    else:
        snap = fav.get("snapshot") or {}
        context = {field: snap[key] for key, field in _SNAPSHOT_FIELDS.items()
                   if snap.get(key) is not None and _SNAPSHOT_GROUP[key] not in off}
        if "grinderSetting" in context:
            context["grinderSetting"] = str(context["grinderSetting"])
        profile = ({"id": snap.get("profileId"), "title": snap.get("profileTitle")}
                   if snap.get("profileId") or snap.get("profileTitle") else None)
    for group in off:
        for field in MASK_FIELDS.get(group, ()):
            if field.startswith("extras."):
                (context.get("extras") or {}).pop(field.split(".", 1)[1], None)
            else:
                context.pop(field, None)
    if context.get("extras") == {}:
        context.pop("extras")
    if "profile" in off:
        profile = None
    return context, profile, off


def favourite_view(fav: dict[str, Any]) -> dict[str, Any]:
    context, profile, off = favourite_workflow(fav)
    view = _compact({
        "source": "dye2_favs",
        "id": fav.get("id"),
        "name": favourite_name(fav),
        "auto": bool(fav.get("auto")) or None,
        "beanName": context.get("coffeeName"),
        "beanRoaster": context.get("coffeeRoaster"),
        "profileTitle": (profile or {}).get("title") if isinstance(profile, dict) else None,
        "profile": profile_kind(profile),
        "dashboardVariables": _compact({
            "dose": context.get("targetDoseWeight"),
            "drink": context.get("targetYield"),
            "ratio": _ratio(context.get("targetDoseWeight"), context.get("targetYield")),
            "grind": context.get("grinderSetting"),
            "grinderModel": context.get("grinderModel"),
        }),
        "maskedOff": off or None,
        "capturedAt": fav.get("capturedAt"),
    })
    return view


def choose(
    own: list[dict[str, Any]], dye2: list[dict[str, Any]], name: str,
    source: str | None, favs: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The one recipe or favourite ``name`` means, and which source it came from.

    A name more than one source uses is not guessed between: they can differ in
    everything, and applying the wrong one quietly changes the machine. DYE2's
    items can also be named by id - five live favourites share one label.
    """
    wanted = " ".join((name or "").split()).casefold()

    def hits(items: list[dict[str, Any]], label: Any) -> list[dict[str, Any]]:
        return [i for i in items
                if " ".join(label(i).split()).casefold() == wanted
                or str(i.get("id") or "") == (name or "").strip()]

    found = {
        "mine": [r for r in own if " ".join(r["name"].split()).casefold() == wanted],
        "dye2": hits(dye2, dye2_name),
        "dye2_favs": hits(favs or [], favourite_name),
    }
    if source in found:
        found = {source: found[source]}
    present = [k for k, v in found.items() if v]
    if len(present) > 1:
        options = " or ".join(f"source='{k}'" for k in present)
        raise ValidationError([f"recipe: {name!r} exists in {', '.join(present)} - "
                               f"say which with {options}"])
    if not present:
        raise ValidationError([f"recipe: nothing called {name!r}"
                               + (f" in {source}" if source else "")
                               + " - list_recipes shows what there is"])
    kind = present[0]
    items = found[kind]
    if len(items) > 1:
        ids = ", ".join(str(i.get("id")) for i in items)
        raise ValidationError([f"recipe: {len(items)} entries in {kind} are called "
                               f"{name!r} (ids {ids}) - name one by its id"])
    return kind, items[0]


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


__all__ = ["ISSUES", "Catalogue", "issues_of", "mark_duplicates", "payload_of",
           "DYE2_NAMESPACE", "DYE2_RECIPES", "SOURCES", "TITLE_SEPARATOR",
           "choose", "context_patch", "default_name", "dye2_name", "dye2_view",
           "own_view", "row_from_workflow", "suggest_name"]


# ------------------------------------------------------------------ Issues

#: Problem codes on a listed recipe or favourite, found before anything is
#: applied. Their meaning is spelled out once, in INSTRUCTIONS.
ISSUES = (
    "name_only_profile",            # profile is a title without steps or id (T44)
    "profile_reference_unresolved", # profile is an id that is not on the tablet
    "profile_missing",              # mine, unpinned: its profile title is gone
    "no_workflow",                  # DYE2 item from before the ready-made workflow
    "batch_is_bean_id",             # beanBatchId holds a bean's id (T45)
    "batch_unknown",                # beanBatchId is neither a batch nor a bean
    "labels_without_batch",         # sets a coffee name but no batch (T42)
    "labels_mismatch_batch",        # coffee name/roaster differ from the batch's bean
    "duplicate",                    # same name and same values as another entry
    "name_not_unique",              # same name as another entry, different values
)


@dataclass(frozen=True, slots=True)
class Catalogue:
    """What the tablet holds right now, fetched once per listing."""

    batches: dict[str, str]                    # batch id -> bean id
    beans: dict[str, tuple[Any, Any]]          # bean id -> (name, roaster)
    profile_ids: frozenset[str]
    profile_titles: frozenset[str]             # casefolded, not deleted


def payload_of(source: str, raw: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """``(context, profile)`` an entry would apply - what its issues are judged on."""
    if source == "dye2_favs":
        context, profile, _ = favourite_workflow(raw)
        return context, profile
    if source == "dye2":
        workflow = raw.get("workflow") or {}
        return dict(workflow.get("context") or {}), workflow.get("profile")
    return context_patch(raw), None


def issues_of(
    source: str, raw: dict[str, Any], catalogue: Catalogue | None,
) -> list[str]:
    """Everything wrong with one entry that can be told without applying it.

    Without a catalogue (tablet off) only what the entry itself shows is
    judged; the rest would be a guess.
    """
    found: list[str] = []
    context, profile = payload_of(source, raw)
    if source == "dye2" and not isinstance(raw.get("workflow"), dict):
        found.append("no_workflow")
    kind = profile_kind(profile)
    if kind == "name":
        found.append("name_only_profile")
    if catalogue is None:
        return found
    if kind == "reference" and profile["id"] not in catalogue.profile_ids:
        found.append("profile_reference_unresolved")
    if (source == "mine" and not raw.get("pin_profile")
            and str(raw.get("profile_title") or "").casefold()
            not in catalogue.profile_titles):
        found.append("profile_missing")

    batch = context.get("beanBatchId")
    if batch:
        bean_id = catalogue.batches.get(str(batch))
        if bean_id is None:
            found.append("batch_is_bean_id" if str(batch) in catalogue.beans
                         else "batch_unknown")
        elif source != "mine":
            name, roaster = catalogue.beans.get(bean_id, (None, None))
            shown = (context.get("coffeeName"), context.get("coffeeRoaster"))
            if any(v is not None for v in shown) and shown != (name, roaster):
                found.append("labels_mismatch_batch")
    elif context.get("coffeeName") or context.get("coffeeRoaster"):
        found.append("labels_without_batch")
    return found


def mark_duplicates(entries: list[tuple[dict[str, Any], str, dict[str, Any]]]) -> None:
    """Within one source: same name and same values is a duplicate; same name
    and different values is a name that does not pick one entry.

    Measured on the live store: five favourites share one derived label, four
    of them identical in every value that would be applied, the fifth with a
    real profile reference instead of a name - so only four are duplicates.
    ``entries`` are ``(view, source, raw)``; views are changed in place.
    """
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for view, source, raw in entries:
        key = (source, " ".join(str(view.get("name") or "").split()).casefold())
        fingerprint = json.dumps(payload_of(source, raw), sort_keys=True, default=str)
        groups.setdefault(key, []).append((view, fingerprint))
    for members in groups.values():
        if len(members) < 2:
            continue
        for view, fingerprint in members:
            twins = [v.get("id") or v.get("name") for v, f in members
                     if f == fingerprint and v is not view]
            view.setdefault("issues", [])
            if twins:
                view["issues"].append("duplicate")
                view["duplicate_of"] = twins
            else:
                view["issues"].append("name_not_unique")
