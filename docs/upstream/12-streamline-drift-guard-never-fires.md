# The auto-save's profile-drift guard never fires: the workflow's profile carries no id

**Project:** github.com/allofmeng/streamline_project (redirects to
decentespresso/streamline-js) · **At:** `721bd5fabe` (2026-09-23),
`src/modules/dyeStrip.js`, `src/modules/api.js`

## The guard

`handleWorkflowUpdatedForAutoSave` is meant to stop auto-saving onto a recipe
or favourite once the user has moved to another profile:

```js
const profileId = workflow?.profile?.id;
if (activeItem.profileId && profileId && profileId !== activeItem.profileId) {
    clearActiveItem();
    return;
}
```

`workflow` is the response of `PUT /api/v1/workflow` (`updateWorkflow` in
`api.js` passes `result` to the listeners).

## Why it never fires

Decaid's workflow profile has no `id`. Measured on Decaid 0.8.6+2801,
2026-09-26:

- `GET /api/v1/workflow` → `profile` keys are `author, beverage_type, notes,
  steps, tank_temperature, target_volume, target_volume_count_start,
  target_weight, title, version`;
- a `PUT` carrying `profile: {"id": "profile:198546fc983546de03b7", "title":
  "D-Flow"}` returns a workflow whose profile has no `id` either, and the next
  `GET` has none - Decaid drops it.

So `profileId` is always `undefined`, the condition is always false, and the
active item stays active across a profile change: a dose or grind tweak made
after switching to another profile is saved onto the old recipe.

## Suggestion

Compare what the workflow does carry. The title is the cheapest (`profile.title`
against the title the item was applied with), though two profiles can share a
title; a hash of the brewing-relevant fields (steps, target weight/volume, tank
temperature) is exact. Or clear the active item on any PUT whose payload
contains `profile`, which is what a profile change looks like from the
dashboard.
