# A stored `workflow.profile` is often a name without a profile, so "ready-to-PUT" does not select one

**Project:** decentespresso/dye2 · **About:** `docs/KV_CONTRACT.md`, "Applying an item to the workflow"

## What the contract says

Every item "carries a `workflow` field that is a ready-to-PUT `WorkflowRequest`
body: `{ context, profile? }`", to be applied with `PUT /api/v1/workflow` and
"no transformation needed".

## What the live store holds

Measured on Decaid 0.8.6+2801 with the bundled DYE2, 2026-09-26:

- Seven of eight `autoFavourites` store `workflow.profile` as
  `{"id": null, "title": "D-Flow"}` - a title, no `id`, no `steps`. One stores
  `{"id": "profile:198546fc…", "title": "D-Flow"}`, an id with no steps.
- The one `recipes` entry stores no `profile` at all, only `context`.

## What PUT then does

Decaid merges a `PUT /api/v1/workflow` into the current workflow. A `profile`
without steps therefore does not select a profile: it keeps the steps of
whatever was running and replaces only its title. Applying the favourite
"Seniman House Blend" to a workflow running another tuned D-Flow left that
other D-Flow brewing, now titled "D-Flow" - verified by comparing the steps
before and after.

A stub that carries a real `id` does not select the profile either: PUT with
`{"profile": {"id": "profile:198546fc983546de03b7", "title": "D-Flow"}}` left
the steps exactly as they were, and Decaid drops the `id` altogether - it is in
neither the PUT response nor the next GET. So the reference cannot work through
this endpoint at all; a consumer has to resolve it and send the full profile.

This is not a consumer mistake: `buildFavouriteWorkflow` in `dev-api.ts` writes
`profile: { id: snp.profileId, title: snp.profileTitle }` for every saved
favourite, and DYE2's own `applyAutoFavourite` in `dashboard.ts` puts the same
stub into the workflow it works on. Read from the source rather than observed
on the dashboard: applying a saved favourite there should rename the running
profile the same way. Only `auto` entries carry the full profile.

## Why it matters

A consumer following the contract to the letter applies a favourite, sees the
profile name it expected, and brews on a different profile. Nothing in the
response says so.

## Suggestion

One of:

- store the full profile in `workflow.profile`, as the contract already says
  auto entries do ("the full recorded profile of the source shot");
- or document that `workflow.profile` may be a reference `{id, title}` and that
  a consumer has to resolve it (`GET /api/v1/profiles/{id}`) and PUT the result -
  and store the `id` whenever the item has one;
- and say what a consumer should do when `id` is null.

## Second observation, same apply path

The recipe "Decaf" stores `context.coffeeName: "Sugar Cane Decaf"` without
`coffeeRoaster` or `beanBatchId`. Applied on another coffee's batch, the machine
shows the other roaster with this bean's name, and the next shot is recorded
that way. Storing all three together - as Decaid's own API examples do - would
keep the labels and the batch in step.

## Third observation: a bean id where a batch id belongs

Six of the eight live favourites store, in `snapshot.beanBatchId` and
`workflow.context.beanBatchId`, an id that answers 404 as a batch and 200 as a
bean - "RT Decaf" and all five untitled Yirga Santos entries. Decaid accepts
any string there (decaid draft 4), so applying one points the machine at a
batch that does not exist, and every shot pulled afterwards records that
reference. The two favourites whose id is a real batch apply cleanly.
