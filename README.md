# decentespresso-mcp

An MCP server that archives espresso shots from a Decent DE1 locally (SQLite)
and makes them available to Claude for analysis through a custom connector.

Since M8 the source is **Decaid on the tablet at the machine**, reached over the
local network. No part of the archive chain runs through someone else's cloud
any more. The upload to [visualizer.coffee](https://visualizer.coffee) can carry
on as a community showcase but is no longer needed.

Full specification: [SPEC_decentespresso-mcp.md](SPEC_decentespresso-mcp.md).

> **Project status.** Personal project, built spec-driven with Claude Code;
> provided as-is, no support promised, issues welcome.
>
> Community project, not affiliated with Decent Espresso International.

> **Renamed.** The project was called `visualizer-mcp` until M8. The name now
> follows the **machine**, not the source: since M8 visualizer.coffee is neither
> the source nor the destination of the archive chain, so a name pointing at it
> would simply be wrong. `visualizer_client.py` keeps its filename — that module
> really does talk to Visualizer.

## Status: milestone M8 (source = Decaid, everything local)

In place:

- Configuration from the environment including startup validation (`config.py`)
- Structured key=value logging on stdout with secret redaction
  (`logging_setup.py`)
- FastMCP server (streamable HTTP) under `/<MCP_PATH_SECRET>/mcp`, `/healthz`,
  every other path `404` without a body (`server.py`)
- Decaid client on the local network with backoff; a tablet that is switched off
  is not an error state (`decaid_client.py`)
- Normalisation of the source data: everything in UTC, ratings without the
  phantom zeros of the import era (`decaid_mapping.py`)
- SQLite with a migration runner, upserts and sync state (`db.py`)
- Sync: the complete shot list, details only for what changed, background loop
  every `SYNC_INTERVAL_MIN` (`sync.py`)
- Profile versioning from the embedded workflow JSON (`decaid_profile.py`); the
  TCL parser stays readable for the archived era only
- Derived metrics per SPEC §8 with a cache in `shot_metrics` (`metrics.py`)
- Four guard rules over the archive with ntfy notification (`guards.py`,
  `notify.py`)
- Every tool from SPEC §9.2 and §20, plus `curve_shape` and the response economy
  from SPEC §17 (`server.py`, `telemetry.py`)
- Four write tools behind `WRITE_ENABLED` (SPEC §18, §20.5, `writes.py`)
- Hardening, acceptance tests and Portainer deployment (SPEC §16)

Not in place yet: the optional MCP prompts from SPEC §9.3 (`dial_in_check`,
`bean_history`). See SPEC §15.

## Tools

| Tool | Purpose |
|---|---|
| `list_beans()` | beans with shot count, date range, grind settings |
| `list_shots(bean?, roaster?, profile?, since?, until?, limit, cursor?)` | compact list, newest first |
| `get_shot(id\|"latest", bean?, include_curve, max_points)` | metadata + metrics + curve shape + profile summary |
| `get_shot_metrics(id)` | the metrics only |
| `compare_shots(ids[2..4], include_profile, include_curves)` | side by side with deltas to the first, profiles and a divergence note |
| `list_profiles()` | profiles with their versions |
| `get_profile(shot_id? \| name? \| version_hash?)` | the complete targets |
| `sync_now()` | immediate sync with the tablet, idempotent |
| `status()` | archive contents, last sync, Decaid state, warnings |
| `audit_archive(since?, rule?, limit?)` | guard findings together with the thresholds in force |
| `get_workflow()` | the setting for the next shot, live from the tablet |
| `update_shot(id, fields)` | note, rating, weights — **only with `WRITE_ENABLED=true`** |
| `update_bean(id, fields)` | master data of a bean — **only with `WRITE_ENABLED=true`** |
| `update_batch(id, fields)` | roast date, frozen state — **only with `WRITE_ENABLED=true`** |
| `set_workflow(fields)` | grind setting, targets, batch — **only with `WRITE_ENABLED=true`** |

Filters are case-insensitive substrings. `bean` matches the bean name *or* the
roastery, `roaster` only the roastery. `since`/`until` take ISO8601 or a
relative shorthand (`12h`, `7d`, `2w`, `1m`, `1y`).

### What the docstrings have to achieve

Claude gets the numbers without units and still has to read them correctly. The
glossary therefore appears twice: once in full in the server prompt
(`INSTRUCTIONS` in `server.py`, always in context) and once in shortened form in
every tool that returns the affected fields. What gets explained: units, the
semantics of `pi_end` together with `pi_end_source`, the difference between
`peak_pressure_infusion` and `max_pressure_global`, and the meaning of
`warnings` (the field is `null`, not 0).

Tools without those fields (`list_beans`, `status`) do not repeat the metric
explanation — there it would be ballast that every tool list drags along.

### Curve: `curve_shape` instead of raw numbers

Every shot brings a segment-by-segment description of its curve, derived from
the machine's phase markers — per segment the starting and ending value for
pressure and scale flow, the direction, and whether the curve is linear. On the
reference shot that is 579 B against roughly 2,900 B of point arrays, and
questions about rise, plateau or phase duration can be answered without a single
raw number.

The point arrays are still available, but only on request
(`include_curve=true` or `include_curves=true`). They come as parallel lists
(`t`, `p`, `fi`, `fo`, `w`, `tb`) — which saves about 49 % against a list of
objects using the same short keys, and about 70 % against one with spelled-out
column names.

### Response budget

SPEC §9 sets ~15 kB per response, SPEC §17 tightens the screws. Measured against
the **development snapshot of 2026-08-01** (14 shots) — both columns at the same
state, otherwise the comparison does not hold:

| Call | before M6 | after M6 |
|---|---:|---:|
| tool definitions (all 9, sent with every request) | 10.4 kB | 6.9 kB |
| `status()` | 0.7 kB | 0.7 kB |
| `list_shots(limit=10)` | 3.3 kB | 3.3 kB |
| `get_shot("latest")` | 5.0 kB | **2.1 kB** |
| `get_shot(id, include_curve=true)` | 4.6 kB | 4.0 kB |
| `compare_shots(2)` incl. profiles | 3 calls, 3.7 kB | **1 call, 4.2 kB** |

Tests pin these bounds — they should fire when something grows back.

Cross-measured at production size (snapshot of 2026-08-31, 69 shots, 3 beans):
`list_beans()` 0.9 kB, `list_shots(limit=10)` 3.7 kB, `status()` 0.7 kB, the
rest unchanged. Only `list_beans` grows with the archive (the number of
**distinct beans**, not of shots) along with `total_matching` in `list_shots` —
so the bounds hold in operation too.

M8 adds tools, and more capability necessarily costs more. The telling figure is
the size **per tool**: 1,155 B before M6, 764 B after M6, 840 B now for the
read-only set (9,241 B across 11 tools). With write mode on it is 808 B per tool
(12,123 B across 15) — leaner than the 845 B M7 needed with a single write tool,
because the behavioural rules moved into `INSTRUCTIONS`.

### Guards (SPEC §20.7)

Four rules check after every sync whether something does not add up. They are
pure functions over table rows - no network, no database, and the clock is
handed in. Each can be switched off on its own through `GUARD_RULES`.

| Rule | Checks |
|---|---|
| `grind_not_adjusted` | batch changed without adjusting the grind |
| `bean_age` | bean age when pulled, frozen time subtracted |
| `missing_rating` | rating not added once the grace period passed |
| `dose_outlier` | weights do not match the workflow target |

Two findings from the real archive shaped the rules:

- **The dose is not checkable.** In all 165 cases `actualDoseWeight` is exactly
  equal to `targetDoseWeight` - the DE1 does not weigh the dose, it adopts the
  target. The rule therefore checks the **yield** against its target and the
  dose for plausibility only (0 g means the scale was not connected).
- **An open-ended deadline would be worthless.** 145 of 169 shots are unrated;
  without an upper bound `missing_rating` reported 83 percent of the archive. It
  now reports only between 36 hours and seven days.

The ntfy notification has three caps so the messages keep being read: at most
one message per shot, only shots from the last 48 hours, at most five messages
per run. Measured against the archive, `audit_archive` shows 63 findings and
ntfy would report 2. A finding never names free text - the messages leave the
house.

### Writing (SPEC §18, §20.5)

**Off** by default. `WRITE_ENABLED=true` unlocks four additional tools:
`update_shot`, `update_bean`, `update_batch`, `set_workflow`. A field goes on a
whitelist only after it was written against the real API **and read back**. A
profile change is explicitly not possible - that belongs at the machine.

When the switch is off the tools do not appear in the tool list — they do not
refuse, they do not exist.

Only these fields are permitted; everything else is refused together with the
complete list:

| Tool | Fields |
|---|---|
| `update_shot` | `espressoNotes`, `enjoyment` (0–100), `actualDoseWeight`, `actualYield` |
| `update_bean` | `name`, `roaster`, `species`, `processing`, `notes`, `decaf` |
| `update_batch` | `roastDate`, `buyDate`, `freezeDate`, `frozen` |
| `set_workflow` | `grinderSetting`, `grinderModel`, `targetDoseWeight`, `targetYield`, `beanBatchId` |

Identifiers, timestamps, telemetry and the profile are blocked. Nothing is ever
deleted — the API can do it, this project does not build it.

**Write-through:** the write always goes to Decaid first, then everything is
read back fresh, upserted locally and the metrics cache refilled. The response
names `before` and `after` per field from that read-back — not from the
assumption of what was sent. If a field appears under `unchanged`, Decaid did
not take it.

Two peculiarities that shape the design (verified against Decaid 0.8.5 on
2026-09-14, every probe reversed immediately):

1. Decaid refuses `id` and `createdAt`/`updatedAt` with `400` — better than the
   silent discarding Visualizer practised. **The shot timestamp it does accept,
   though**: a `PUT` carrying `timestamp` came back `200` and the value really
   did stand afterwards. For the telemetry fields the block list is therefore
   the only safeguard.
2. The API checks **no** value ranges. The validation in `writes.py` is the only
   protection against a typo reaching the archive.

**No thaw date.** Decaid keeps no `unfreezeDate`. To thaw, set `frozen` to
false; bean age counts on from that point, and `audit_archive` can only bound it
from above for later shots (`certain: false`). Anyone who needs it exactly notes
the day in the bean notes.

For roast dates ISO (`YYYY-MM-DD`) is expected while the DE1 app writes
`DD.MM.YYYY` — both formats can therefore appear in the archive.

One log line per write with the shot id, the field names and the duration —
**without values**, because notes can hold private things.

### Which build is running?

`status()` answers that with three fields:

| Field | Origin |
|---|---|
| `version` | package metadata from `pyproject.toml` (`importlib.metadata`) |
| `milestone` | derived from the minor version: `0.7.x` -> `M7` |
| `build_ref` | commit SHA, baked in as `BUILD_REF` when the image is built |

The occasion was a misstep during the M7 deployment: `status()` reported version
`0.1.0` and `M6` while M7 code was running — both numbers were maintained by
hand and stale. Whether the old image was running or only the field lagged
behind could not be told apart.

The rule now: if `build_ref` does not match the expected commit, Portainer did
not re-pull (**Re-pull image** forgotten). If `build_ref` is `null`, the server
is not running from a built image.

**Bump the version at each milestone** — minor version = milestone number. A
test compares it against the highest milestone listed in SPEC §14 and fails when
the bump is missing.

### Measurement per call

`telemetry.py` logs `tool`, `dur_ms` and `bytes` for every tool call.
Deliberately **no** parameter values, no URL, no path: arguments are the
likeliest route by which something confidential eventually ends up in a log
line.

### Freshness check on `get_shot("latest")`

If the last sync is more than two minutes ago, the server syncs before
answering — a just-pulled shot is therefore there immediately. The outcome sits
in the `freshness` field. If the sync fails an answer still comes from the
archive, with a note: stale data beats none. The background loop, `sync_now` and
the freshness check share a lock so two runs never write at once.

### Metrics: phase markers and `pi_end`

Decaid reports the machine state in plain text: `state.substate` runs
`preparingForShot` -> `preinfusion` -> `pouring`. The end of preinfusion is
therefore **read off rather than inferred** - the moment the machine reports
`pouring`.

Three sources in this order; `pi_end_source` names the one actually used:

| Source | Where from | Quality |
|---|---|---|
| `substate` | the machine's state report | read off |
| `profile_frame` | last step boundary before the pressure anchor | inferred |
| `heuristic` | pressure first reaches `0.6 x max_pressure_global` | approximation |

The hierarchy is not a precaution. The de1app never recorded the machine state,
Decaid does - and the archive holds both:

| Origin | `substate` | `profile_frame` | heuristic |
|---|---|---|---|
| imported (88) | — | 84 | 3 |
| native (81) | 77 | 2 | 2 |

Without the second stage 84 shots would have a guessed `pi_end`. With
`heuristic` the value is an approximation and not fit for comparisons down to a
tenth of a second; the metric says so itself through `warnings`.

On the first data point `profileFrame` still holds the value of the preceding
shot - that change is discarded.

The square wave of the Visualizer era (`espresso_state_change`, jumping between
`-10000000` and `+10000000` on every change) is no longer read. It said *that*
something changed, not *to what*; a test on the old fixtures records that it is
ignored and that those shots run through the heuristic path.

### Metrics: plausibility of the scale

Three cases make derived values unusable, rather than reporting them silently
wrong (SPEC §8: a missing basis -> `null` plus `warnings`):

- **Scale not tared** — if it already shows more than 0.3 g in the first half
  second, that cannot be the shot (the machine preinfuses for seconds).
  `t_first_drops` becomes `null`.
- **Mean pour flow <= 0** — a coefficient of variation around a mean <= 0 would
  be negative and thus meaningless. `flow_stability` becomes `null`.
- **Weight goes negative** — the scale was knocked. The values stay but a
  warning points it out.

### Metrics cache

`shot_metrics` holds the result per shot as JSON together with the
`metrics_version` it was produced under. When a definition changes, the constant
`METRICS_VERSION` in `metrics.py` is bumped — the next start then recomputes
everything, without a migration and without a re-sync. Upserting a shot again
discards its cache as well, because the series, dose and yield may have changed.

### Profiles: `libtk8.6` is mandatory

SPEC §7.1 parses with `tkinter.Tcl()`. In `python:*-slim` `_tkinter` is compiled
in but the Tk runtime libraries are missing — even `import tkinter` fails there
with `ImportError: libtk8.6.so: cannot open shared object file`. The Dockerfile
therefore installs `libtk8.6`; remove the line and the smoke step in the build
workflow turns the build red.

As a second safeguard the import is soft: if the interpreter is unavailable,
`tcl_profile.py` carries on parsing with a list splitter of its own rather than
letting the service die. Both routes are checked against each other on **all**
fixtures, including the question of which inputs they reject; a test
additionally simulates the import failure.

This module is superseded as of M8 — Decaid ships the profile as JSON — but it
stays readable for the archived era.

### Profiles: what the parser does and does not do

Our own parser is the reference — the version hash hangs on the same TCL it
reads. Visualizer's `format=json` serves as a cross-check in the tests
(`tests/test_tcl_profile.py`); if one side diverges the test fails. Two
differences are known and documented there:

1. **Legacy profiles (`settings_2a`/`2b`) have no steps in the TCL.**
   `advanced_shot` is empty. Visualizer *synthesises* six steps for such
   profiles from the `flow_profile_*` and `preinfusion_*` settings. That is a
   reconstruction, not information from the file — we do not reproduce it. The
   targets sit as headline fields in `parsed_json`.
2. **Whitespace in the notes.** At one point Visualizer's JSON contains the
   character sequence `\n` plus eight spaces where the TCL has a single space.
   The cause lies in Visualizer's serializer.

`settings_2a` -> `pressure` and `settings_2c` -> `advanced` are verified against
real data; `settings_2b` -> `flow` follows the DE1 convention but no evidence
for it has turned up yet.

An unparseable profile aborts nothing: `raw_tcl` is stored regardless,
`parsed_json` gets `parse_ok: false` along with the error text, and title and
notes come from a tolerant line scan.

## Decaid API — verified state

Verified against the running instance on 2026-09-14, Decaid 0.8.5+2624. The
findings are tabulated as T1–T26 in SPEC §20.2; the ones that shape behaviour
are noted at the relevant constants in `decaid_client.py`.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/info` | version, commit, build time |
| `GET /api/v1/shots?limit=&offset=&order=` | everything except `measurements`, `updatedAt` included |
| `GET /api/v1/shots/ids` | all ids in one go, unpaginated |
| `GET /api/v1/shots/latest` | the full detail of the newest shot |
| `GET /api/v1/shots/<id>` | the full detail including `measurements` |
| `GET /api/v1/beans`, `GET /api/v1/bean-batches` | beans and batches, unpaginated |
| `GET /api/v1/workflow` | the setting for the next shot, including the profile |

What differs from what the brief assumed:

- The paths carry an `/api/v1` prefix; without it they are not reachable.
- `/api/v1/batches` does not exist; the path is `/api/v1/bean-batches`.
- The shot list **caps silently at 100** items.
- There is **no server-side time filter**; the incremental sync pages with a
  client-side `updatedAt` cursor.
- There is **no `time` field** in the measurements; the time axis comes from
  `machine.timestamp` minus the first data point.
- Protected fields are refused with **400** rather than discarded silently —
  better than the Visualizer behaviour, and it allows a clearer error message.
  The shot `timestamp` is the exception: it is accepted.
- There is **no `unfreezeDate`** on a batch.

## Visualizer API — verified state (historical)

> Since M8 Decaid is the source; this section describes the era before it. It
> remains because the upload to visualizer.coffee can carry on as a showcase and
> the fixtures for it are in the repo.

Checked against <https://apidocs.visualizer.coffee/> on 2026-08-01 (OpenAPI 3.1,
Visualizer API v1.15.0), additionally confirmed with real requests:

| Endpoint | Returns |
|---|---|
| `GET /api/me` | `{id, name, public, avatar_url}` — checks the credentials |
| `GET /api/shots?page=&items=` | **only** `{id, clock, updated_at}` per shot plus `paging{count,page,limit,pages}` |
| `GET /api/shots/{id}` | the full detail including `timeframe[]` and `data.espresso_*[]` |
| `GET /api/shots/{id}/profile` | raw TCL, `application/x-tcl` |
| `GET /api/shots/{id}/profile?format=json` | the profile as Visualizer parsed it |

What differs from what the spec assumed:

- **Time series come as strings**, not numbers — `timeframe` included.
- **`updated_after` (Unix seconds) + `sort=updated_at` exist.** The incremental
  sync used that instead of the page heuristic from SPEC §6.2 and thereby also
  found shots changed after the fact.
- **ETag/`If-None-Match` returns 304** on both list and detail.
- **Rate limits:** 50 req/min per IP, 200 req/10 min per IP and per user. The
  client keeps its distance with a sliding-window limiter (40/min, 170/10 min).
- **The dose is in the detail JSON** as `bean_weight` — the caveat in SPEC §7.2
  applies to the CSV route only.
- **`brewdata` is empty for DE1 uploads**; the profile needs a request of its
  own.
- Empty fields (`private_notes`, `metadata`) are absent from the response
  entirely.

The complete field mapping sits as a table in the docstring of
`shot_row_from_detail()` in
[visualizer_client.py](src/decentespresso_mcp/visualizer_client.py).

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"    # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check src tests
```

The fixtures under `tests/fixtures/` are real, anonymised API responses (account
identifiers replaced) — among them a reference shot with 184 data points, the
bean and batch lists, and three versions of the same Visualizer-era profile.

**`data/shots.db` is a development copy, not the archive.** It holds whatever a
sync could fetch at some point in time and drifts arbitrarily far from the
production instance afterwards. Numbers from that file are therefore never a
statement about the real archive; for that, ask `status()` against the
production instance.

Run locally (without Docker):

```bash
cp .env.example .env    # fill it in, generate MCP_PATH_SECRET
set -a; . ./.env; set +a
python -m decentespresso_mcp
```

## Deployment A: docker compose on the host

```bash
cp .env.example .env
openssl rand -hex 24            # -> MCP_PATH_SECRET
mkdir -p data && sudo chown -R 10001:10001 data   # the container runs as UID 10001
docker compose build && docker compose up -d
docker compose logs -f
```

`compose.yaml` attaches the container to the external network
`cloudflared_net` and deliberately publishes **no** host port. Adjust the
network name to the existing tunnel stack if needed.

The container also needs a route onto the local network to reach Decaid — the
host network or a macvlan, depending on the setup. That traffic must never go
through the tunnel (SPEC §20.6).

Add the Cloudflare tunnel ingress:

```yaml
ingress:
  - hostname: coffee-mcp.example.com
    service: http://decentespresso-mcp:8000
  # ...existing rules...
  - service: http_status:404
```

`/healthz` should not be reachable through the tunnel (SPEC §10.1) — the Docker
health check addresses the container directly.

## Deployment B: Portainer + GitHub Container Registry

See SPEC §16. The difference from A: the image is not built on the host but by
GitHub Actions, and Portainer pulls it from `ghcr.io`.

### One-time setup

1. **Push the repo to GitHub.** The workflow
   [.github/workflows/build-image.yaml](.github/workflows/build-image.yaml) runs
   on every push to `main`: first `ruff` and `pytest`, then build and push to
   `ghcr.io/<owner>/<repo>` with the tags `latest` and `sha-<commit>`. No secret
   of your own is needed — the runner brings `GITHUB_TOKEN`.

2. **If the repo is private**, so is the package. Either make the package public
   (GitHub → Packages → Package settings → Change visibility; the image contains
   no credentials, those arrive at runtime) or register the registry `ghcr.io`
   in Portainer under *Registries → Add registry → Custom* with your GitHub
   login and a PAT carrying `read:packages`.

3. **Create the stack in Portainer:** *Stacks → Add stack → Repository* (point
   it at [compose.portainer.yaml](compose.portainer.yaml)) or *Web editor* with
   the contents of that file.

4. **Set the stack variables** (Portainer, section *Environment variables*):

   | Variable | Example |
   |---|---|
   | `IMAGE_REPOSITORY` | `ghcr.io/<owner>/decentespresso-mcp` |
   | `IMAGE_TAG` | `latest` |
   | `DECAID_URL` | `http://10.100.100.171:8080` |
   | `MCP_PATH_SECRET` | `openssl rand -hex 24` |
   | `PUBLIC_BASE_URL` | `https://<hostname>` |
   | `CLOUDFLARED_NETWORK` | name of the existing tunnel network |
   | `SYNC_INTERVAL_MIN` | `15` (optional) |

5. **Deploy the stack.** On the first start Docker creates the named volume
   `decentespresso_mcp_data` — with the ownership from the image, so the manual
   `chown 10001` is not needed here.

#### Cutover from the old volume

Since the rename the volume is called `decentespresso_mcp_data` rather than
`visualizer_mcp_data`. That is **deliberate and coincides with the history reset
from M8**: the new schema cannot be applied to a file from the Visualizer era
anyway, `db.py` refuses it outright. A fresh volume is therefore not a loss but
the intended route.

One thing does have to come along: **`shots-visualizer-era.db`**, the backup of
the old archive. It is the only source for notes and ratings from before Decaid
and is what the migration script reads. So before throwing the old volume away:

```bash
# Get it out of the old volume
docker run --rm -v visualizer_mcp_data:/old -v "$PWD":/out alpine \
  cp /old/shots-visualizer-era.db /out/

# Put it into the new one (the stack may be running meanwhile)
docker run --rm -v decentespresso_mcp_data:/new -v "$PWD":/in alpine \
  sh -c 'cp /in/shots-visualizer-era.db /new/ && chown 10001:10001 /new/shots-visualizer-era.db'
```

The filename deliberately stays `shots-visualizer-era.db` — it names exactly
where the data came from. Only delete the old volume once the file sits in the
new one and the migration script has run.

### Updating

After a push to `main`, wait for the workflow and press *Stacks →
decentespresso-mcp → Update the stack* in Portainer with **Re-pull image**
ticked. Without the tick the old layer stays put, because the tag `latest` has
not changed.

Rolling back: set `IMAGE_TAG` to `sha-<commit>` of an older version and update
the stack.

### Connector URL

The URL contains the secret and is therefore **not** logged at startup. Fetch it
when needed:

```bash
docker compose exec decentespresso-mcp decentespresso-mcp --print-connector-url
```

That yields `https://<host>/<MCP_PATH_SECRET>/mcp` — enter this URL in claude.ai
under Settings → Connectors → "Add custom connector", leaving the OAuth fields
empty.

### Syncing by hand

The server syncs in the background on its own; the first run is automatically a
backfill. Manually:

```bash
docker compose exec decentespresso-mcp decentespresso-mcp --backfill    # every shot
docker compose exec decentespresso-mcp decentespresso-mcp --sync-once   # only new/changed
```

Both print a JSON summary and exit with code 1 when individual shots failed.

The summary separates `errors` from `warnings`:

- **`errors`** are transient problems (network, write failures). While any occur
  the backfill stays open — otherwise a failed shot would remain unfetched
  forever.
- **`warnings`** are deterministic findings (a shot without a profile in its
  workflow). A retry would change nothing about them.

If `waiting_for_tablet` is set, the tablet was off. That is not an error, just
nothing to fetch.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DECAID_URL` | — | Decaid on the tablet; **private IP literals only** (SPEC §20.6) |
| `MCP_PATH_SECRET` | — | ≥32 characters `[A-Za-z0-9_-]`, stands in for authentication |
| `SYNC_INTERVAL_MIN` | `15` | poll interval in minutes (0–1440); `0` switches the background loop off |
| `WRITE_ENABLED` | `false` | unlocks the four write tools (SPEC §18, §20.5). Off means the tools do not exist |
| `GUARD_RULES` | all | comma-separated rule names, or `none` (SPEC §20.7) |
| `BEAN_AGE_WARN_DAYS` | `42` | threshold of the `bean_age` rule |
| `RATING_GRACE_HOURS` | `36` | grace period of the `missing_rating` rule |
| `DOSE_TOLERANCE_G` | `1.0` | tolerance of the `dose_outlier` rule (yield: four times this) |
| `NTFY_URL` / `NTFY_TOPIC` / `NTFY_TOKEN` | — | guard notification; empty means none |
| `DB_PATH` | `/data/shots.db` | SQLite file, must be absolute |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `TZ` | `Europe/Berlin` | display time zone; storage is always UTC |
| `PUBLIC_BASE_URL` | — | only for `--print-connector-url` |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | bind address inside the container |
| `VISUALIZER_EMAIL` / `VISUALIZER_PASSWORD` | — | no longer the source; kept for the optional showcase upload |

If something is missing or implausible the server does not start and lists
**all** problems at once.

## Security

- No secret in logs or tool responses: the redaction sits in the log formatter
  and catches tracebacks and third-party libraries too. Tested in
  `tests/test_logging_redaction.py`.
- The redaction knows not only the plaintext password but also the base64 part
  of the `Authorization` header — the form in which it actually travels. In
  addition an expression masks every `Authorization` header regardless of its
  content.
- If the password is shorter than 8 characters the server warns at startup: the
  filter deliberately lets such short values through, because it would otherwise
  shred incidental matches in the text.
- uvicorn access logs are off — they would record the secret path of every
  request.
- `/healthz` is reachable through the tunnel as long as the ingress forwards the
  hostname wholesale. The route only returns `ok` — no numbers, no version, no
  hint at the secret path. To close it, add an entry with `path: ^/healthz$` and
  `service: http_status:404` before the catch-all rule in the `cloudflared`
  ingress (SPEC §10.1).
- **Decaid is addressed on the local network only.** `DECAID_URL` accepts
  private IP literals and nothing else — a hostname could be repointed later
  without the configuration changing. This traffic must never go through the
  Cloudflare tunnel (SPEC §20.6).
- Container: non-root (UID 10001), `read_only: true`, `no-new-privileges`, only
  `/data` and `/tmp` writable.

## The schema

`migrations/001_decaid_init.sql` creates the archive from scratch. The
migrations of the Visualizer era sit under `migrations/visualizer-era/` and are
no longer applied; there is deliberately no path from there to here, because
Decaid holds the more complete data and reaches back to 2026-06-24.

`db.py` refuses to open a file from the Visualizer era. The new migrations
consist of `CREATE TABLE IF NOT EXISTS` — applied to an old file they would
silently do nothing and leave the old schema standing while the server acted as
if it were up to date.

Worth knowing about the columns:

- `shots.time_source` records per shot whether its timestamp arrived in UTC
  (`de1app` import) or in local time (recorded by Decaid). Without it, what was
  converted could no longer be traced afterwards.
- `shot_series` carries the **target** values per data point
  (`target_pressure`, `target_flow`, …) as well as `state`, `substate` and
  `profile_frame`. None of that existed in the Visualizer era.
- `shots.bean_batch_id` and `shots.bean_id` carry no foreign key on purpose: a
  shot must not fail to be archived because its batch was deleted in Decaid.
- `shots.raw_json` is stored **without** the measurements. Those live in
  `shot_series`, and a detail response weighs about 140 kB with them.

### `version_hash` vs. `semantic_hash`

`version_hash` is the **identity** of a profile version — a shot's link hangs on
it and it is never touched. `semantic_hash` is pure **grouping**: it runs over
the brewing-relevant fields only (steps, targets, tank temperature, beverage
type). Title, author and notes are not included, so two versions differing only
in a note land in the same group.

The occasion for the hash was a real case: two versions of the default profile
differed only in two empty extra keys and a double space in the notes.

## Acceptance on the host

Four of the six criteria from SPEC §13 run automatically
([tests/test_acceptance.py](tests/test_acceptance.py), each test with its
number). Two need the real machine or Docker. Work through them in this order:

### 0. Precondition — the image exists

```bash
docker compose build                       # deployment A
# or: workflow green on GitHub, package visible under ghcr.io (deployment B)
```

**Success:** the build completes without errors.

### 1. First start

```bash
docker compose up -d && docker compose logs -f
```

**Success:** the logs show, in order,
`migrations applied files=001_decaid_init.sql`, `sync worker started`, then
`sync done mode=backfill new=<n> ... errors=0`. That covers **criterion 1**. No
`docker logs` entry may contain the password, the `MCP_PATH_SECRET` or an
`Authorization` header (**criterion 6**, spot check):

```bash
docker compose logs | grep -iE 'authorization|<the-first-8-characters-of-the-secret>'
```

**Success:** no hits, or only lines carrying `***REDACTED***`.

### 2. Health check

```bash
docker inspect --format '{{.State.Health.Status}}' decentespresso-mcp
```

**Success:** `healthy` (can take up to 75 s — `start_period` 15 s plus one
interval).

### 3. Criterion 5 — restart without data loss

```bash
docker compose exec decentespresso-mcp python -c \
  "import sqlite3;print(sqlite3.connect('/data/shots.db').execute('select count(*) from shots').fetchone())"
docker compose restart
# wait 30 s, then run the same command again
```

**Success:** the same count before and after, health check `healthy` again, and
no `migrations applied` line on the second start (the migrations are already
recorded).

### 4. Connect the connector

```bash
docker compose exec decentespresso-mcp decentespresso-mcp --print-connector-url
```

Enter the printed URL in claude.ai under Settings → Connectors → *Add custom
connector*, leaving the OAuth fields empty.

After connecting, `status()` is worth a look: `version` and `build_ref` say
which build is really running — not which one was built.

**Success:** the connector connects and eleven tools appear under "+" in the
chat (fifteen with `WRITE_ENABLED=true`). Test question: *"What is the state of
my espresso archive?"* → `status()` answers with the shot count. If it does not
connect, check first:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<hostname>/<secret>/mcp
```

**Success:** `400` or `405`, **not** `404`. A `404` means the wrong path or the
wrong secret; a `502` means cloudflared cannot reach the container (same Docker
network? service name in the ingress correct?).

### 5. Criterion 2 — a new shot appears in time

Pull an espresso. Note the time. Then ask in claude.ai: *"Show me my last shot."*

**Success:** `get_shot("latest")` returns the new shot. That may work
**immediately** — the freshness check syncs when the last one is more than two
minutes ago, and `freshness.synced` is then `true`. Without asking, the shot
turns up in `list_shots` after at most `SYNC_INTERVAL_MIN` + 1 min; that is the
bound from criterion 2.

If nothing arrives:

```bash
docker compose exec decentespresso-mcp decentespresso-mcp --sync-once
```

The JSON output shows `new_shots`, `errors` and `warnings` in plain text. If it
shows `waiting_for_tablet`, the tablet is off — switch it on and try again.

### 6. Criterion 4 — a profile change at the machine

Change a profile on the DE1 (the temperature by one degree, say), pull a shot.
Then in claude.ai: *"Which profile versions are there?"*

**Success:** `list_profiles()` shows one version more for that profile than
before, and the older shot still hangs on its old `version_hash`. If only the
`version_hash` changes and not the `semantic_hash`, the change was cosmetic —
the machine reformatted the file without anything about the shot changing.

## Backup

```bash
sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"
```

Set this up as a nightly host cron job with a 7-day rotation.
