# A documented path for a second writer of recipes

**Project:** decentespresso/dye2 · **About:** `docs/KV_CONTRACT.md`

## The situation

`KV_CONTRACT.md` makes DYE2 the single writer of `dye2.reaplugin/recipes` and
every other consumer read-only. decentespresso-mcp, an MCP server that lets a
chat assistant set up and dial in coffees, kept to that at first - and its
recipes stayed on its side, where the tablet never saw them. A coffee dialled
in over a conversation could not be tapped on the dashboard the next morning.

## What we do now, and why it is not the answer

Since our 0.13 we write into `recipes` after all, as a marked projection: one
item per bean, each carrying `origin: "decentespresso-mcp"` and a `recipeId`,
with a complete `workflow`. Items without the marker are never changed and keep
their place; the key is read immediately before every write and read back
after it. That is Streamline's auto-save pattern (`saveItemFields`), for the
same reason: there is no other way onto the dashboard.

It works, and it is still a second writer on a last-write-wins key. An edit in
DYE2 in the same instant as one of our writes can be lost, and DYE2's own
dashboard, which applies a recipe from `dashboardVariables` and a profile stub
rather than from `workflow`, cannot switch the profile with our items. A key of
our own would end both.

## The contract is already the right model

It exists to let a consumer other than DYE2 read a stable shape - the same idea
works in the other direction. What is missing is not permission to write
DYE2's key; it is a key of its own.

## Proposal

A separately owned key - for example `dye2.reaplugin/recipes.external`, or a
namespace per external writer - with:

- the same item schema as `recipes[]`, `workflow` required;
- one documented writer per key (the external tool), so the single-writer rule
  still holds per key;
- DYE2 and Streamline rendering it read-only next to DYE2's own recipes,
  visibly marked as coming from elsewhere, without an edit button.

DYE2 keeps full ownership of `recipes`; nobody races anybody.

## Evidence that a sanctioned path is needed

Streamline, the contract's reference consumer, already writes back into
`recipes` and `autoFavourites`: its recipe auto-save in
`src/modules/dyeStrip.js` (`saveItemFields`) reads the array, patches one item
and writes the whole array back. The contract names the risk itself - no
ETag, last write wins - and the comment in `dyeStrip.js` acknowledges it.

Related, and worth a look on the Streamline side: the auto-save's profile-drift
guard compares `workflow.profile.id` with the id the recipe was applied with.
The workflow's embedded profile carries no `id` (measured on Decaid
0.8.6+2801 - its keys are author, beverage_type, notes, steps,
tank_temperature, target_volume, target_volume_count_start, target_weight,
title, version), so the guard's condition is never true and an edit made after
switching profiles is still saved onto the old recipe.

## What we would do on our side

Move the projection to our own key the day one exists, stop writing
`recipes`, and follow whatever schema and naming you settle on.
