# decentespresso-mcp — specification

**Applies to version 0.13.0.** This document describes the system as it is, not
how it came to be. Where a decision still explains behaviour, the reasoning is
kept; where it only explains history, it is gone.

---

## 1. What this is

A self-hosted MCP server (Docker, homelab) that archives the espresso shots of a
Decent DE1 and gives Claude access to them through a custom connector.

Three things it does:

1. **Archives every shot locally** in SQLite - metadata, the full measurement
   series, and the profile each shot ran on. The archive outlives whatever the
   tablet keeps.
2. **Derives metrics deterministically** from the measurement series, so values
   are comparable across shots, and diagnoses the puck from them.
3. **Answers compactly.** Tool responses are sized for a conversation, not for a
   dump: shape before raw numbers, three levels of detail, a budget per
   response.

**Not goals:** no web UI, no multi-user, no deleting of shots.

## 2. Architecture

```
        DE1 ──BLE──▶ Decaid (tablet, private IP)   ← source of record
                          │  REST, local network only, no auth
                          ▼
                 decentespresso-mcp (Docker)   ← archive and analysis
                          │  internal Docker network
                    cloudflared ──▶ Claude
                          ▲
                          └─ NEVER Decaid traffic (§11)
```

Decaid on the tablet is the source of record for shots. This server is the
archive and the analysis layer. Nothing in that chain leaves the local network;
the tunnel exists only so Claude can reach the MCP endpoint.

The tablet is not always on - it runs while coffee is being made. That is a
normal state, not a fault: see §6.

## 3. Tech stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python ≥ 3.12 | ecosystem, no compiled dependencies needed |
| MCP framework | `fastmcp` 2.x | streamable HTTP transport built in |
| Transport | streamable HTTP | what Claude supports; SSE is on its way out |
| HTTP client | `httpx` | async, timeouts, retries |
| Database | SQLite in volume `/data` | single user, file backup, no extra container |
| Container | `python:3.12-slim`, non-root | small; no apt layer is needed |
| Tests | `pytest` with fixtures from real responses | deterministic |

## 4. The source: Decaid

Verified against the running instance, Decaid **0.8.6+2801** (2026-09-26). The
constant `VERIFIED_DECAID_VERSION` in `decaid_client.py` holds that version;
`status()` warns when the tablet reports a different one.

The table below was built against Decaid 0.8.5+2624. When the tablet moved to
Decaid 0.8.6 the findings this server leans on hardest were measured again, and
one had changed: T4, T8, T9, T11, T12, T13, T16, T22 (creation), T32 and T34
hold; **T10 changed** - `/shots/latest` no longer carries the measurements.
T37-T41 are new, measured on Decaid 0.8.6. The rest (T14, T15, T17-T21,
T23-T26, T28, T29, T33) was not re-probed; where the code depends on one of
them it checks before it writes rather than trusting the table.

**Verification is a duty, not a courtesy.** Every finding below was measured
against the live API, and each one that shapes behaviour is noted at the
relevant constant in the code.

