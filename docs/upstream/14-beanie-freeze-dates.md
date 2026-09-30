# Freezing a batch in Beanie leaves Decaid's freezeDate/unfreezeDate unset

**Project:** github.com/giladger/Beanie · **At:** `a54a625` (v0.3.9) · **Decaid:** 0.8.6+2801

## What happens

Freezing or thawing a batch in Beanie records the event in
`extras.storageEvents` (`[{type: "frozen"|"thawed", at: ISO}]`) and sets
`frozen`. Decaid's own fields for the same thing, `freezeDate` and
`unfreezeDate`, stay as they were - unset on a batch Beanie froze.

Repro against Decaid, writing what Beanie writes (`src/api/gateway.ts`,
`toGatewayBatchBody`):

```bash
curl -s -X PUT $DECAID/api/v1/bean-batches/$BATCH -H 'Content-Type: application/json' \
  -d '{"extras":{"storageEvents":[{"type":"frozen","at":"2026-09-20T08:00:00.000Z"}]},"frozen":true}'
curl -s $DECAID/api/v1/bean-batches/$BATCH | jq '{frozen, freezeDate, unfreezeDate}'
# {"frozen": true, "freezeDate": null, "unfreezeDate": null}
```

## Why it matters

`freezeDate`/`unfreezeDate` are Decaid's schema for freezer time. Every other
client reads those: DYE2, the Decaid UI, and tools that compute bean age from
the archive. For them a batch Beanie froze is "frozen since an unknown date",
and after a thaw it looks like it was never frozen - the time in the freezer
counts as ageing. Beanie's history is richer (several periods) and stays
exactly as it is; the two fields would only carry the latest period alongside.

## Where

All three builders in `src/domain/beanFreshness.ts` return the batch patch
that every caller spreads into its save:

- `appendBatchStorageEvent` - freeze/thaw (`beanInventoryBrowserFlow.ts`
  `setStorageState`, the freeze-stock flows in `beanInventoryController.ts`
  and `beanInventoryPolicy.ts`);
- `editLastBatchStorageEventDate`;
- `setBatchStorageEventDates` - the date editor in
  `beanInventoryBrowserFlow.ts`.

## Suggestion

Set both fields in those three patches, derived from the events: `freezeDate`
the latest freeze, `unfreezeDate` the thaw after it, or null while frozen. A
patch doing that is attached as `patches/14-beanie-freeze-dates.diff` (2 files,
+29/-3): a `decaidFreezeFields(events)` helper and the two optional fields on
`BeanBatch`. With it, `tsc --noEmit` is clean and the freezer-related suites
pass (beanFreshness 20, beanInventoryBrowserFlow 6, beanInventoryController
53, storageEventsMigration 7). The full `npm test` runner could not be started
on Windows (`ERR_UNSUPPORTED_ESM_URL_SCHEME` from tsx, with and without the
patch), so the other suites were not run.

## Before a pull request

The repository has no licence file, so it is unclear under which terms a
contribution would be accepted. Worth asking in the issue first, rather than
opening a PR with the patch.
