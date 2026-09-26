# A documented path for a second writer of recipes

**Project:** decentespresso/dye2 · **About:** `docs/KV_CONTRACT.md`

## The situation

`KV_CONTRACT.md` makes DYE2 the single writer of `dye2.reaplugin/recipes` and
every other consumer read-only. We keep to that: decentespresso-mcp, an MCP
server that lets a chat assistant set up and dial in coffees, reads DYE2's
recipes and applies their `workflow` exactly as the contract describes, and
never writes the key.

That leaves recipes made in a chat stored on our side, where the tablet never
sees them. A person who saves "Seniman - House Blend" in a conversation cannot
tap it on the dashboard the next morning.

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

Write only our own key, read DYE2's as today, and follow whatever schema and
naming you settle on.