**The canonical API description** is `assets/api/rest_v1.yml` in the Decaid
repository (<https://github.com/decentespresso/decaid>). That is the file to
cite and the file to check against. The `rea_restapi.yml` circulating from dye2
is a copy for plugin developers - useful, but a copy, and behind the original
whenever the two disagree. Where the YAML and the running instance disagree, the
instance wins and the finding goes in the table above.

### 4.1 Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/v1/info` | version, commit, build time |
| `GET /api/v1/shots?limit=&offset=&order=` | everything except `measurements`, `updatedAt` included |
| `GET /api/v1/shots/ids` | every id in one go, unpaginated |
| `GET /api/v1/shots/latest` | the newest shot, without measurements since Decaid 0.8.6 (T10) |
| `GET /api/v1/shots/<id>` | one shot in full, including `measurements` |
| `GET /api/v1/beans`, `GET /api/v1/bean-batches` | beans and batches, unpaginated |
| `GET /api/v1/workflow` | the setting for the next shot, including the profile |
| `PUT /api/v1/shots/<id>`, `/beans/<id>`, `/bean-batches/<id>`, `/workflow` | write paths, see §11 |
| `POST /api/v1/beans`, `/beans/<id>/batches` | creation, 201 with the new record (T41) |
| `GET/POST /api/v1/profiles`, `GET/PUT /profiles/<id>` | profile records with content-hash ids (T37) |

### 4.2 What the API actually does

| # | Checked | Result |
|---|---|---|
| T1 | Path prefix | Everything under `/api/v1/`. Without it: 404 |
| T2 | `/info` | `{version, fullVersion, commit, commitShort, buildTime, buildNumber, localIp, appStore, branch}` |
| T3 | List envelope | `{items, total, limit, offset}` |
| T4 | `limit` ceiling | **Caps silently at 100.** `limit=500` returns 100 items without an error |
| T5 | `offset` | Works |
| T6 | `order` | `asc` oldest first, `desc` newest; default `desc` |
| T7 | Filters | `beanId` works. `profileId` is **ignored** |
| T8 | Server-side time filter | **Does not exist.** `updated_after`, `since`, `sort` are silently ignored |
| T9 | `/shots/ids` | All ids, unpaginated, as a flat array |
| T10 | `/shots/latest` | **Changed in Decaid 0.8.6:** everything except `measurements`; it was the full detail before. Nothing here reads the series from it |
| T11 | `/shots/<id>` | Full detail |
| T12 | `measurements` shape | `{machine{…}, scale{…}, volume}` per point. **No `time` field** - the axis comes from `machine.timestamp` minus the first. `profileFrame` sits under `machine` |
| T13 | `PUT` semantics | Deep merge: sending one annotation leaves the others and all measurements untouched |
| T14 | Protected fields | `createdAt`, `measurements`, `id` → **400**, nothing changed |
| T15 | `updatedAt` | Set server-side on a content change; `createdAt` untouched |
| T16 | Beans and batches | `/api/v1/bean-batches` or `/api/v1/beans/<id>/batches`; `/api/v1/batches` → 404. Single-item fetches **do** exist: `/api/v1/beans/<id>` and `/api/v1/bean-batches/<id>` both return 200 |
| T17 | WebSocket | `/ws/v1/machine/shotState` → 101 Upgrade. `/ws/v1/machine/state` → 404 |
| T18 | `enjoyment` scale | 0–100 as a float up to 0.8.6. **Superseded by T46**: Decaid's own 0–10 from decaid#887 on |
| T19 | Retention | No pruning endpoint; the archive reaches back without gaps |
| T20 | Writable annotations | `espressoNotes`, `enjoyment`, `actualDoseWeight`, `actualYield` |
| T21 | Writable on a bean | `name`, `roaster`, `species`, `processing`, `notes`, `decaf`, and the origin fields of T31 |
| T22 | Writable on a batch | `roastDate`, `buyDate`, `openDate`, `bestBeforeDate`, `freezeDate`, `unfreezeDate`, `frozen`, `weight`, `weightRemaining`, `roastLevel`, `harvestDate`, `qualityScore`, `price`, `currency`, `notes`. Dates come back with a time attached - the long-standing three with a trailing `Z`, the later ones without |
| T23 | **`unfreezeDate`** | **Exists and is writable.** An earlier entry here said it did not exist; that was read off a response where it was unset, and an absent key is not an absent field. Written and read back 2026-09-16. With it the freezer time is subtracted exactly instead of the age becoming an upper bound |
| T24 | Writable on the workflow | `context.grinderSetting`, `context.grinderModel`, `context.targetDoseWeight`, `context.targetYield`, `context.beanBatchId`, and `profile` as a whole object (T38) |
| T25 | Protected on write | `id` → 400 ("ID in path does not match"), `createdAt`/`updatedAt` → 400 ("system-managed") |
| T26 | **`timestamp`** | **Not protected.** A `PUT` carrying it returns 200 and the value stands. Rechecked on Decaid 0.8.6 (2026-09-26), with the original kept as an exact string this time and restored exactly |
| T27 | **Weight on a batch** | **Exists and is in use.** `weight` and `weightRemaining` are writable, and the reference batch now carries 500 g. The earlier entry called them nonexistent on the strength of a response that omitted them while unset. See T34 for why only the first of the two can be trusted |
| T28 | **Batch and coffee labels** | **Kept apart, and nothing joins them.** `context` holds the managed reference `beanBatchId` next to the display strings `coffeeName` and `coffeeRoaster`; setting the batch alone leaves the previous coffee's name standing on the machine. Measured live: after `beanBatchId` was moved to the decaf batch, `coffeeName` still read `Arabica Honey Process`. Decaid's own API examples write the id and both labels together |
| T29 | **`beanBatchId` is unchecked** | An arbitrary UUID is accepted with 200 and stands afterwards. There is no referential integrity, so the client is the only thing between a typo and a workflow pointing at nothing |
| T30 | **`coffeeData` is gone** | The legacy containers `doseData`, `grinderData` and `coffeeData` stopped being accepted in Decaid 0.5.2. Everything goes through `context`, whose fields are flat |
| T31 | **Origin on a bean** | `country`, `region`, `producer`, `variety` (array of strings), `altitude` (`[min, max]` in metres) and `decafProcess` all exist and all six are writable (verified 2026-09-16, restored with null) |
| T32 | **Unset means absent** | An unset field is left out of the response entirely rather than sent as `null` - which is what made T23 and T27 wrong. Decaid's UI shows grey placeholder text in empty fields ("washed, natural, honey…"); the API never sends those |
| T33 | **`null` clears a field** | A `PUT` carrying an explicit `null` removes the value. That is how a probe is undone, and the only reason the origin fields could be verified without leaving residue |
| T34 | **`weightRemaining` does not count down** | It is initialised to `weight` when the batch is created and nothing decrements it. On the reference batch it reads 500 g of a 500 g bag after 27 shots that consumed 486 g. A remainder worth having is derived from the recorded doses; Decaid's figure is reported beside it, never instead of it |
| T35 | **`harvestDate` is not a date** | The API calls it "harvest date or season" and the real value is `"2026"`. Stored and validated as text - parsing it would either fail or invent a first of January |
| T36 | **Future dates are legitimate** | `bestBeforeDate` lies ahead by nature, and the operator's `openDate` did too. A blanket "no future dates" rule refused both |
| T37 | **A profile's id is its content** | `profile:<20 hex>`, hashed from the brewing-relevant fields; the title is not among them. A `PUT` that changes brewing content moves the record to a new id and the old id answers 404 - replaced, not kept alongside. `parentId` and `metadata` survive the move. Identical content gives the identical id on every creation: a probe and the acceptance run produced `profile:0291c850b4407417dd79` independently |
| T38 | **The workflow embeds, it does not refer** | `workflow.profile` is a full profile object with no id anywhere in the workflow. Selecting a profile means copying it in; which record it came from can only be told by comparing content. A `PUT` carrying `profile` is accepted and leaves `context` untouched |
| T39 | **A duplicate creation reports success** | A `POST /profiles` whose content already exists answers **201** with the *existing* record and silently drops the new title, `parentId` and `metadata`. Nothing is created. A client trusting the status code reports a new profile that is not there |
| T40 | **Deleting a bean does not cascade** | `DELETE /beans/<id>` on a bean with batches fails with 500 (SQLite foreign key, code 787) although the API description says it deletes the batches too. Nothing is deleted. Batches first, then the bean. Not used by this server, which deletes nothing - found while cleaning up probes |
| T41 | **Creation: required and returned** | `POST /beans` needs `name` and `roaster` (both refused alone with 400, a type-cast error rather than a message) - but **accepts both as empty strings** with 201, a bean with no name (measured, deleted) and returns 201 with the new id. `POST /beans/<id>/batches` needs nothing and sets `weightRemaining` from `weight`. `POST /profiles` returns 201 with the record, visible, not default. Titles are not length-limited by the API - 300 characters were taken |
| T42 | **A DYE2 recipe can mislabel the coffee** | The live recipe "Decaf" stores `context.coffeeName` and neither `coffeeRoaster` nor `beanBatchId`. Applied as stored on a Grano Gayo batch, the machine showed "Coffee Circle / Sugar Cane Decaf". `apply_recipe` applies it unchanged, as the contract asks, and reports the mismatch |
| T43 | *(withdrawn)* | Recorded on 2026-09-26 as "every DYE2 favourite carries a D-Flow stored nowhere". Wrong: the comparison hashed stubs without steps against each other. T44 is what the data shows |
| T44 | **A DYE2 `workflow.profile` may be a name without a profile** | Seven of eight live favourites store `{"id": null, "title": "D-Flow"}`, one an id without steps - and Decaid drops a profile `id` sent to the workflow and does not resolve it (measured with a real id); the recipe stores no profile. PUT as stored, such a stub only renames the running profile (T13) - applying "Seniman House Blend" left the previously running tune brewing. Reported on apply; the unsaved-profile guard does not fire for it, because nothing is replaced |
| T46 | **Ratings move to 0–10** (decaid#887, on main after 0.8.7-beta.1, in no release yet; read from the source 2026-09-26) | `annotations.enjoyment` becomes Decaid's own 0–10 field; `PUT /shots` refuses values outside it with 400, and that check runs **before** the shot is looked up. The schema-6 migration divides by ten every value over 10, and at 10 or below only an untouched de1app import (`id LIKE 'de1app-%'`, `created_at != timestamp`, `updated_at <= created_at`); it does **not** set `updated_at`. Everything else from 1 to 10 stays as it is - Decaid calls these ambiguous and does not guess. DYE2 moved to 0–10 with dye2#8 (0.1.15, stars ×2) and **already runs on the 0.8.6 tablet**, so new DYE2 ratings there are 0–10 inside a 0–100 field. Live, read-only: 88 imports, all `createdAt == timestamp`, so none untouched; two native shots at 4.0 and 5.0 (DYE2's raw star index before dye2#7). The probe - `PUT` of `enjoyment: 11` to a non-existent id - answers 404 "Shot not found" on 0.8.6+2801 and changes nothing |
| T47 | **Profile upload to the DE1 has no completion signal in the API** (source v0.8.6 + live, 2026-09-27) | `PUT /workflow` answers once the workflow is stored. The upload runs afterwards in WorkflowDeviceSync - asynchronous, over Bluetooth, queued - and is **skipped silently** while the DE1 is not connected, then pushed on the next connect. A failed attempt raises `profileUploadFailed` (retries after 3, 10, 30 s), visible only in `connectionStatus.error` of the `/ws/v1/devices` socket - one slot, currently held by an older `scaleDisconnected` - and as a WARNING in the log. Success is reported nowhere in the API. It is visible in `/api/v1/logs`: the upload's last write logs `DE1 - mmr write: tankTemp` at INFO (the tablet logs at INFO; FINE lines such as "encoding step" are absent). Measured, DE1 connected and asleep: 0.79, 0.86, 0.87 and 0.92 s from the PUT to that line; in a day of log every profile PUT had one within about a second. A sleeping DE1 takes the upload |
| T48 | **Two tablet clients write the workflow from their own state** (source DYE2 0.1.15 and streamline-js 6f2b914 on Decaid 0.8.6; log 2026-09-27) | DYE2's dashboard loads the workflow once when the page opens and never refreshes it; every edit there, and "Done", PUTs that whole copy back - context and profile - over whatever changed meanwhile. Streamline, on a profile switch, writes `targetDoseWeight`, `targetYield` and `grinderSetting` from the profile record's `metadata`, and `grinderSetting: null` when none is saved there; the batch it leaves alone. Both write from 127.0.0.1. The shot of 2026-09-27 08:33 (D-Flow, grind "3.50", Grano Gayo) followed such a local PUT by 2.1 s; "3.50" is Streamline's spelling (`toFixed(2)`), not DYE2's. Where the value came from is **not yet reproduced** - D-Flow's metadata holds "2.60" today |
| T49 | **When a finished shot is not stored** (source v0.8.6, log 2026-09-26/27) | Three cases, only one of them logged: aborted during preheat, before preinfusion or pour ("Shot aborted" at INFO); `blockOnNoScale` without a scale; and a profile of `beverage_type` `cleaning` or `calibrate` **in the workflow when the shot ends** - dropped without a log line. The stored record takes the workflow of that moment too, not of the start. Across the log window (2026-09-26 16:00 to 2026-09-27 09:00) one run went unstored: 22:22, "Cleaning/Forward Flush x5", eight frames advanced - by design |
| T50 | **Streamline clears the coffee from the workflow after every stored shot** (streamline-js source; log on Decaid 0.8.6, 2026-09-27) | `clearDyeWorkflowContext` sets `beanBatchId`, `coffeeName`, `coffeeRoaster`, grinder, barista, drinker and basket to null once a shot is persisted, so they do not label the next one - DYE2 is meant to restate them per shot. Measured: "Storing shot" 08:34:11.99 → `PUT /workflow` from 127.0.0.1 at 08:34:12.05; 12:47:27.68 → 12:47:27.76. Grind, dose, yield and profile stay. The context-lost finding of M12 (B) follows from this, not from a fault |
| T51 | **Streamline's DYE strip: five recipes, applied from `workflow`, auto-saved into `dashboardVariables`** (streamline-js 6f2b914, on Decaid 0.8.6) | The strip shows the first five visible recipes in array order (`showOnStreamlineDashboard !== false`). Tapping one GETs the live workflow, merges `workflow.context` over it, takes `workflow.profile` whole, and PUTs. Its auto-save folds a later dashboard edit of dose, drink or grind into that recipe's `dashboardVariables` only - which its own apply never reads while a `workflow` is present, so on a DYE2 recipe the edit does not come back. Since streamline-js fe73b4a the auto-save stops when the workflow's profile no longer matches a fingerprint of the applied one (`JSON.stringify` of title, targets, tank temperature and the steps without temperature) - key order counts, so a projected profile must keep Decaid's own order, which is not alphabetical |
| T52 | **DYE2's own dashboard ignores a recipe's `workflow`** (DYE2 0.1.15 source, on Decaid 0.8.6) | `applyRecipe` takes dose, drink and grind from `dashboardVariables` and sends the profile as `{id: profileId, title: profileTitle}` - a stub that renames the running profile and keeps its steps (T44). A recipe can change the profile with one tap only through Streamline's strip |
| T53 | **DYE2's recipe editor keeps foreign items** (DYE2 0.1.15 source, on Decaid 0.8.6) | It shows five fixed slots, ids "1"-"5", but saves through `updateRecipe`: GET the array, upsert by id, POST. Items with other ids survive an edit in DYE2. The store's POST replaces the whole value and answers `{}` (decaid `kv_store_handler.dart`) |
| T54 | **Only ids "1"-"5" exist for DYE2's editor and Streamline's long press** (reported by the operator 2026-09-28; streamline-js cc14749 and DYE2 0.1.15 source, on Decaid 0.8.6) | A projected recipe with the id `mcp-<bean>` showed in Streamline's strip but not in DYE2's "Describe your espresso" overview, which normalises the list to five slots with ids "1"-"5". Streamline's long press opens slot `parseInt(id) \|\| 1` - for `mcp-...` that is slot 1, the user's own Decaf. Our items therefore take DYE2's free slots |
| T45 | **A bean id where a batch belongs** | Six of eight live DYE2 favourites store a bean's id as `beanBatchId` (404 as a batch, 200 as a bean). Decaid would take it (T29). Applying such a favourite is refused, naming the bean's batches |

T26 is why the block list in `writes.py` is not a second line of defence but the
only one for the telemetry fields.

### 4.3 What the shot payload carries

Beyond the obvious: `measurements` carry **`targetPressure` and `targetFlow` per
data point**, and `machine.state.substate` names the phase in plain text. Both
matter more than they look:

- Profile compliance is **measured**, not estimated. The target the machine was
  aiming at is in the data next to what it achieved.
- The end of preinfusion is **read off**, not inferred (§8.2).

`context.extras` carries the basket (`basketName`, `basketId`). A shot names
only its batch; which bean that is sits on the batch.

## 5. Data model

SQLite in `/data`, `PRAGMA journal_mode=WAL`. Migrations are numbered SQL files
applied at startup; `migrations/001_decaid_init.sql` creates everything.

`db.py` refuses to open a database written against a superseded schema. Those
migrations consist of `CREATE TABLE IF NOT EXISTS`, so applied to an old file
they would silently do nothing and leave the old tables standing while the
server acted as if it were current.

### 5.1 Tables

`shots` — one row per shot. Beyond the obvious columns:

- **`time_source`** (`utc` | `local_berlin`) records where the timestamp came
  from before it became UTC. Without it, what was converted could not be traced
  afterwards. See §5.2.
- **`bean_batch_id`, `bean_id`** carry no foreign key on purpose: a shot must
  not fail to be archived because its batch was deleted in Decaid. `bean_id` is
  resolved from the batch during ingestion.
- **`raw_json`** holds the response **without** the measurements. Those live in
  `shot_series`; a detail response weighs about 140 kB with them, which across
  the archive would be a multiple of everything else, stored twice.
- **`enjoyment`** is 0–10 or NULL, whatever scale the tablet runs. NULL means
  not rated. `enjoyment_ambiguous` marks a value whose scale cannot be told.
  See §5.3 and §5.4.

`shot_series` — one row per data point, `(shot_id, elapsed)` as the key.
Carries the measured channels, the **target** channels
(`target_pressure`, `target_flow`, `target_temp_mix`, `target_temp_basket`) and
the phase markers (`state`, `substate`, `profile_frame`).

`beans`, `bean_batches` — Decaid's catalogue, mirrored so that queries and
guards work without the tablet. Batches carry `roast_date`, `buy_date`,
`freeze_date`, `frozen`.

`profiles` — one row per profile version, deduplicated by `version_hash`.
`shot_metrics` — the metrics cache, keyed by shot, tagged with the version it
was computed under. `sync_state` — key/value: cursor, last run, errors.

### 5.2 Time: the archive keeps UTC

Decaid does not do so uniformly. Shots imported from the de1app carry their
timestamp in UTC; shots Decaid recorded itself carry local time with no zone.
The discriminator is the identifier (`de1app-<unix time>` = UTC), cross-checked
against `createdAt` - if the two disagree, Decaid has changed its behaviour and
that produces a warning rather than a silent error.

Conversion runs through `Europe/Berlin` **with full daylight saving handling**.
A fixed offset would be wrong after the last Sunday in October. In the ambiguous
hour when the clocks go back, the first reading applies. `tzdata` is a
dependency because a slim base image does not guarantee `/usr/share/zoneinfo`.

The split is clean and without overlap, exactly at the day Decaid took over the
recording: 88 shots `utc`, 81 `local_berlin`.

### 5.3 Rating: a `0.0` from the import era is not a rating

Decaid creates imported shots with `enjoyment: 0.0`. Measured across the
archive: 75 of the 88 imported shots sit like that, real ratings run from 20 to
100, and **not a single** natively recorded shot ever carries `0.0`. For a shot
with a de1app identifier, a 0 is therefore archived as `NULL`.

Without this rule, 75 phantom ratings would enter the archive and the guards in
§9 would take them at face value. When **writing**, the rule does not apply:
what the user explicitly sets to 0 is an input.

### 5.4 Rating scale: the archive keeps Decaid's 0-10

Decaid moves `enjoyment` from a pass-through of de1app's 0-100 to its own 0-10
(T46). The archive takes 0-10 as canonical on either side of that change, so a
rating means the same whenever it was read.

**The scale is asked of the tablet, not inferred from its version.** Once per
Decaid version, a `PUT` of `enjoyment: 11` goes to a shot id that cannot exist:
with #887 the range check refuses it (400), before it the lookup does (404).
Neither writes anything. The build number is only the fallback when the answer
is neither: builds are numbered by commits on main, #887 is in every build from
2836 on, but a build off another branch can carry that count without it.

**Read from a 0-100 tablet, ratings are converted by Decaid's own rule**, so the
archive holds before the update what the tablet will hold after it: over 10
divided by ten, 10 and below only on an untouched import. What that rule leaves
alone is kept as it is and marked `enjoyment_ambiguous`: a low 0-100 rating,
DYE2's raw star index and a 0-10 write by today's DYE2 on a 0-100 tablet look
identical. `stats` leaves those out of means and counts them; `audit_archive`
lists them, so they can be rated again. Migration 005 converted the archive by
the same rule.

**Once the tablet is on 0-10, every archived shot's annotations are read again**,
once, from the shot list. Decaid's migration does not move `updatedAt`, so the
cursor of §6 would never see a value it changed. Where the archive's conversion
predicted the result, nothing changes; where it did not, the tablet wins. A mark
survives the switch as long as value and `updatedAt` stand - Decaid left such a
value as it was - and goes when the shot is rated again.

**Writing follows the tablet's scale.** `update_shot` takes `enjoyment` as
Decaid will check it: 0-10 with one decimal after #887, whole numbers 0-100
before - and there not 1 to 10, which Decaid's migration would keep and so read
ten times higher afterwards. The answer reports the tablet's scale and the
archived value.

## 6. Ingestion

**One procedure, not two.** The shot list returns everything except
`measurements`, `updatedAt` included, so without a single detail request it is
known which shots changed. Backfill and incremental run are the same algorithm;
`full` chooses only the label.

It used to force a refetch of every listed shot, and that made the first
backfill of a large archive impossible to finish: with more shots than the
per-run cap, each run re-selected the same oldest 60, stored nothing new, left
`pending` where it was and never set the done marker - so the next run made the
same choice again. A backfill means "make sure everything is here", not
"download it all again".

**The list is always read in full.** It is sorted by shot time, not by
modification time, so a shot pulled in June whose note was added today still
sits at the back. Paging that stopped early would never see it. For ~170 shots
the full list is two requests on the local network - cheap, and it is the whole
point of having a cursor.

**Cap per run:** 60 detail requests. A detail weighs about 140 kB; a first
backfill would otherwise be over 20 MB in one go over the tablet's Wi-Fi. A
started backfill counts as complete only once nothing is outstanding, and each
run must therefore carry on where the last one stopped. Measured on a fresh
archive of 174 shots: 60, then 54, then done.

**A tablet that is off is not an error.** The sync enters `waiting_for_tablet`,
backs off and catches up later. No error message, no notification, no red
status; `status()` simply shows when contact last existed.

Optional: `/ws/v1/machine/shotState` can trigger a fetch on shot end. Polling
stays active as the fallback.

## 7. Profiles

The source is the profile JSON embedded in the shot's workflow. There is no
second request and no foreign format to parse - the profile a shot ran on
arrives with the shot and cannot be missing or unreadable.

Two hashes:

- **`version_hash`** is the *identity* of a version. A shot's link hangs on it
  and it is never touched.
- **`semantic_hash`** groups versions that brew alike. Only the steps and the
  targets go in; title, author and notes do not.

The occasion for the second hash was a real case: two versions of the default
profile differed only in two empty extra keys and a double space in the notes.
Without grouping they read as two different profiles.

## 8. Metrics

`metrics.py`, computed once per shot and cached in `shot_metrics` together with
the `METRICS_VERSION` they were produced under. Bumping that constant
invalidates the cache; the next access recomputes everything, with no migration
and no re-sync.

Units throughout: pressure bar, flow ml/s, weight g, temperature °C, time s.
Values are rounded (pressure 0.1, flow 0.01, time 0.1 s). A missing basis makes
the field `null` and adds a line to `warnings` - never a zero, never a guess.

### 8.1 Definitions

| Metric | Definition |
|---|---|
| `t_first_drops` | smallest `elapsed` with `weight > 0.3 g` |
| `pi_end` | end of preinfusion, see §8.2 |
| `peak_pressure_infusion`, `t_peak` | pressure maximum within `[0, pi_end + 2 s]` |
| `max_pressure_global` | pressure maximum across the whole shot |
| `pressure_dip_after_peak` | `peak_pressure_infusion − min(pressure)` within `[t_peak, t_peak + 4 s]` |
| `avg_flow_pour` | mean `flow_out` over `[pi_end, end]` |
| `flow_stability` | coefficient of variation of `flow_out` over the same window |
| `end_pressure` | mean `pressure` over the last 2 s |
| `pressure_trend_pour` | linear slope (bar/s) of `pressure` over `[pi_end, end]` |
| `temp_basket_mean`, `temp_basket_std` | over `[pi_end, end]` |
| `duration_s`, `ratio` | last `elapsed`; `yield/dose` |

**Two pressure maxima, deliberately kept apart.** `peak_pressure_infusion` is
the pressure that builds the puck - the number that matters when dialling in.
`max_pressure_global` is the maximum across the shot; with a ramping profile it
sits on the last data point and says nothing about the puck. They are never to
be read against each other as a "rise".

**Why `end_pressure` is a mean over 2 s.** The final single reading reacts to
one outlier. On the old reference shot the last value was 5.43 bar while the 2 s
mean was 5.32 - the mean is the more robust measure.

**Why `t_first_drops` uses a 0.3 g threshold.** The first weight at all can
appear well before that, inside the scale's noise band (resolution ~0.1 g; drop
impact and vibration cause deflections). A lower threshold would vary with the
scale and the cup placement and make values incomparable across shots.

### 8.2 `pi_end`: three sources, best first

`pi_end_source` names the one actually used.

| Source | How | Quality |
|---|---|---|
| `substate` | the machine reports the change to `pouring` | read off |
| `profile_frame` | last step boundary before the pressure anchor | inferred |
| `heuristic` | pressure first reaches `0.6 × max_pressure_global` | approximation |

The hierarchy is not a precaution. Across the archive: of 88 imported shots, 84
fall to `profile_frame` and 3 to the heuristic; of 81 native ones, 77 use
`substate`. Without the second stage, 84 shots would carry a guessed `pi_end`.

`profileFrame` still holds the previous shot's value on the first data point, so
a change inside the first 0.5 s is the cleanup, not a step change, and is
discarded.

With `heuristic` the value is an approximation and not fit for comparisons down
to a tenth of a second; the metric says so through `warnings`.

### 8.3 Plausibility of the scale

Three cases make derived values unusable instead of silently wrong:

- **Scale not tared** - more than 0.3 g within the first half second cannot be
  the shot; the machine preinfuses for seconds. `t_first_drops` becomes `null`.
- **Mean pour flow ≤ 0** - a coefficient of variation around such a mean is
  negative and meaningless. `flow_stability` becomes `null`.
- **Weight goes negative** - the scale was knocked. Values stay, a warning
  points at them.

### 8.4 Curve shape

`curve_shape` describes the shot segment by segment along the machine's phase
markers - the same source as `pi_end`. Per segment and channel: starting value,
ending value, direction (`rising` | `falling` | `steady`) and whether the path
between the ends is linear.

`dir` compares the ends (`steady` below 0.2 bar or 0.15 ml/s). `linear` is the
maximum deviation from the straight line relative to the range in that segment,
bounded at 15 %. Both together: a segment can be `steady` and still not linear
if it has a dip.

`source` records where the boundaries came from: `machine`, `markers` (derived
from `pi_end` instead) or `none`.

This answers questions about rise, fall, plateau, phase length and the
comparison of two shots without a single raw number, at about a tenth of the
size of the point arrays.

### 8.5 The settled window

Every diagnostic below is measured over the stretch in which the machine is
holding its pressure target, not over `[pi_end, end]`.

`pi_end` is where preinfusion ends *as the machine reports it*, and on some
profiles that is the first second of the shot - the machine calls everything
`pouring` from the start. A window anchored there still contains the pressure
ramp, and a resistance trend measured over it comes out with the wrong sign: on
the acceptance reference +0.09 per second (rising), while the flow at a
constant 7.5 bar plainly says the bed is opening up.

The window therefore starts where `|pressure − target| ≤ 0.6 bar` for a target
above 1 bar, **plus 4 seconds** for the bed to finish compacting. That delay is
measured: without it the robust trend and the plain first-to-last direction
agree on only 40 % of shots; at 4 s they agree on 85 % and only 7 of 149 shots
lose their window; 6 s reaches 92 % for one further shot lost, which is not
worth the data.

A shot that never settles - a flush, an abort, a profile that holds no
pressure - gets no diagnosis rather than a wrong one. That is 29 of 171 shots.

### 8.6 Puck resistance

`pressure / flow²`, over the settled window, using the **pump** flow. A
simplified Darcy analogue: for flow through a porous bed the pressure rises
roughly with the square of the flow, so the quotient stays near-constant while
the bed does. Unit bar·s²/ml².

The pump flow rather than the scale: that is the water going *into* the puck,
where the scale sees what comes out, seconds later and smoothed by the basket.
Points below 0.4 ml/s are excluded - the error is squared in the denominator,
so at 0.2 ml/s a sensor wobble of 0.05 moves the result by 60 %.

The trend is a **median of pairwise slopes**, not a least-squares fit: the
latter is at the mercy of its last points, where a collapsing flow sends the
value towards infinity.

| Band | Range | Measured share |
|---|---|---|
| `very_low` | < 1.0 | 10 of 171 |
| `low` | < 2.5 | 33 |
| `moderate` | < 5.0 | 48 |
| `high` | < 9.0 | 37 |
| `very_high` | ≥ 9.0 | 14 |

These are this archive's own quartiles (p50 = 3.9, p90 = 8.5), not the bands
gaggimate-mcp validated - on this grinder and these profiles their scale would
call half of all shots "high". The figure is not absolute: use it to compare
shots on this machine, and above all to watch one shot change within itself.

| Trend | Slope per second |
|---|---|
| `rising` | > +0.15 |
| `steady` | −0.15 … +0.15 |
| `declining` | −0.45 … −0.15 |
| `steep_decline` | ≤ −0.45 (the steepest 15 %) |

### 8.7 Channeling: five independent indicators

Each has its own physical signature and keeps its own raw value. An indicator
that could not be computed is **absent from `based_on`** rather than counted as
passing - a knocked scale must never read as good news.

| Indicator | Signature | Threshold | Fires on |
|---|---|---|---|
| `pressure_dip` | the bed gave way and the pump briefly lost against it | ≥ 0.5 bar (p95) | 8 of 171 |
| `flow_instability` | flow would not settle while a constant target was held | ≥ 0.10 ml/s (p90) | 9 |
| `flow_divergence` | the pump delivered more than the scale received | ≥ +0.30 ml/s (p99) | 2 |
| `early_drops` | liquid arrived before preinfusion ended | ≥ 5.0 s (p90) | 12 |
| `resistance_trend` | the bed lost resistance while pressure was held | ≤ −0.45/s (p13) | 18 |

`flow_instability` is measured only inside stretches where the machine asks for
a constant pressure or a constant flow, so a profile that ramps on purpose is
not mistaken for an unstable puck. `flow_divergence` and `early_drops` depend on
the scale and are skipped when the scale warnings fire.

**Why −0.45 per second.** The trend distribution sits just below zero - median
−0.11, p90 +0.05 - because a bed compacts slightly during any pour, so mere
settling must not count. Across the 142 shots with a measurable trend, −0.45 is
p13 and the tail steepens quickly beneath it (p15 −0.39, p20 −0.32), which puts
the cut where ordinary settling stops and a bed opening up begins. It is
deliberately no tighter: of the three worst-rated shots the two steepest
(−1.06, −1.92) fire and the third (−0.25) does not. The indicator reports a bed
losing resistance, not a bad shot, and the percentages above are the count of
shots where that happened - not a claim about how many tasted wrong.

The "fires on" column counts shots where the indicator could be **computed**.
For `resistance_trend` that is 18 of 142, not of 171: a shot without a settled
pour has no trend, and counting it as passing would be the same mistake as
reading a knocked scale as good news.

**Why five rather than the four originally specified.** Measured against the
operator's own notes across 165 shots, the four fire on 4 of the 10 worst-rated
shots - and also on one rated 100. The resistance trend separates them: the
three lowest-rated shots all combine a high resistance with a steep decline
(7.4/−0.25, 18.3/−1.06, 32.7/−1.92), and the two the operator labelled
"channeling" himself sit at the opposite extreme, at 0.6 and 1.7 against a
median of 3.9. The four remain - they are physically sound and will matter on
data where those failure modes occur - but leaving out the one signal that
actually discriminates would make the aggregate worse than its parts.

**Aggregation:** two or more fired is `high`, one is `elevated`, none is `low`.
Measured across the archive: 94 `low`, 49 `elevated`, **0 `high`**, 28 not
judged.

That `high` never occurs is worth stating rather than hiding. The 49 elevated
shots fire exactly one indicator each and **no two indicators ever coincide** -
which is evidence that they are catching genuinely different things rather than
correlated noise. Requiring two independent signatures is the right definition
of a strong case; that this archive contains none means the setup is sound, not
that the band is wrong.

### 8.8 Profile compliance

What the machine was asked for against what it did, per data point. This is the
one thing this data allows that a pressure-only log does not: the target sits
next to the reading.

Each channel is judged **only where it is the one being held**. A
pressure-controlled stretch leaves the flow target at 0 and vice versa; judging
both everywhere produced a mean flow deviation of 1.7 ml/s across the archive,
which is not a deviation but the absence of a target.

| Channel | `close` | `loose` | `off` |
|---|---|---|---|
| pressure | < 0.25 bar | < 0.50 bar | ≥ 0.50 bar |
| flow | < 0.30 ml/s | < 0.70 ml/s | ≥ 0.70 ml/s |

Measured: pressure p50 = 0.09 bar, flow p50 = 0.26 ml/s. The pressure figure is
reported to three decimals - rounding 0.028 to 0.0 would erase the scale the
band sits on.

**Temperature is judged throughout**, not only where a channel is held: the
group is meant to hold its target whatever the pressure is doing. Bands follow
the machine's specification (±1 °C) and the point at which a trained taster
reliably notices (2 °C): `on_target` below 1 °C, `off_target` below 2 °C,
`notable` beyond. `direction` says which way.

Phases follow the machine's own `profile_frame`. 160 of 165 shots run through
two to ten frames; the five that report a single one still get their aggregate
compliance.

### 8.9 Attribution

The physics - resistance as `P/F²`, the idea of independent channeling
signatures, temperature bands anchored on the machine specification and on
tasting thresholds, and the three-level detail system - is informed by
[gaggimate-mcp](https://github.com/julianleopold/gaggimate-mcp) (MIT), and in
particular its threshold calibration document. **No code was copied**, so
nothing of theirs is redistributed here and no MIT notice has to travel with
this. Were code taken later, MIT permits its use inside a GPL-3.0-or-later work
provided the MIT notice and copyright line are kept with it - the combination
then ships under the GPL, which is the licence of this project (§17).

The numbers are not theirs. Their sampling is 100 ms where this is ~250 ms, and
their data carries no per-point targets, so every threshold above was read off
this archive's own distribution. Where a threshold sits at a percentile, that
percentile is the justification.

## 9. The MCP interface

Server name `decentespresso`, streamable HTTP under the secret path. Every tool
is read-only except `sync_now` and the write tools from §11.

| Tool | Returns |
|---|---|
| `list_beans()` | beans with shot count, date range, grind settings |
| `list_shots(bean?, roaster?, profile?, since?, until?, limit, cursor?)` | compact rows, newest first, with `next_cursor` |
| `get_shot(id \| "latest", bean?, include_curve, max_points)` | metadata, metrics, curve shape, profile summary |
| `get_shot_metrics(id)` | the metrics alone |
| `compare_shots(ids[2..4], include_profile, include_curves)` | side by side with deltas to the first and a divergence note |
| `list_profiles(on_tablet?)` | profiles the shots ran on, with versions; `on_tablet` lists the tablet's records live, with the ids the profile tools take |
| `get_profile(shot_id? \| name? \| version_hash?)` | the complete targets |
| `get_workflow()` | the setting for the next shot, live from the tablet |
| `sync_now()` | an immediate sync, idempotent |
| `status()` | archive contents, last sync, Decaid state, warnings |
| `audit_archive(since?, rule?, limit?)` | guard findings with the thresholds in force |
| `list_batches(bean?)` | batches with roast date and usage - where batch identifiers come from |
| `get_batch(id)` | one batch in full: all dates, freezer state, weights |
| `stats(period, compare_previous)` | what was pulled in a period, and how it tasted |
| `list_recipes(source?)` | saved recipes - mine, DYE2's, or both - in one shape |

Filters are case-insensitive substrings. Dates take ISO8601 or a relative
shorthand (`12h`, `7d`, `2w`, `1m`, `1y`).

Errors are structured tool errors with a code: `shot_not_found`,
`waiting_for_tablet`, `invalid_argument`, `decaid_rejected`.

### 9.1 Detail levels

`get_shot`, `get_shot_metrics` and `compare_shots` take a `detail` parameter.
One cached metrics row serves all three - computing the diagnostics is cheap
next to fetching the series, so they are always computed and only the answer is
trimmed. Defaults are unchanged, so existing calls behave as before.

| Level | Carries | Measured |
|---|---|---|
| `summary` (default) | every scalar metric, the curve shape, the resistance band, the channeling risk, the temperature verdict | 2.3 kB |
| `per_phase` | plus the raw value behind every indicator and the compliance per phase | 3.9 kB |
| `detailed` | plus the point arrays | 6.0 kB |

Triage, then locate a cause, then look at the shape. Each step roughly doubles,
which is the point: the cost of looking closer should be visible.

`compare_shots` drops the per-phase table at every level. Four phase tables side
by side is not what a comparison is for, and it is what pushed
`compare_shots(4, per_phase)` to 19 kB, well past the budget. With it gone the
worst case - four shots, `detailed`, curves attached - is 14.7 kB.

### 9.2 Response economy

Every turn of a conversation reprocesses the whole context so far. Two things
drive it: responses that deliver more than the question needs, and questions
that cost several calls.

**The budget is ~15 kB per response.** Curves are always downsampled.

**Shape before numbers.** `curve_shape` (§8.4) comes along always; the point
arrays only on request (`include_curve`, `include_curves`), at roughly ten times
the size. They come as parallel lists (`t`, `p`, `fi`, `fo`, `w`, `tb`) rather
than a list of objects - about 49 % smaller than objects with the same short
keys, 70 % smaller than one with spelled-out names.

**Downsampling** is even over *time*, not over the index; with uneven sampling a
densely sampled stretch would otherwise dominate. Guaranteed to survive: the
first point, the last point, and both pressure maxima. These mandatory points
outrank `max_points` - otherwise a small budget could cut away the peak that is
described as guaranteed.

`compare_shots` distributes a **total** budget of 100 points across the compared
shots (at least 20 each). A flat 60 per shot put four shots with curves at
18.2 kB, over budget; split, the worst case is 13.4 kB.

**Semantics live in the server `INSTRUCTIONS`**, which are in context once,
rather than repeated in every docstring. The tool docstrings name the purpose
and the parameters and point there. A test bounds the total size of the tool
definitions and checks that the glossary does not migrate back into them.

The telling figure is the size **per tool**, not the sum: more capability
necessarily costs more, verbosity does not.

### 9.3 Measurement per call

`telemetry.py` logs `tool`, `dur_ms` and `bytes` for every call - and **no**
parameter values, no URL, no path. Arguments are the likeliest route by which
something confidential eventually reaches a log line.

### 9.4 Batch master data on the read paths

Batches were synced and writable long before anything gave them out. The roast
date of the batch in the hopper sat in the database, correct, and no read path
exposed it - so bean age, one of the few variables a person actually turns while
dialling in, could not be asked about, and `update_batch` was close to
unusable: identifiers could only be guessed out of a shot, and there was no way
to see the current state before overwriting it.

- `get_shot` carries a `bean_batch` block: `roast_date`, `days_off_roast`,
  `frozen`, `freeze_date`, `unfreeze_date`, `buy_date`, `open_date`. The age is
  measured against that shot (§10.2).
- `list_beans` carries `batches` per bean - identifier, roast date, frozen
  state, shot count, date range.
- `list_batches(bean?)` and `get_batch(id)` are the lookup path.
- A bean carries `origin` (`country`, `region`, `producer`, `decaf_process`,
  `variety`, `altitude`) where anything is recorded, and no `origin` key at all
  where nothing is. Unset is absent, never a placeholder (T31, T32).
- `get_batch` adds the provenance a filled-in batch carries: roast level,
  harvest season, cupping score, price with its currency, the batch note, and
  the `stock` block described in §9.5. `price` is reported as amount and
  currency together or not at all - a bare number invites being read as euros.

### 9.5 Statistics

`stats(period, compare_previous)` answers what was pulled in a period, from
what, and how it tasted. Deliberately lean: shot counts overall and per day, the
top beans and profiles by count and mean rating, the averages for dose, yield,
ratio and duration, how the grind moved per bean, and what each batch was used
for. `compare_previous` adds the same figures for the period of equal length
before it, plus the deltas.

`period` takes an ISO date ("since then") or a shorthand: `12h`, `7d`, `4w`,
`1m`, `1y`.

**What counts as a shot.** Excluding cleaning profiles by name is not enough:
across 171 shots, 22 produced under 5 g, and only 12 of those carry a
maintenance name (`Cleaning/Forward Flush x5`, `Test/temperature calibration`).
The other 10 ran under `Default` or `D-Flow` and are aborts. So there are two
criteria - the profile name must not match `clean`, `flush`, `rinse`, `descal`,
`calibrat` or `purge`, and the shot must have produced at least 5 g. A shot with
no recorded yield still counts: the scale may simply have been off, and dropping
those would shrink the archive quietly.

`busiest_day` is the exception and counts everything, maintenance included,
reporting the split. A day with thirty flushes was still a day at the machine,
and hiding that would make the number confusing rather than clean.

**Grind settings are free text** typed on a tablet, and the archive holds "2.7",
"2.70" and "2,8" for one grinder. They are reported as written, but two
spellings of the same number are not a change - comparing the strings reported
13 changes on a bean that was moved 11 times.

**Remaining stock is derived, not read.** Decaid's `weightRemaining` is
initialised to the bag weight and never counted down (T34): the reference batch
reads 500 g left after 27 shots that consumed 486 g. So `get_batch` reports the
bag weight, what the recorded doses actually used, and the difference - and puts
Decaid's own figure beside it rather than instead of it, with a note when the
two disagree by more than a dose. An estimate presented as a reading would be
the worse error. The estimate misses anything ground and thrown away; it is
labelled as an estimate for that reason.

**Top lists carry their rating coverage.** A mean over one rating out of twenty
is not the same claim as one over ten, so `rated` sits next to `shots`.


## 10. Guards and audit

Four rules in `guards.py`, pure functions over rows - no network, no database,
and the clock is handed in. Each can be switched off through `GUARD_RULES`; a
rule that fires too often gets ignored wholesale and takes the others with it.

| Rule | Checks |
|---|---|
| `grind_not_adjusted` | batch changed without adjusting the grind |
| `bean_age` | bean age when pulled, frozen time subtracted |
| `missing_rating` | no rating once the grace period passed |
| `dose_outlier` | weights against the workflow target |

### 10.1 The dose is not checkable

The obvious rule - actual dose against target dose - cannot exist here. In all
165 cases with both values, `actualDoseWeight` is **exactly** equal to
`targetDoseWeight`: the DE1 does not weigh the dose, it adopts the target.

The scatter sits in the **yield**, where the scale really measures: 8.9 g off
target on average, 497 g in the extreme. The rule therefore checks the yield
against its target and the dose for plausibility only - a dose of 0 g means the
scale was not connected.

### 10.2 Bean age, and when it is only a bound

Frozen time does not count as ageing, so it is subtracted. Decaid keeps an
`unfreezeDate` (T23) and where it is filled the subtraction is exact. Where it
is not - `frozen` false, a `freezeDate` set, no thaw date - the batch was thawed
at some point and nobody knows when. The finding then carries `certain: false`
and the age is explicitly an **upper bound**. A number that cannot be
substantiated is not reported as certain.

**Without a roast date the rule sits out.** It never falls back to the purchase
date, the open date or zero: those are different facts, and an age derived from
one of them would be indistinguishable downstream from a measured one.
`audit_archive` reports the count under `not_checked`, because a rule that
skips silently looks exactly like a rule that found nothing.

The age is always measured against the **shot**, never against the clock. An
age taken at query time would keep growing after the fact, so the same shot
would answer differently every week and a comparison across a month would
quietly compare two different questions.

### 10.3 Two bounds, not one deadline

`missing_rating` reports only between `RATING_GRACE_HOURS` (36 h - before that
one drinks it first) and seven days. Without an upper bound the rule reported
140 of 169 shots: 83 % of the archive, because only about one shot in six gets
rated.

### 10.4 Notification

ntfy, with three caps that all serve the same purpose - that the messages keep
being read: at most one message per shot even when four rules fire; only shots
from the last 48 hours; at most five messages per run, newest first. Measured
against the archive: `audit_archive` shows 63 findings, ntfy would report 2.

**No content in findings.** A finding names the identifier, the rule and the
numbers it rests on - never the text of a note or a bean name. Findings leave
the house over ntfy.

**Silence is not a failure.** If ntfy is down it gets logged and the sync
carries on; only delivered messages count as reported.

## 11. Writing

Thirteen tools, each behind `WRITE_ENABLED` and each with its own whitelist. When
the switch is off the tools are **not registered** - they do not appear in the
tool list and do not refuse. A tool that exists and refuses invites asking
again; one that does not exist does not.

| Tool | Fields |
|---|---|
| `update_shot` | `espressoNotes`, `enjoyment` (tablet's scale, §5.4), `actualDoseWeight`, `actualYield` |
| `update_bean`, `create_bean` | T21 and T31; `name` and `roaster` required to create |
| `update_batch`, `create_batch` | T22 |
| `set_workflow` | `grinderSetting`, `grinderModel`, `targetDoseWeight`, `targetYield`, `beanBatchId`, `profileId` |
| `clone_profile`, `update_profile` | the three overrides of §11.5, nothing else |
| `save_workflow_profile`, `save_recipe`, `apply_recipe`, `delete_recipe` | §11.6 - one recipe per bean, projected to the tablet |

The docstrings do not repeat the field lists: they drifted - `update_batch` went
on telling every request that Decaid keeps no thaw date for ten days after T23
had been corrected everywhere else. A wrong field is refused with the full list
of allowed ones, and that list is generated from the whitelist itself.

**A field goes on a whitelist only after it was written against the real API and
read back.** Better one field too few than one that gets discarded silently.

**A profile is never taken from the caller as an object.** Selection goes by
reference (`profileId`), change goes through the overrides of §11.5. An object
would be whatever the caller typed, and Decaid runs whatever it is given.

**Nothing is ever deleted.** The API can do it; this does not build it.

### 11.1 Write-through

The write goes to Decaid first, then everything is read back fresh, upserted
locally and the metrics cache refilled - a dose change changes the ratio. The
response names `before` and `after` per field **from that read-back**, not from
the assumption of what was sent. A field under `unchanged` was not taken.

The comparison covers the whole entity, not only the fields that were sent, and
anything that moved without being asked for comes back under `alongside`. Only
`createdAt` and `updatedAt` are left out, because Decaid maintains those on
every write (T15). A write that quietly changes something else is precisely what
a read-back exists to catch.

### 11.2 A batch change carries its labels

Decaid keeps `beanBatchId` and the display strings `coffeeName` /
`coffeeRoaster` side by side in the workflow context and derives neither from
the other (T28). Setting the batch alone leaves the machine showing the previous
coffee's name - reproduced on the live instance, and the reason `set_workflow`
resolves the batch to its bean and writes all three fields together. Decaid's
own API examples do exactly the same, so this is not a workaround but the
intended client behaviour.

Consequences that follow from it:

- `coffeeName` and `coffeeRoaster` are **blocked** as caller-supplied fields.
  Setting them by hand is how the machine ends up showing one coffee while
  pulling another; they are ours to derive, not the caller's to choose.
- **An unresolvable batch aborts the write.** Decaid accepts any string as a
  `beanBatchId` without checking it (T29), so this refusal is the only thing
  between a typo and a workflow pointing at nothing.
- Clearing the batch leaves the labels standing. They are then the only record
  of what is in the hopper, and deleting that would be a loss, not a cleanup.

### 11.3 Validation

Before the first API call, with every violation collected. The API validates no
value ranges at all, so this is the only protection against a typo reaching the
archive. Weights are bounded, ratings follow the tablet's scale (§5.4), dates are ISO and
not in the future, free text is capped.

Decimal commas are accepted (`"18,5"` → `18.5`). For roast dates the error
message says that the DE1 app writes `DD.MM.YYYY` while ISO is expected here -
which does produce mixed formats in the archive. Accepted deliberately: a
machine-readable format is worth more than uniformity with an ambiguous one.

### 11.4 Logging

One line per write with the target, the **field names** and the duration - no
values. Notes can hold private things and a log is the wrong place for them.

### 11.5 Coffee onboarding and the profile forge

A new bag, set up for the machine, in four steps the model runs one at a time
with the user's confirmation between them: `create_bean` → `create_batch` →
`clone_profile` → `set_workflow`. There is deliberately no single tool doing
all four. A combined call either succeeds whole or leaves the user to work out
which part did not; four confirmed steps make each change visible as it
happens, and a failure stops the flow at the step that failed. The order and the
rule "stop, never skip" are in `INSTRUCTIONS`.

**Favourites are per-coffee profiles plus the workflow.** Decaid's own
favourites store (the DYE2 key-value store) is not written from here.

**Creating is never retried.** Reads and updates are retried on 5xx and network
errors; a `POST` is sent exactly once. A request the tablet acted on before it
failed to answer would otherwise create a second bean, and the message on such
a failure says the thing may exist anyway.

**A duplicate bean is refused before sending.** Same name and roaster, compared
case- and whitespace-insensitively, is the same coffee; the refusal names the
existing one. Verified live: "probe-m10" / "PROBE-M10  Roaster" was caught
against "PROBE-M10" / "PROBE-M10 Roaster".

**What a clone may change** - three overrides, each verified by writing it and
reading it back on the live instance, and nothing else:

| Override | Sets | Limits |
|---|---|---|
| `temperature_c` | the main step to this value, every other step shifted by the same amount | 80–96 °C for **every** resulting step |
| `target_weight_g` | stop-at-weight | 10–100 g |
| `main_setpoint` | pressure or flow of the main step | ≤ 10 bar, ≤ 8 ml/s |

The **main step** is the last step, provided it is also the longest - the pour,
in every profile where the question has an obvious answer. Where it has none
(Blooming Espresso ends on a 1 s reset step at zero flow) `main_setpoint` is
refused, and so is `temperature_c` on a temperature curve with no anchor.

The temperature is **shifted, not flattened**. A first version set every step to
one value and refused any profile whose steps differed. The live instance showed
why that was wrong: the operator's D-Flow fills at 88.5 and pours at 88.0 -
D-Flow's editor has separate fill and pour temperatures - and the rule would
have refused the very profile the acceptance was built on. Shifted, a clone at
91.5 °C pours at 91.5 and fills at 92.0, and the half degree survives. Because
every shifted step is a value written here, every one is bounded: Adaptive v3 at
92 °C would fill at 97 and is refused.

**A copy must brew differently.** Decaid's id is the content (T37). A copy that
differs only in title is the original to Decaid, and it answers the `POST` with
201 and the original's record (T39) - so a naive clone reports success for
something that never happened. Refused before sending, as is a copy whose
content matches any existing record (that one is named instead).

**Titles.** Default `"<roaster> – <bean>"` with an en dash, taken from `bean_id`.
A taken title - compared case-insensitively against every profile not deleted -
is refused with a free one suggested. Sixty characters at most: Decaid took 300
without complaint, so the limit is readability on the tablet, not an API rule.

**By title means the visible one.** Every edit on the tablet leaves the
previous version behind as a hidden record with the same title; the live
instance holds four "D-Flow"s. A title resolves to the visible one; an id finds
any record that is not deleted.

**What `update_profile` will not touch.** A bundled default, ever - Decaid
refuses that itself (400, verified), and this refuses it earlier with a reason.
A profile not made by `clone_profile` (recognised by a marker in its metadata,
which survives the id change of T37) only with `allow_foreign`, set when the
user named that profile. The id changes with the content; the response names
the new one and says whether the workflow is still running the old version - it
holds a copy (T38), so it does not follow.

**Selecting a profile.** `set_workflow` takes `profileId` (an id or exact
title), resolves it against the tablet and copies the profile in. Unknown
references are refused; Decaid would take any object at all. A profile change
overwrites the workflow without asking. Until M13 a profile that existed only
in the workflow - a tablet tune - was protected and needed
`replace_unsaved_profile`; that made every profile change a two-step
conversation, and the tune now lives on in the bean's recipe instead (§11.6).
`get_workflow` says whether the running profile is stored (`profile_stored`),
for information only.

**Visible on the tablet** is what the API says (`visibility: visible`). Whether
the tablet's UI shows it in the list it shows is not something the API can
answer; the acceptance run confirmed the flag, not the pixels.

### 11.7 A profile change is confirmed at the machine

A workflow write that changes the profile is not finished when Decaid
answers: the profile still has to reach the DE1 over Bluetooth (T47). A shot
started in between runs the old profile, and its record still names the new
one, because Decaid stores the workflow of the moment the shot ends (T49).

So `set_workflow` and `apply_recipe` watch for it whenever the profile they
send differs from the running one - a new title included, since Decaid
re-uploads on any change:

- **Before the PUT** the DE1's connection is read from `/api/v1/devices`, and
  the tablet's clock from the newest log line. With the DE1 not connected
  nothing is waited for: `pending_connection`, Decaid uploads on connect.
- **After it** the log is read every 0.5 s, 6 s at most. The first
  `mmr write: tankTemp` after this server's `PUT /api/v1/workflow` line is
  `confirmed`, with the seconds between; a `setProfile failed` WARNING is
  `failed_retrying`. Neither in time: `unconfirmed`.

The answer carries it as `machine`, with a note for anything but `confirmed`
that says not to start the shot until the tablet shows the new curve. A second
client uploading in the same second cannot be told apart in the log; the
first upload after our PUT is taken. A log at WARNING level, or none, leaves
every change `unconfirmed` - the safe side.

### 11.6 Recipes and favourites

**One recipe per bean, derived.** A recipe is where a bean was last dialled in:
batch, grind, dose, target yield and the profile as the workflow ran it,
embedded whole (migration 006, table `bean_recipes`). It is named
"<roaster> – <bean>" and keyed by the bean, so a new batch of the same bean
moves it along. There is no name to choose, nothing to pin, no history and no
version - and no stored profile copy: a profile tuned on the tablet lives in
the recipe itself.

**Every write updates it.** After each successful `set_workflow`,
`apply_recipe`, `clone_profile`, `update_profile` and `save_workflow_profile`
the workflow is read and taken as the active bean's recipe. The active bean is
the workflow's batch - or the newest shot's, because Streamline clears the
batch from the workflow after every stored shot (T50). `save_recipe` takes no
parameters and does the same on demand, for a dial-in done on the tablet.
Updating is never allowed to fail the write it follows.

**Applying** PUTs context and the embedded profile through the one workflow
path, with the batch labels (T28) and the upload watch (§11.7).

**Projected into DYE2's list, so the tablet has it with one tap.** Each recipe
is written into `dye2.reaplugin/recipes` as an item marked `origin:
"decentespresso-mcp"` with `recipeId` (the bean), and a complete ready-to-PUT
`workflow` - context with batch, labels, grind, dose and
yield, and the full profile, never a stub. That is what Streamline's strip
applies (T51). `profileId`/`profileTitle` are left out on purpose: DYE2's own
dashboard would PUT them as a stub and rename the running profile (T52).

This writes a key DYE2's contract reserves for DYE2. The rules that keep it
safe: items without the marker are never touched and keep their place - ours
are appended, because the strip shows only the first five; the key is read
immediately before every write and read back after it; nothing is written when
nothing changed. DYE2's editor keeps foreign items (T53). The store has no
ETag, so an edit in DYE2 in the same instant can still be lost - a window one
request wide, the same Streamline's auto-save already lives with. A documented
second key is proposed upstream (`docs/upstream/07`). `DYE2_PROJECTION=false`
switches it off; the recipes themselves stay.

**DYE2's five slots.** DYE2's editor knows the ids "1"-"5" only, and
Streamline's long press opens a slot by that number (T54). Our items take the
slots DYE2's own items leave free, most recently dialled-in beans first; a bean
keeps its slot, and when they are full the bean pulled longest ago gives way -
it stays a recipe here, just not on the tablet. The strip shows five recipes
anyway, so the slots are no narrower than the screen. Saved over in DYE2's
editor under another name, a slot becomes the user's: DYE2 merges its save
into our item and keeps the marker, so the marker is removed and nothing else
changed. A dial-in there keeps the name and stays ours. Switching
"Show on Streamline Dashboard" off in DYE2 stands.

**The projection is a derivation.** Every sync repairs it: missing or outdated
entries are written again and counted in `status()` (`projection_heals`), and
the recipe of a bean that no longer exists goes, with its entry. Changes made
on the tablet to our entries are overwritten - with one exception, the dial-in:
Streamline's auto-save writes a dashboard edit of dose, drink or grind into the
applied recipe's `dashboardVariables` (T51). A difference there from what was
last projected is taken into the recipe before re-projecting, so a dial-in on
the tablet needs no step in the chat - and, unlike on DYE2's own recipes,
reaches the machine the next time the entry is tapped.

**DYE2's own recipes** are listed in the contract's shape, with its fallbacks
(`title` → `name` → "Recipe <id>"), and applied the way the contract says:
their `workflow`, PUT as it is. One from an older DYE2 without that field is
listed and not applied. Our projected items are not listed among them.

**A name in both sources is not guessed between.** Listing shows both; applying
needs `source`.

**Favourites** are these recipes plus the per-coffee profiles of §11.5, and
DYE2's own `autoFavourites`, listed and applied as source `dye2_favs` - read
only, like its recipes. What applying one sends:

- **The context, with the copyMask respected.** A group is on unless it is
  explicitly false (the contract's "absent => on"); the mapping from group to
  fields is DYE2's own, from `buildFavouriteWorkflow`. DYE2 already applies the
  mask when it builds `workflow`, so doing it again is a no-op there and keeps
  an item honest whose mask changed afterwards. A favourite without `workflow`
  is derived from snapshot and mask - the contract's legacy path.
- **The profile only where there is one** (T44). A full profile (auto entries)
  is sent as it is. A stub with an id is
  resolved and the full profile sent, since Decaid would neither resolve nor
  keep the id. A stub without an id is not sent: "favourite carries a
  name-only profile; keeping the current profile untouched". This is where
  DYE2's own apply stumbles - its builder writes the stub and its dashboard
  PUTs it.
- **Through the guarded path.** The batch brings its labels (T28); a bean id in
  the batch's place (T45) is refused with the bean's batches named.

Names follow the contract's fallbacks (title, subtitle, a derived "roaster ·
coffee"); five live favourites derive the same label, so DYE2's items can also
be named by id.

**Issues are listed before anything is applied.** Each entry of `list_recipes`
carries `issues` when something is wrong with it, and no key when nothing is -
so a broken favourite is recognisable in the listing, not only in the refusal
or the read-back of `apply_recipe`. The rules are pure (`recipes.issues_of`)
and judge what the entry would apply against one catalogue of batches, beans
and profiles, fetched once per listing:

| issue | meaning | source of the finding |
|---|---|---|
| `name_only_profile` | profile is `{id: null, title}`; applying keeps the current one | T44 |
| `profile_reference_unresolved` | a profile id no stored record has | T44 |
| `profile_missing` | an unpinned own recipe whose title no longer resolves | §11.6 above |
| `no_workflow` | a DYE2 recipe from before `workflow`; listed, not applied | contract |
| `batch_is_bean_id` | the batch field holds a bean id; apply refuses it | T45 |
| `batch_unknown` | the batch id is neither a batch nor a bean | T29 |
| `labels_without_batch` | coffee name or roaster set, no batch | T42 |
| `labels_mismatch_batch` | labels differ from the named batch's bean | T28 |
| `duplicate` | same source, same name, same values; `duplicate_of` lists the others | measured |
| `name_not_unique` | same source and name, different values; apply by id | measured |

"Same values" means the context and profile that applying would send - the
payload, not the stored item, whose ids and creation times always differ. That
line matters on the live store: of the five Yirga Santos favourites four are
identical, the fifth carries a profile reference instead of a name and no rpm.
Calling all five duplicates would invite deleting the one that differs. Own
recipes are not compared: their names are unique by constraint. With the
tablet off only what an entry shows by itself is judged (`name_only_profile`,
`no_workflow`); whether a batch exists cannot be told unseen.

Measured read-only against the live instance on 2026-09-26: the recipe "Decaf"
`labels_without_batch`; seven favourites `name_only_profile`; RT Decaf and all
five Yirga Santos `batch_is_bean_id`; four `duplicate`, one `name_not_unique`;
no `labels_mismatch_batch`, no unresolved reference.

**What the tablet cannot see.** Recipes saved here do not appear on the tablet:
the only way to put them there would be to write DYE2's key. A documented path
for a second writer is proposed upstream (`docs/upstream/07`).

## 12. Security

**Threat model:** the endpoint is publicly reachable, because Claude calls it
from Anthropic's infrastructure - restricting it to the home network does not
work. The shot data is uncritical; the path secret and the ntfy token are not.

1. **A secret path instead of auth.** `https://<host>/<MCP_PATH_SECRET>/mcp`,
   32+ random characters. Every other path returns 404 without a body.
   `/healthz` is the exception: it sits on the same app and returns nothing but
   `ok` - no counts, no version, no hint at the secret path. To close it, add a
   `path: ^/healthz$` rule to the tunnel ingress before the catch-all.
2. **No Cloudflare Access in front** - it would block Claude from connecting.
   Rate limiting and the standard WAF rules instead.
3. **Secrets from the environment only.** Two exist: the path secret and the
   ntfy token. The log formatter redacts both, and a separate expression masks
   any `Authorization` header regardless of content. The redaction sits in the
   formatter rather than a filter, so it also catches tracebacks and
   third-party output. uvicorn access logs are off - they would record the
   secret path of every request.
4. **Container hardening:** non-root (UID 10001), `read_only: true`, only
   `/data` and `/tmp` writable, `no-new-privileges`, no published host ports.
5. **Decaid is local only.** `DECAID_URL` accepts private IP literals and
   nothing else - a hostname could be repointed later without the configuration
   changing. That traffic must never go through the tunnel.

## 13. Deployment

Two ways, same image.

**A - compose on the host.** `compose.yaml` builds locally, binds `./data`
(which must belong to UID 10001) and attaches to the existing `cloudflared`
network without publishing a port. The container also needs a route onto the
local network to reach Decaid.

**B - Portainer with GHCR.** GitHub Actions runs `ruff` and `pytest` as a gate,
then builds and pushes to `ghcr.io/<owner>/<repo>` with the tags `latest` and
`sha-<commit>`. `compose.portainer.yaml` differs in three points: the image
comes from the registry, the data lives in a named volume (which Docker creates
with the image's ownership, so no manual `chown`), and the configuration comes
from Portainer stack variables.

A smoke step runs inside the built image and checks what cannot be checked
locally: that the package imports from site-packages, that its metadata survived
the build, that `BUILD_REF` was passed through, and that a profile parses there
the way it does here.

### 13.1 Which build is running

| Field | Origin |
|---|---|
| `version` | package metadata from `pyproject.toml` (`importlib.metadata`) |
| `build_ref` | commit SHA, baked in at build time |

One source for the version, not two - the second is the one that gets
forgotten. This was not always so: a deployment once reported a version and a
milestone that were both maintained by hand and both stale, and whether the old
image was running or only the fields lagged could not be told apart.

If `build_ref` does not match the expected commit, the registry image was not
re-pulled. If it is `null`, the server is not running from a built image.

**The version in this document and in `pyproject.toml` must agree.** A test
enforces it. That is the whole convention: no milestone counter, no second
place to forget.

## 14. Operation

**Backup:** `sqlite3 ./data/shots.db ".backup …"` as a nightly cron job, 7 days
of rotation.

**Updates:** deliberate, not automatic. `git pull && docker compose build && up
-d`, or a re-pull in Portainer.

**Logs:** key=value on stdout. Sync runs log a summary. `docket`, fastmcp's
task queue, is muted to WARNING: its worker starts unconditionally and
announces three built-in demo tasks that have nothing to do with this server.

| Symptom | Check |
|---|---|
| Claude cannot connect | URL exact including `/mcp`? `curl` returns 400/405 rather than 404? |
| Tools missing | connector enabled in the chat? reconnected after tool changes? |
| `waiting_for_tablet` | the tablet is off. Not an error |
| 502 from the hostname | same Docker network as cloudflared? service name right? |
| Empty archive | `status()` for the last sync, then `sync_now()` |

**Staleness guard:** `status()` warns when the last sync is more than 7 days
ago - that long a silence means shots are missing.

## 15. Tests and acceptance

The suite is the acceptance. A change is done when it is green, ruff is clean,
and nothing was committed halfway.

Fixtures are anonymised real responses, never invented ones. Numbers pinned in
tests come from measurement and the comment says where.

**Acceptance criteria:**

1. The backfill loads every shot including profile versions cleanly.
2. A new shot appears in `list_shots` within `SYNC_INTERVAL_MIN` + 1 min.
3. `get_shot("latest")` returns metadata, metrics, curve shape and profile in
   one response ≤ 15 kB.
4. A profile change at the machine produces a new version; the old shot stays
   linked to the old one.
5. The container survives a restart without data loss.
6. No secret in logs or tool output.

Criteria 2 and 5 need the real machine and Docker; the rest run automatically.

## 16. Backlog

- OAuth instead of the secret path.
- MCP prompts (`dial_in_check`, `bean_history`).
- Structured TDS/EY capture, if a refractometer appears.
- Importing further sources - the schema is prepared through `raw_json`.

## 17. Licence

**GPL-3.0-or-later**, full text in `LICENSE`, declared in `pyproject.toml` as an
SPDX expression so the built package carries it too.

Chosen to match the ecosystem: Decaid is GPL-3.0 and the Decent app store
requires its entries to be open source, so copyleft is what keeps this
consistent with the software it talks to and usable there. Before this the
repository carried no licence at all, which meant default copyright - nobody
could legally fork or contribute. Fixing that ahead of any outside contribution
is the point of the choice being made now rather than later.

The attribution question this raises is answered in §8.9: the gaggimate-mcp
influence is MIT-licensed inspiration, no code was copied, and MIT would in any
case permit the combination.
