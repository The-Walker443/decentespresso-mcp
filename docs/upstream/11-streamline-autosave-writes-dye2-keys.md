# Recipe/favourite auto-save writes DYE2's KV keys, which the KV contract reserves for DYE2

**Project:** github.com/allofmeng/streamline_project (redirects to
decentespresso/streamline-js) · **At:** `721bd5fabe` (2026-09-23),
`src/modules/dyeStrip.js`

## What the contract says

DYE2's `docs/KV_CONTRACT.md`, "Single-writer rule":

> **DYE2 is the sole writer.** It rewrites the whole array on every mutation. A
> consumer (skin/dashboard) is **read-only**: `GET` these keys, never `POST`
> them. Do not merge, dedupe, or write back — you will clobber concurrent DYE2
> edits.

## What dyeStrip.js does

The recipe/favourite auto-save (`handleWorkflowUpdatedForAutoSave` →
`saveItemFields`) folds dashboard edits back into the active item:

```js
const list = await getDye2KvArray(target.key);          // recipes or autoFavourites
...
next[idx] = { ...next[idx], [target.field]: { ...(next[idx][target.field] || {}), ...fields } };
await setDye2KvArray(target.key, next);                 // writes the whole array back
```

That is a read-modify-write of the entire array on `dye2.reaplugin/recipes`
and `dye2.reaplugin/autoFavourites`. The file's own header says so ("mostly a
read-only consumer … the one exception is the recipe auto-save below"), and the
comment above `AUTOSAVE_TARGETS` names the risk: no field-level API, no
version or ETag.

## Why it matters

The store is last-write-wins with no ownership check. DYE2 already writes
`autoFavourites` from two places (plugin runtime and pages, per the contract);
this makes three. An auto-save landing between DYE2's read and write - DYE2
rewrites recent entries after every shot - silently drops one side's change.
The debounce and the re-GET narrow the window; they do not close it.

## Suggestion

Keep the auto-save, but let DYE2 own the write: either an endpoint on the DYE2
plugin that applies a field patch to one item (DYE2 then remains the single
writer and can serialise with its own writes), or a documented exception in
`KV_CONTRACT.md` with the conditions Streamline meets. Either way the contract
and its reference consumer would agree again.

Related: a proposal for a separately owned key for external writers was
drafted for DYE2 at the same time.
