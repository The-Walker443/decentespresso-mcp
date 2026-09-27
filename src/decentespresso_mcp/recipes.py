"""Recipes: one per bean, derived; DYE2's read from its plugin store (SPEC §11.6).

Pure functions, like the guards and the profile forge.

OURS ARE A DERIVATION, NOT AN OBJECT. Per bean there is exactly one recipe,
named "<roaster> – <bean>": where that bean was last dialled in - batch,
grind, dose, yield and the profile as the workflow ran it, embedded whole.
Every write that changes the workflow rewrites it. Nothing to name, pin or
version.

PROJECTED INTO DYE2'S LIST, MARKED. So the tablet can call a recipe up with
one tap, each of ours is written into ``dye2.reaplugin/recipes`` as an item
carrying ``origin`` and ``recipeId``. Items without that marker are DYE2's and
are passed through untouched, in their place. This breaks the contract's
single-writer rule on purpose, the way Streamline's recipe auto-save already
does (dyeStrip.js, saveItemFields): read immediately before the write, replace
only our own items, read back. The store has no ETag, so a DYE2 edit in the
same instant can still be lost; the window is one request wide.
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


#: The marker on every item we write into DYE2's list. Items without it are
#: never touched.
ORIGIN = "decentespresso-mcp"
_ENTRY_PREFIX = "mcp-"


def recipe_name(row: dict[str, Any]) -> str:
    """"<roaster> – <bean>", the same convention as the per-coffee profiles."""
    return (default_title(row.get("bean_roaster"), row.get("bean_name"))
            or str(row.get("bean_name") or row.get("bean_id")))


def recipe_fields(workflow: dict[str, Any], batch_id: str | None) -> dict[str, Any]:
    """The recipe columns from a workflow, as it stands after a write."""
    context = workflow.get("context") or {}
    return {
        "bean_batch_id": batch_id,
        "grinder_setting": _text(context.get("grinderSetting")),
        "grinder_model": _text(context.get("grinderModel")),
        "dose_g": _number(context.get("targetDoseWeight")),
        "yield_g": _number(context.get("targetYield")),
        "profile_json": json.dumps(workflow.get("profile") or {}, ensure_ascii=False,
                                   separators=(",", ":"), sort_keys=True),
    }


def recipe_workflow(row: dict[str, Any]) -> dict[str, Any]:
    """What applying the recipe PUTs: context plus the whole profile.

    Only what the recipe holds; a field it never captured is left as it is on
    the machine rather than cleared. The coffee labels go along with the batch
    (T28), so the tablet shows the bean the batch belongs to.
    """
    context = _compact({
        "beanBatchId": row.get("bean_batch_id"),
        "coffeeName": row.get("bean_name") if row.get("bean_batch_id") else None,
        "coffeeRoaster": row.get("bean_roaster") if row.get("bean_batch_id") else None,
        "grinderSetting": row.get("grinder_setting"),
        "grinderModel": row.get("grinder_model"),
        "targetDoseWeight": row.get("dose_g"),
        "targetYield": row.get("yield_g"),
    })
    body: dict[str, Any] = {"context": context}
    profile = json.loads(row.get("profile_json") or "{}")
    if profile.get("steps"):
        body["profile"] = profile
    return body


def own_view(row: dict[str, Any]) -> dict[str, Any]:
    """Our recipe, shaped after the contract's items so both sources read alike."""
    profile = json.loads(row.get("profile_json") or "{}")
    return _compact({
        "source": "mine",
        "name": recipe_name(row),
        "beanId": row.get("bean_id"),
        "beanBatchId": row.get("bean_batch_id"),
        "profileTitle": profile.get("title"),
        "dashboardVariables": _compact({
            "dose": row.get("dose_g"),
            "drink": row.get("yield_g"),
            "ratio": _ratio(row.get("dose_g"), row.get("yield_g")),
            "grind": row.get("grinder_setting"),
            "grinderModel": row.get("grinder_model"),
        }),
        "updatedAt": row.get("updated_at"),
    })


# ---------------------------------------------------- Projection into DYE2


def is_ours(item: Any) -> bool:
    return isinstance(item, dict) and item.get("origin") == ORIGIN


def entry_id(bean_id: str) -> str:
    return _ENTRY_PREFIX + str(bean_id)


def dial_in(row: dict[str, Any]) -> dict[str, Any]:
    """The dashboardVariables of our entry - what Streamline's auto-save edits.

    Grind as a number where it is one: that is what DYE2's editor and
    Streamline's auto-save write there, and a string would read as a change.
    """
    return _compact({"dose": row.get("dose_g"), "drink": row.get("yield_g"),
                     "grind": _grind_number(row.get("grinder_setting")),
                     "grinderModel": row.get("grinder_model")})


def projection_entry(row: dict[str, Any], captured_at: str) -> dict[str, Any]:
    """Our recipe as an item of ``dye2.reaplugin/recipes`` (KV_CONTRACT.md).

    The ready-to-PUT ``workflow`` is what Streamline's strip applies, profile
    and all (dyeStrip.js applyStoredWorkflow). ``profileId``/``profileTitle``
    are left out on purpose: DYE2's own dashboard ignores ``workflow`` and
    PUTs those two as a stub, which only renames the running profile (T44,
    T52) - without them it sets dose, drink and grind and leaves the profile.
    """
    name = recipe_name(row)
    return {
        "id": entry_id(row["bean_id"]),
        "origin": ORIGIN,
        "recipeId": row["bean_id"],
        "name": name,
        "title": name,
        "subtitle": row.get("bean_name") or "",
        "beverage": "espresso",
        "beanId": row["bean_id"],
        "beanName": row.get("bean_name"),
        "showOnStreamlineDashboard": True,
        "dashboardVariables": dial_in(row),
        "capturedAt": captured_at,
        "workflow": recipe_workflow(row),
    }


def merged_list(store: list[Any], ours: list[dict[str, Any]]) -> list[Any]:
    """DYE2's items where they were, then ours - replaced as a block.

    Ours go after theirs because Streamline's strip shows the first five
    recipes only (dyeStrip.js renderStrip); an item the user made in DYE2
    keeps its place.
    """
    return [item for item in store if not is_ours(item)] + list(ours)


def tablet_dial_in(entry: dict[str, Any], projected: str | None) -> dict[str, Any]:
    """Recipe columns the tablet changed on our entry since we wrote it.

    Streamline's auto-save folds a dashboard edit of dose, drink or grind into
    the active recipe's dashboardVariables (dyeStrip.js recipeAutoSaveFields) -
    and nothing else. That is the dial-in the chat never saw.
    """
    before = json.loads(projected) if projected else None
    now = entry.get("dashboardVariables") or {}
    if before is None:
        return {}
    changes: dict[str, Any] = {}
    for key, column in (("dose", "dose_g"), ("drink", "yield_g"), ("grind", "grinder_setting")):
        if now.get(key) is not None and now.get(key) != before.get(key):
            changes[column] = (_grind_text(now[key]) if key == "grind"
                               else _number(now[key]))
    return changes


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
        "mine": [r for r in own if wanted in (
            " ".join(recipe_name(r).split()).casefold(),
            " ".join(str(r.get("bean_name") or "").split()).casefold())],
        "dye2": hits([i for i in dye2 if not is_ours(i)], dye2_name),
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


def _ratio(dose: Any, drink: Any) -> float | None:
    try:
        return round(float(drink) / float(dose), 2) if dose and drink else None
    except (TypeError, ValueError):
        return None


def _compact(block: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in block.items() if v not in (None, {}, [])}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _grind_number(value: Any) -> Any:
    number = _number(value)
    return number if number is not None else value


def _grind_text(value: Any) -> str:
    """Back to the workflow's spelling: a string, without a trailing ".0"."""
    number = _number(value)
    if number is None:
        return str(value)
    return f"{number:g}"


__all__ = ["ISSUES", "ORIGIN", "Catalogue", "issues_of", "mark_duplicates", "payload_of",
           "DYE2_NAMESPACE", "DYE2_RECIPES", "SOURCES", "TITLE_SEPARATOR",
           "choose", "dial_in", "dye2_name", "dye2_view", "entry_id", "is_ours",
           "merged_list", "own_view", "projection_entry", "recipe_fields",
           "recipe_name", "recipe_workflow", "tablet_dial_in"]


# ------------------------------------------------------------------ Issues

#: Problem codes on a listed recipe or favourite, found before anything is
#: applied. Their meaning is spelled out once, in INSTRUCTIONS.
ISSUES = (
    "name_only_profile",            # profile is a title without steps or id (T44)
    "profile_reference_unresolved", # profile is an id that is not on the tablet
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
    body = recipe_workflow(raw)
    return body["context"], body.get("profile")


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
