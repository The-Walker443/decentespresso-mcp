# Specification: `decentespresso-mcp` — MCP server for espresso shot analysis

**Version:** 1.5 · **Date:** 2026-09-14 · **Audience:** Claude Code (implementation) + operator (Matthias)

> **Changes 1.5 (2026-09-14):**
> - Project renamed: `visualizer-mcp` → `decentespresso-mcp`. The name follows
>   the **machine**, not the source. Since §20 visualizer.coffee is neither the
>   source nor the destination of the archive chain; a name pointing at it would
>   describe the project wrongly. Affected: package name, module directory,
>   console entry point, FastMCP server name, User-Agent, image path, container
>   and volume name. `visualizer_client.py` keeps its filename — that module
>   really does talk to Visualizer, so the name is historically correct.
> - The repository switches to English throughout: code, docstrings, comments,
>   messages, tests, documentation and commit messages. User data is not
>   translated; changelog entries written before the switch stay in German.
>
> **Änderungen 1.4 (2026-09-14):**
> - §20 (M8): Decaid im LAN ersetzt visualizer.coffee als Quelle.
>   Historien-Reset, neues Quellschema mit Sollwerten pro Messpunkt,
>   Wächter und Audit. Verifikationstabelle T1–T19 gegen Decaid 0.8.5+2624.
>
> **Änderungen 1.3 (2026-08-31):**
> - §19 Versionierung: `version` kommt aus den Paketmetadaten, `milestone` wird
>   daraus abgeleitet, `build_ref` nennt den gebauten Commit.
>
> **Änderungen 1.2 (2026-08-31):**
> - §17 (M6) Antwortökonomie: `curve_shape`, `compare_shots` in einem Aufruf,
>   gestraffte Docstrings, Messung je Aufruf.
> - §18 (M7) Schreibende Tools: `update_shot` hinter `WRITE_ENABLED`. Damit
>   ist „rein lesend" aus §1 kein Nicht-Ziel mehr — Löschen bleibt eines.
>
> **Änderungen 1.1 (2026-08-01)** — nach Abnahme von M1:
> - §6.2: Inkrementeller Sync läuft über `updated_after` statt über die
>   Seitenheuristik (die API stellt den Parameter bereit; er findet auch
>   nachträglich geänderte Shots).
> - §8: `pi_end` primär aus den Phasenmarken; `peak_pressure` aufgeteilt in
>   `peak_pressure_infusion` und `max_pressure_global`.
> - §13: Erwartungswerte des Referenz-Shots an §8 angeglichen (`end_pressure`
>   ≈ 5.3, `t_first_drops` ≈ 5.3), jeweils mit Begründung.

---

## 1. Goal and context

A self-hosted MCP server (Docker, homelab) that gives Claude in claude.ai and
the Claude apps access to espresso shot data through a custom connector:

1. **Archive shots locally** (SQLite) so the full history survives independently
   of whatever the source keeps. Until M8 the source was visualizer.coffee with
   its one-month free-tier window; since §20 it is Decaid on the tablet.
2. **Load, parse and version profiles automatically** — Claude gets the target
   curves and phases as plain JSON, never a manual upload again.
3. **Return compact, analysable tool responses**: shot lists (filterable by
   bean), individual shots including curve and profile, derived metrics,
   comparisons.

**Environment (existing):**
- Docker stack on the home network, a `cloudflared` tunnel is already running.
- Machine: Decent DE1 Pro (firmware/app 1.46).
- Claude usage: claude.ai web plus the mobile app (custom connectors can only be
  added through web/desktop; using them afterwards works on mobile too).

**Non-goals (v1):** no web UI of our own, no multi-user, no self-hosting of
Visualizer itself, **no deleting** of shots.

*(This list originally also said "no writing". §18 settled that: a single tool,
a strict whitelist, off by default. Reading still dominates — writing happens
only on an explicit instruction.)*

---

## 2. Architecture

```
Decent DE1 ──▶ Decaid on the tablet (source of record for shots)
                      │  REST API over the local network, no auth
                      ▼
           ┌────────────────────────────┐
           │  decentespresso-mcp        │  Docker container
           │  ├─ sync worker            │  (poll every N min + manual)
           │  ├─ SQLite  /data          │  (shots, curves, profiles, versions)
           │  └─ MCP server             │  streamable HTTP  :8000
           └───────────┬────────────────┘
                       │  internal Docker network
             cloudflared (existing)
                       │  https://<hostname>/<secret>/mcp
                       ▼
          Claude (claude.ai / desktop / mobile)
          custom connector; read tools plus - only with
          WRITE_ENABLED - the four write tools (§18, §20.5)
```

Principle: **the MCP container is the source of truth for analysis** (full
history, profiles, metrics). Since §20 nothing in that chain leaves the local
network. The upload to visualizer.coffee can carry on as a community window but
is no longer part of the chain.

---

## 3. Tech stack (binding; justify deviations)

| Component | Choice | Reason |
|---|---|---|
| Language | Python ≥ 3.12 | ecosystem, TCL parsing possible through the stdlib |
| MCP framework | `fastmcp` (v2.x, jlowin/fastmcp) | streamable HTTP transport built in, little boilerplate |
| Transport | **streamable HTTP** | supported by Claude; SSE is on its way out |
| HTTP client | `httpx` | async, timeouts, retries |
| DB | SQLite (file in volume `/data`) | single user, simple backup, no extra container |
| Scheduler | `apscheduler` (or an asyncio loop) | periodic sync in the same process |
| Container | `python:3.12-slim`, non-root user | small, safe |
| Tests | `pytest` + fixture files (real CSV/TCL samples) | deterministic |

---

## 4. Visualizer API — reference and duty to verify

> Superseded by §20 as of M8: Decaid is the source. This section documents the
> era before it and stays relevant for the optional showcase upload.

**Authoritative source: https://apidocs.visualizer.coffee/ — verify the schema
and field names there before implementing any endpoint.** State of research:

| Endpoint | Auth | Status | Purpose |
|---|---|---|---|
| `GET /api/shots?page=&items=` | Basic | documented | paginated list of one's own shots (metadata, ids) |
| `GET /api/shots/{id}` or `/api/shots/{id}/download` | Basic | check the API docs | full shot data including time series as JSON |
| `GET /api/shots/{id}/profile.csv` | public shots: none | **verified** | time series as CSV (columns see §7.2) |
| `GET /api/shots/{id}/profile` | public shots: none | **verified** | profile file, content type `application/x-tcl` |

Rules:
- **HTTP Basic Auth** with `VISUALIZER_EMAIL` / `VISUALIZER_PASSWORD` (per the
  API docs, intended for personal automation). Credentials from the environment
  only.
- Prefer the JSON download endpoint for time series (one request rather than CSV
  and metadata separately); implement the CSV endpoint as a fallback.
- Poll politely: default interval 15 min, use `If-None-Match`/ETag where
  available, back off on 429/5xx (exponential, max 1 h), and set the User-Agent
  `decentespresso-mcp/<version> (private, contact mail)`.
- Log every Visualizer error, but tools must never emit credentials or complete
  HTTP headers.

---

## 5. Data model (SQLite)

Migrations as numbered SQL files (`migrations/001_init.sql`, …), applied
automatically at startup. Set `PRAGMA journal_mode=WAL;`.

> The schema below is the Visualizer-era one. §20.4 replaces it; the current
> schema lives in `migrations/001_decaid_init.sql`, the old migrations under
> `migrations/visualizer-era/`.

```sql
CREATE TABLE shots (
  id            TEXT PRIMARY KEY,          -- Visualizer UUID
  started_at    TEXT NOT NULL,             -- ISO8601 UTC
  bean_brand    TEXT,                      -- "Tchibo", for instance
  bean_type     TEXT,                      -- "Test", for instance
  bean_notes    TEXT,
  profile_name  TEXT,
  profile_id    INTEGER REFERENCES profiles(id),
  grinder_model TEXT,
  grinder_setting TEXT,
  dose_g        REAL,                      -- may be absent -> from the download JSON, else NULL
  yield_g       REAL,
  duration_s    REAL,
  ratio         REAL,                      -- yield/dose, computed
  drink_tds     REAL,
  drink_ey      REAL,
  enjoyment     INTEGER,                   -- Visualizer rating if present
  notes         TEXT,
  raw_json      TEXT NOT NULL,             -- the complete API response (post-processing)
  synced_at     TEXT NOT NULL
);

CREATE TABLE shot_series (                 -- time series, one row per data point
  shot_id   TEXT REFERENCES shots(id) ON DELETE CASCADE,
  elapsed   REAL NOT NULL,
  pressure  REAL, flow_in REAL, flow_out REAL,
  weight    REAL, temp_mix REAL, temp_basket REAL,
  PRIMARY KEY (shot_id, elapsed)
);

CREATE TABLE profiles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,             -- title from the TCL
  version_hash  TEXT NOT NULL UNIQUE,      -- sha256 of the normalised TCL
  raw_tcl       TEXT NOT NULL,
  parsed_json   TEXT NOT NULL,             -- see §7 for the parser output
  profile_notes TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE sync_state (                  -- key-value: last run, cursor, errors
  key TEXT PRIMARY KEY, value TEXT
);

CREATE INDEX idx_shots_bean ON shots(bean_brand, bean_type, started_at DESC);
CREATE INDEX idx_shots_started ON shots(started_at DESC);
```

Design decisions:
- Always store `raw_json` -> later schema extensions without a re-sync.
- Profiles are **deduplicated through `version_hash`**; a shot references exactly
  the profile version it was pulled with. That keeps it traceable which targets
  an old shot ran on even after the profile was changed later.
- Deleting a shot at the source deletes **nothing** locally (that is the point of
  an archive).

---

## 6. Sync logic

> Superseded by §20.3 as of M8. Decaid has no server-side time filter, so the
> incremental run pages a client-side `updatedAt` cursor.

1. **Initial backfill:** page through `GET /api/shots` to the end; for every
   unknown shot load and store the detail data plus the profile TCL.
2. **Incremental (every `SYNC_INTERVAL_MIN`, default 15):**
   `GET /api/shots?sort=updated_at&updated_after=<cursor>` (Unix seconds). The
   cursor is the highest known `updated_at` minus 120 s of overlap. Every listed
   id that is unknown **or** whose `updated_at` changed gets fetched again. If
   the run hits errors the cursor stays put.
   This replaces the earlier heuristic of "page 1 until only known ids remain":
   that would never have found shots changed after the fact (notes, rating, TDS).
3. **Dedupe:** the primary key is the source UUID. Fetching a known shot again
   only updates mutable fields (notes, rating, TDS) through an upsert.
4. **Profile handling per shot:** load the TCL -> normalise (unify whitespace and
   line endings) -> sha256 -> if the hash is new: parse and insert into
   `profiles`; otherwise only update `last_seen`. Link the shot via `profile_id`.
5. **Error handling:** one faulty shot does not abort the run; record errors in
   `sync_state['last_errors']` (a JSON list, max 20).
6. **Times:** store UTC throughout internally; tool output carries a time zone
   suffix (display TZ `Europe/Berlin` from the env var `TZ`).

---

## 7. Parsing

### 7.1 DE1 profile (.tcl)

Format: a flat Tcl key-value structure, among others `title`, `author`,
`profile_notes`, `beverage_type`, `settings_profile_type`, the
temperature/pressure/flow settings, and for advanced profiles
`advanced_shot { {step1…} {step2…} }` (a list of step dicts with `name`,
`temperature`, `pressure`/`flow`, `seconds`, `transition` and `exit_*` fields).

**Implementation:** no regex fiddling — use the stdlib:

```python
from tkinter import Tcl
tcl = Tcl()
pairs = tcl.splitlist(raw)            # top level: alternating key, value
steps = [dict_from(tcl.splitlist(s))  # advanced_shot: a list of step strings
         for s in tcl.splitlist(d["advanced_shot"])]
```

(Runs headless without a display. Fallback on a parse error: store the raw text,
extract `profile_notes`/`title` with a tolerant line scan, flag `parse_ok=false`
in the JSON.)

**Container environment (revision 1.1, made precise after a production crash):**
the design was right — `Tcl()` needs no X — but `python:*-slim` still needs one
ingredient. `_tkinter` is compiled in there, yet the Tk runtime libraries are
missing; even `import tkinter` fails with

```
ImportError: libtk8.6.so: cannot open shared object file
```

The Dockerfile therefore installs `libtk8.6` (`apt-get install -y
--no-install-recommends libtk8.6`, which pulls in `libtcl8.6` and the necessary
X11 libraries as dependencies). The `tk` metapackage with `wish` and its tools
is not needed.

Two safeguards, so the same failure does not surface in production twice:

- The `tkinter` import in `tcl_profile.py` is soft. If the interpreter is
  unavailable the module sets `TCL_INTERPRETER_AVAILABLE = False`, records the
  reason in `TCL_IMPORT_ERROR` and carries on parsing with a list splitter of its
  own (`_split_tcl_list`) — bare words, `{...}` with nesting and newlines,
  `"..."` with backslash substitution, no substitution inside braces. A profile
  parser is no reason to stop the service from starting. A test simulates the
  import failure and checks exactly that.
- The smoke step in the build workflow **requires** the interpreter in the
  finished image. If the apt line is missing the build goes red rather than the
  container.

Both parser routes are checked against each other on all fixtures, including the
question of which inputs they reject.

**Parser output (`parsed_json`):**

```json
{
  "title": "D-Flow / default", "author": "Damian", "type": "advanced|pressure|flow",
  "notes": "…profile_notes…", "target_weight_g": 36.0, "target_temp_c": 88.0,
  "steps": [
    {"name": "Fill", "mode": "flow", "target": 6.0, "temp_c": 88.0,
     "duration_s": 25, "transition": "fast",
     "exit": {"type": "pressure_over", "value": 4.0}}
  ],
  "parse_ok": true
}
```

> Superseded by §20.4 as of M8: Decaid ships the profile as JSON inside the
> workflow, and `decaid_profile.py` produces the same `parsed_json` shape from
> it. This module stays readable for the archived era.

### 7.2 Time series (CSV fallback)

Columns: `information_type, elapsed, pressure, current_total_shot_weight,
flow_in, flow_out, water_temperature_boiler, water_temperature_in,
water_temperature_basket, metatype, metadata, comment`. Rows with
`information_type=meta` form a metadata map, `=moment` are the data points. Note:
`dose` is **not** in the CSV (only in the detail JSON or on the shot page);
boiler temperature is often empty.

---

## 8. Derived metrics (define them deterministically)

Module `metrics.py`, computed once per shot and cached (a column or the table
`shot_metrics`, JSON). Definitions — implement exactly these, so values stay
comparable across shots:

| Metric | Definition |
|---|---|
| `t_first_drops` | the smallest `elapsed` with `weight > 0.3 g` |
| `pi_end` | **primarily** from the machine's phase report: the moment that marks the end of preinfusion. **Fallback** only when the shot has no phase markers: the smallest `elapsed` with `pressure ≥ 0.6 × max_pressure_global`. Which route applied is recorded in `pi_end_source` (§20.4 gives the current three-level hierarchy) |
| `peak_pressure_infusion`, `t_peak` | the pressure maximum within the window `[0, pi_end + 2 s]` plus its moment. That is the pressure building the puck — with ramping profiles (D-Flow) the global maximum sits at the end of the shot and says nothing about it |
| `max_pressure_global` | the global pressure maximum across the whole shot, its own field |
| `pressure_dip_after_peak` | `peak_pressure_infusion − min(pressure)` within `[t_peak, t_peak+4 s]` (an indicator of channelling or the puck giving way) |
| `avg_flow_pour` | the mean of `flow_out` over `[pi_end, end]` |
| `flow_stability` | the coefficient of variation of `flow_out` over the same window |
| `end_pressure` | the mean of `pressure` over the last 2 s |
| `pressure_trend_pour` | the linear slope (bar/s) of `pressure` over `[pi_end, end]` |
| `temp_basket_mean/std` | over `[pi_end, end]` |
| `duration_s`, `ratio` | the last `elapsed`; `yield/dose` (dose from the metadata, else null) |

Every value is rounded (pressure 0.1, flow 0.01, time 0.1 s). A missing basis ->
the field is `null` plus an entry in the `warnings` list.

---

## 9. MCP interface

Server name: `decentespresso`. Every tool is **read-only** except `sync_now` and
the write tools from §18 and §20.5. Response budget: ≤ ~15 kB by default; always
downsample curves (§9.1).

### 9.1 Downsampling rule

Parameter `max_points` (default 120, max 400). Evenly thinned over time;
additionally guaranteed to be included: the first point, the last point, the
point of `max_pressure_global` and the point of `peak_pressure_infusion` (this is
about curve fidelity, hence both maxima).
Returned as compact arrays (`t[]`, `p[]`, `fo[]`, `fi[]`, `w[]`, `tb[]`) rather
than a list of objects — that saves roughly 60 % of the tokens.

### 9.2 Tools

| Tool | Parameters | Returns (core) |
|---|---|---|
| `list_beans()` | – | beans with `name, roaster, shot_count, first/last_shot, the most recent grinder_settings` |
| `list_shots(bean?, roaster?, profile?, since?, until?, limit=10, cursor?)` | filters case-insensitive, substring match | compact rows: `id, started_at, bean, profile, grind, dose, yield, ratio, duration, peak_pressure_infusion, short note`; `next_cursor` |
| `get_shot(id \| "latest", include_curve=true, max_points=120)` | `latest` optionally with a `bean` filter | metadata + metrics (§8) + curve (§9.1) + profile summary (`title, version_hash[:8], compact steps`) |
| `get_shot_metrics(id)` | – | the §8 metrics plus warnings only |
| `compare_shots(ids[2..4], include_curves=false)` | must exist | a table of the metrics side by side plus a delta column against the first shot; optionally curves (then `max_points=60` per shot) |
| `list_profiles()` | – | per profile: `name, versions[] (hash8, first/last_seen, shot_count)` |
| `get_profile(shot_id? \| name?, version_hash?)` | exactly one argument | the complete `parsed_json` plus `notes`; with `name` and no version: the newest |
| `sync_now()` | – | `{new_shots, updated, new_profile_versions, duration_ms, errors[]}` |
| `status()` | – | database statistics, last sync, version, and a warning when the last sync is long enough ago that shots may be missing |

Conventions:
- Errors as structured MCP tool errors with a clear message (`shot_not_found`,
  `waiting_for_tablet`, `decaid_rejected`, …).
- Every tool description (docstring) explains the units (bar, ml/s, g, °C, s) —
  the model reads that and then interprets the numbers correctly.
- Date parameters: ISO8601 or a relative shorthand (`"7d"`, `"1m"`).

### 9.3 Optional MCP prompts (nice to have)

- `dial_in_check(shot_id)` — a pre-formulated analysis task (metrics against the
  profile targets).
- `bean_history(bean)` — an evaluation of one bean over time.

---

## 10. Security

**Threat model:** the endpoint is publicly reachable (Claude calls it from
Anthropic's infrastructure, so restricting it to the home network or a VPN does
NOT work). The data is uncritical (espresso shots), the credentials are not.

Measures (v1, pragmatic):
1. **A secret path instead of auth:** the MCP endpoint sits under
   `https://<hostname>/<MCP_PATH_SECRET>/mcp` with `MCP_PATH_SECRET` being 32+
   random characters (`openssl rand -hex 24`). Every other path -> 404 without a
   body.

   **Exception `/healthz`** (as implemented, corrected against the draft): the
   route lives on the same ASGI app as the MCP endpoint and is therefore
   reachable through the tunnel as long as the ingress forwards the hostname
   wholesale — which the template in §11.4 does. It returns nothing but `ok` as
   text: no counts, no version, no hint at the secret path. What a scanner gains
   is the knowledge that something runs behind the hostname at all — which a 404
   with a TLS handshake gives away anyway.

   To close it regardless, add this before the catch-all rule in the
   `cloudflared` ingress:

   ```yaml
   - hostname: coffee-mcp.example.com
     path: ^/healthz$
     service: http_status:404
   ```

   The Docker health check is unaffected; it addresses `127.0.0.1:8000` inside
   the container and never goes through the tunnel.
2. **No Cloudflare Access in front** — Access would block Claude from
   establishing the connection. Instead, in Cloudflare: a rate-limiting rule
   (100 req/min, say), bot fight mode adjusted or off for that hostname, and the
   standard WAF rules on.
3. **Credentials:** through the environment or `.env` only (not in git; commit
   `.env.example`). Tools and logs never emit credentials, auth headers or
   complete URLs carrying the secret. Implement a log filter for that.
4. **Container hardening:** non-root user, `read_only: true` root filesystem,
   only `/data` writable, `no-new-privileges`, no ports published on the host
   (only the internal Docker network to the cloudflared container).
5. **Writing is the exception:** of all the tools only the `update_*` and
   `set_*` ones mutate anything at the source, and only when `WRITE_ENABLED` is
   set — otherwise they are not registered at all (§18.4). They are idempotent,
   have a closed field list and cannot delete. `sync_now` is likewise idempotent
   and writes nothing outward.
6. **Decaid is local only** (§20.6): `DECAID_URL` accepts private IP literals
   and nothing else, and that traffic must never go through the tunnel.

**Upgrade path (v2, optional):** FastMCP brings auth providers (OAuth 2.1 /
token verifier). Claude supports both authless and OAuth-based remote servers;
if wanted, retrofit OAuth later and retire the secret path. File it as an issue
in the repo, do not build it in v1.

---

## 11. Deployment

### 11.1 Repository layout

```
decentespresso-mcp/
├─ compose.yaml
├─ Dockerfile
├─ .env.example
├─ migrations/001_decaid_init.sql
├─ src/decentespresso_mcp/
│  ├─ server.py            # FastMCP app, tools, prompts
│  ├─ sync.py              # scheduler + sync worker
│  ├─ decaid_client.py     # httpx client for Decaid (§20)
│  ├─ decaid_mapping.py    # normalisation into archive rows (§20.4)
│  ├─ decaid_profile.py    # profile versioning from the workflow JSON
│  ├─ visualizer_client.py # the Visualizer client, superseded (§20.1)
│  ├─ tcl_profile.py       # §7.1, superseded
│  ├─ metrics.py           # §8
│  ├─ guards.py            # §20.7
│  ├─ notify.py            # ntfy, §20.7
│  ├─ writes.py            # whitelists, §18.2 and §20.5
│  ├─ db.py                # schema, migrations, queries
│  └─ config.py            # env parsing, validation at startup
├─ tests/
│  ├─ fixtures/            # real CSV, TCL and JSON samples
│  └─ test_*.py
└─ README.md               # operating manual (a short form of §11–13)
```

### 11.2 compose.yaml (template)

```yaml
services:
  decentespresso-mcp:
    build: .
    container_name: decentespresso-mcp
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: Europe/Berlin
    volumes:
      - ./data:/data
    networks:
      - cloudflared_net          # adjust to the tunnel's existing network
    read_only: true
    tmpfs: [/tmp]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval: 60s
      timeout: 5s
      retries: 3

networks:
  cloudflared_net:
    external: true
```

### 11.3 .env.example

```dotenv
DECAID_URL=http://10.100.100.171:8080
MCP_PATH_SECRET=<openssl rand -hex 24>
SYNC_INTERVAL_MIN=15
DB_PATH=/data/shots.db
LOG_LEVEL=INFO
PUBLIC_BASE_URL=https://coffee-mcp.example.com   # only for printing the connector URL
```

### 11.4 Cloudflare tunnel (an addition to the existing config)

```yaml
ingress:
  - hostname: coffee-mcp.example.com
    service: http://decentespresso-mcp:8000
  # …existing rules…
  - service: http_status:404
```

DNS: CNAME `coffee-mcp` -> `<tunnel-id>.cfargotunnel.com` (proxied).

### 11.5 Connecting it to Claude

1. claude.ai (web) -> Settings -> Connectors -> "Add custom connector".
2. URL: `https://coffee-mcp.example.com/<MCP_PATH_SECRET>/mcp` — OAuth fields
   empty.
3. In the chat, enable it under "+" -> Connectors; the tools appear
   automatically.
4. Mobile: the connector carries over; adding new servers only works through
   web/desktop.
5. Add a project instruction: "For shot analysis use the connector
   decentespresso: list_shots/get_shot first, profiles through get_profile."

---

## 12. Operation and maintenance

**Backup:** a nightly host cron job:
`sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"` plus
rotation (keep 7 days). Optionally later: Litestream replication.

**Updates:** manually, `git pull && docker compose build && docker compose up
-d`. No auto-update (Watchtower) for this container — better to apply
API-breaking changes deliberately.

**Logs:** structured (JSON or key=value) on stdout; `docker logs`. Log sync runs
with a summary (`new=2 updated=1 profiles=0 dur=1.2s`).

**Troubleshooting:**

| Symptom | Check |
|---|---|
| Claude: "couldn't connect" | URL exact (including `/mcp`)? Tunnel ingress? Does `curl -s https://…/<secret>/mcp` return an MCP response or 405 rather than 404? |
| Tools missing in the chat | Connector enabled in the chat? Server reconnected after tool changes? |
| `waiting_for_tablet` in the sync log | The tablet is off. Not an error - switch it on and sync again |
| 502 from the hostname | Container running? Same Docker network as cloudflared? Service name in the ingress correct? |
| Empty shot list | `status()` -> last sync? Run `sync_now()`; check the backfill log |
| New shots missing | Is the tablet reachable? Does `status()` show `waiting_for_tablet`? |
| Profile `parse_ok=false` | Look at the raw TCL in the database; add a parser fixture, file an issue |

**Staleness guard:** `status()` warns when `now − last_sync > 7 days` — that long
a silence means the tablet has not been reachable and newer shots are missing.

---

## 13. Tests and acceptance criteria

**Unit (pytest, offline with fixtures):**
- TCL parser: the D-Flow sample -> the expected steps and notes; broken TCL ->
  `parse_ok=false` without an exception.
- Metrics: the reference shot (fixture = a real shot) -> the expected values.

  The original figures belonged to the Visualizer-era reference shot
  `6eb25d36…`: `peak_pressure_infusion` ≈ 4.1 bar, `end_pressure` ≈ 5.3 bar,
  `t_first_drops` ≈ 5.3 s, `duration` ≈ 22.9 s, `pi_end` ≈ 6.2 s. With §20 the
  acceptance reference moves to a Decaid shot that actually exists in the
  archive: `peak_pressure_infusion` ≈ 6.6 bar, `end_pressure` ≈ 8.5 bar,
  `t_first_drops` ≈ 14.2 s, `duration` ≈ 45.6 s, `pi_end` = 21.1 s (source
  `substate`). The old fixtures stay in the repo as unit fixtures — they still
  exercise valid logic and are the only real data set covering the heuristic
  path.

  On the Visualizer-era shot `max_pressure_global` ≈ 5.4 bar and coincides with
  the final point — which is exactly why the infusion maximum and the global one
  are separate fields.

  Two of those values were aligned to §8 in revision 1.1, not the other way
  round:
  - `end_pressure` ≈ 5.3 rather than 5.4: §8 averages over the last 2 s (5.320
    here). The final single reading alone would be 5.43 but reacts to a single
    outlier — the mean is the more robust measure.
  - `t_first_drops` ≈ 5.3 rather than 4.8: the threshold stays at
    `weight > 0.3 g`. The first weight at all appears at 4.77 s with 0.20 g,
    which sits inside the scale's noise band (resolution ~0.1 g; drop impact and
    vibration cause deflections there). A threshold below 0.3 g would vary with
    the scale and the cup placement and make the values incomparable across
    shots — which is exactly what §8 is meant to prevent.

- pi_end fallback: a fixture test of its own with a series **without** phase
  markers -> `pi_end_source == "heuristic"`. The path stays covered even while
  the archive is served by the machine's own reports.
- Downsampling: the peak point always stays in; `len ≤ max_points`.
- Sync dedupe: running the same data twice -> no duplicates.

**Integration (manual):**
- MCP Inspector (`npx @modelcontextprotocol/inspector`) against the local
  container: every tool callable, schemas valid.
- End to end: connect the connector in claude.ai -> "Show me my last shots with
  Tchibo Test" returns correct data.

**Acceptance (definition of done):**
1. The backfill loads every existing shot including profile versions cleanly.
2. A new shot on the DE1 appears in `list_shots` within `SYNC_INTERVAL_MIN` + 1
   min.
3. `get_shot("latest")` returns metadata + metrics + curve + profile in one
   response ≤ 15 kB.
4. A profile change at the machine produces a new profile version after the next
   shot; the old shot stays linked to the old version.
5. The container survives a restart without data loss; health check green.
6. No secret in logs or tool output (spot check).

---

## 14. Build order for Claude Code

- **M0** scaffolding: repo, Dockerfile, compose, config.py, healthz, a pytest
  setup without CI.
- **M1** `visualizer_client.py` + `db.py` + backfill (metadata and time series
  only).
- **M2** `tcl_profile.py` + profile versioning, linking shot to profile version.
- **M3** `metrics.py` + caching.
- **M4** MCP tools (§9) + inspector test.
- **M5** hardening (read-only FS, log filter), the README operating section,
  acceptance tests.
- **M6** response economy (§17): `curve_shape`, `compare_shots` in one call,
  trimmed docstrings, measurement per call.
- **M7** write tools (§18): `update_shot` with a whitelist, validation and
  write-through, behind `WRITE_ENABLED`.
- **M8** source = Decaid (§20): local-network ingestion, a new source schema,
  guards and audit; Visualizer only an optional showcase.

After every milestone: tests green, **bump the version in `pyproject.toml`**
(minor version = milestone number, see §19), a short commit. Verify the API
schemas against the live API before nailing down any fields.

---

## 15. Deliberately deferred (backlog)

- OAuth instead of the secret path (a FastMCP auth provider).
- The MCP prompts from §9.3 (`dial_in_check`, `bean_history`).
- Weekly or per-bean reports as an MCP resource.
- Structured TDS/EY capture (should a refractometer be bought).
- Importing further sources (Beanconqueror) — the schema is prepared for it
  (`raw_json`).
- ~~Write tools (notes/ratings back to Visualizer)~~ — settled in §18 (M7).
  Deleting stays out deliberately.

---

## 16. Deployment via Portainer + GHCR

Extends §11 for the case where the stack is managed through Portainer rather
than `docker compose` on the host. §11 stays valid for local runs.

### 16.1 Building the image in GitHub Actions

`.github/workflows/build-image.yaml`: on every push to `main` (except pure
documentation changes) and on demand.

1. **Job `test`** — `ruff check` and `pytest`. Deliberately a gate: an image with
   failing tests should not land in the registry.
2. **Job `build`** — `docker/build-push-action` builds from the existing
   `Dockerfile` and pushes to `ghcr.io/<owner>/<repo>`.

Tags: `latest` and `sha-<commit>`. The SHA tag allows rolling back to a specific
version without rebuilding the stack. Authentication runs through the
`GITHUB_TOKEN` the runner provides, with `packages: write` — no secret of your
own is needed. The build cache lives in `type=gha`.

### 16.2 The stack file

`compose.portainer.yaml`, three differences from `compose.yaml`:

| Point | `compose.yaml` (local) | `compose.portainer.yaml` |
|---|---|---|
| Image | `build: .` | `image: ${IMAGE_REPOSITORY}:${IMAGE_TAG}` |
| Data | bind mount `./data`, needs `chown 10001` | named volume `decentespresso_mcp_data` |
| Config | `env_file: .env` | `${VAR}` from the Portainer stack variables |

The named volume is the more important of the three: Docker creates it with the
ownership `/data` has in the image (UID 10001), which removes the stumbling
block of "the container runs non-root, the bind mount belongs to root".

Everything else from §10.4 stays: `read_only: true`, `tmpfs: /tmp`,
`no-new-privileges`, no host ports. In addition a cap on log rotation — the
container runs permanently.

### 16.3 Private repositories

If the GitHub repo is private, so is the package in GHCR, and Portainer cannot
pull it without signing in. Two routes:

- **Make the package public** (GitHub -> Packages -> Package settings -> Change
  visibility). The image contains no credentials — those arrive at runtime from
  the stack variables. Sufficient for this project.
- **Register the registry in Portainer** (Registries -> Add registry -> Custom,
  URL `ghcr.io`, username = GitHub login, password = a PAT with
  `read:packages`). Needed if the package is to stay private.

### 16.4 Acceptance on the host

Criteria 2 and 5 from §13 can only be checked there. The step sequence including
success criteria sits in the README under "Acceptance on the host".

---

## 17. Response economy (M6)

**Problem.** Every turn of a conversation reprocesses the whole context so far.
Two things drive it: responses that deliver more than the question needs, and
questions that cost several calls. Both are fixable without economising on the
quality of the analysis.

**Left untouched:** the metric definitions from §8 (`METRICS_VERSION` stays),
the tool names, and the semantics in the `INSTRUCTIONS`.

### 17.1 Curve shape instead of raw numbers

A new field `curve_shape`, always returned by `get_shot` and `compare_shots`. It
describes the curve segment by segment along the machine's phase markers — the
same source as `pi_end`:

```json
{"segments": [{"from": 6.2, "to": 22.9,
               "p":  {"from": 3.0, "to": 5.4,  "dir": "rising",  "linear": false},
               "fo": {"from": 2.09,"to": 1.8,  "dir": "falling", "linear": false}}],
 "markers": {"t_first_drops": 5.3, "pi_end": 6.2, "t_peak": 7.2},
 "source": "machine"}
```

`dir` compares the starting and ending value (`steady` below 0.2 bar or
0.15 ml/s), `linear` describes the path between them: the maximum deviation from
the straight line, relative to the range within the segment, with a 15 % bound.
The two together — a segment can be `steady` and still not linear if it has a
dip. `source` records where the boundaries came from: `machine` (the machine's
phase markers), `markers` (derived from `pi_end` instead) or `none`.

Consequently the defaults from §9.1 invert: **`include_curve` in `get_shot` now
defaults to `false`**, and `max_points` defaults to 60 rather than 120. The point
arrays remain available unchanged but are the exception.

**Correction to §9.2:** it says "optionally curves (then `max_points=60` per
shot)". Four shots at 60 points each, together with `curve_shape` and the
profiles, come to 18.2 kB and blow the response budget. `compare_shots`
therefore distributes a total budget of 100 points across the compared shots (at
least 20 each): 2 shots -> 47, 3 -> 30, 4 -> 23 points. The worst case then sits
at 13.4 kB. A test checks exactly that case.

### 17.2 `compare_shots` in one call

Additionally returns the profile summary per shot (title, `version_hash[:8]`,
`semantic_hash[:8]`, type, headline targets, step count) and sets
`profile_notice` when the shots did not run on the same targets. Three cases,
deliberately phrased with different force:

| Situation | Note |
|---|---|
| same `version_hash` | none |
| different version, same `semantic_hash` | "purely cosmetic" |
| differing `semantic_hash` | "careful … differences may come from the profile" |
| at least one profile missing | "the comparison of targets is incomplete" |

The third case is the reason for the field: without it one easily reads a
difference in the metrics as a consequence of the grind setting when the profile
was a different one. Switchable through `include_profile=false`.

### 17.3 Docstrings without duplication

The full glossary lives in the `INSTRUCTIONS` — they are always in context
anyway. The tool docstrings now name only the purpose and the meaning of the
parameters and point there for the terms. The detailed explanations of `pi_end`,
the two pressure maxima and `warnings` are removed from the individual tools.

A test pins an upper bound on the sum of all tool definitions and checks that the
glossary texts do not migrate back into individual docstrings.

### 17.4 Measurability

`telemetry.py` logs `tool`, `dur_ms` and `bytes` for every tool call — **no**
parameter values, no URL, no path. Arguments are the likeliest route by which
something confidential eventually ends up in a log line.

Measured against the **development snapshot of 2026-08-01** (14 shots) — both
columns at the same state, so the comparison holds. Bytes:

| Call | before M6 | after M6 | |
|---|---:|---:|---|
| tool definitions (all 9, sent with every request) | 10,399 | 6,876 | −34 % |
| `get_shot("latest")` | 5,021 | 2,092 | −58 % |
| `get_shot(id)` | 4,626 | 1,868 | −60 % |
| `compare_shots(2)` incl. profiles | 1,680 + 2×1,013¹ | 4,234 | 3 calls -> 1 |
| `get_shot(id, include_curve=true)` | 4,626 | 3,959² | −14 % |
| `list_shots(limit=10)` | 3,277 | 3,277 | ±0 |

¹ Before M6 `compare_shots` returned no profiles; the same information cost two
additional `get_profile` calls and therefore three conversation turns.

² Now additionally contains `curve_shape` and is still smaller, because
`max_points` dropped from 120 to 60. Anyone needing more points still asks for
them (maximum 400).

Of the remaining 6,876 B of tool definitions, roughly 3,600 B are the JSON schema
of the parameters. Going lower means fewer parameters, not shorter text.

**Cross-measurement at production size** (snapshot of 2026-08-31, 69 shots, 3
beans, 11 profile versions). Only two values depend on the archive, both
uncritically:

| Call | dev snapshot (14 shots) | production size (69) |
|---|---:|---:|
| `list_beans()` | 522 | 921 |
| `list_shots(limit=10)` | 3,277 | 3,684 |
| `status()` | 654 | 683 |
| tool definitions | 6,876 | 6,876 |

`get_shot` and `compare_shots` do not scale with the archive at all — they return
fixed shots. `list_shots` is capped by `limit` and only grows by
`total_matching`; `list_beans` grows with the number of **distinct beans**, not
of shots. The bounds from §17.4 therefore hold in operation too.

**M8 adds four tools**, and more capability necessarily costs more. The telling
figure is the size **per tool**, and it stayed flat: 1,155 B before M6, 764 B
after M6, 840 B now for the read-only set (9,241 B across 11 tools). With write
mode on it is 808 B per tool (12,123 B across 15) — leaner than the 845 B M7
needed with a single write tool, because the behavioural rules moved into the
`INSTRUCTIONS`.

---

## 18. Write tools (M7)

Settles the backlog item "write tools" from §15. **Deleting remains a non-goal**
— the API can do it (`DELETE /shots/{id}`), v1 does not build it.

### 18.1 Verifying the API (2026-08-01, v1.17.1)

As §4 demands: checked first, built second. The endpoint is
`PATCH /api/shots/{id}` with `{"shot": {…}}`. Four findings, three of them
undocumented:

1. **`Accept: application/json` is mandatory.** Without the header the API
   answers `422 {"error":"Request must be JSON."}` — even with a correct
   `Content-Type`. It appears in no documentation.

2. **The documented field list is incomplete.** `ShotUpdateRequest` lists only
   `profile_title`, `barista`, `bean_weight`, `bean_notes`, `espresso_notes`,
   `private_notes`, the eight tasting notes, `coffee_bag_id`, `tag_list` and
   `metadata`. Actually writable are (checked individually — each one written,
   read back and reset):

   | Field | documented | writable |
   |---|:--:|:--:|
   | `bean_brand`, `bean_type`, `roast_date`, `roast_level` | – | ✅ |
   | `grinder_setting`, `drink_weight` | – | ✅ |
   | `espresso_enjoyment`, `drink_tds`, `drink_ey` | – | ✅ |
   | `bean_weight`, `bean_notes`, `espresso_notes`, `barista` | ✅ | ✅ |
   | `private_notes` | ✅ | ❌ (400, premium) |
   | `id`, `start_time` | – | ❌ (400) |

3. **Fields that are not permitted get discarded silently.** A `400` only comes
   back when *nothing* permitted survives the filtering (`"param is missing or
   the value is empty or invalid: shot"`). A call with `private_notes` **plus**
   one permitted field therefore returns `200` — without writing the note. That
   is why the read-back comparison in §18.3 is not optional polish.

4. **The API validates no value ranges.** `espresso_enjoyment: 999` and `-5` were
   stored without complaint. The validation in §18.2 is the only protection, not
   a second line of defence.

### 18.2 One tool, a strict whitelist

`update_shot(id, fields)`. Permitted are exclusively:

| Group | Fields |
|---|---|
| Bean | `bean_brand`, `bean_type`, `roast_date`, `roast_level`, `bean_notes` |
| Preparation | `grinder_setting`, `bean_weight`, `drink_weight` |
| Rating | `espresso_enjoyment`, `espresso_notes`, `private_notes`, `drink_tds`, `drink_ey` |
| Other | `barista` |

Unknown fields -> an error carrying the complete list. Explicitly blocked are the
identifier, timestamps, telemetry and the profile; they get a message of their
own naming the reason rather than merely "unknown". `null` clears a field.

Validation happens **before** the first API call, with every violation collected:

| Field | Rule |
|---|---|
| `espresso_enjoyment` | whole number 0–100 |
| `bean_weight` | 5–30 g |
| `drink_weight` | 10–100 g |
| `drink_tds` / `drink_ey` | 0–30 % / 0–50 % |
| `roast_date` | ISO `YYYY-MM-DD`, not in the future |
| free-text fields | ≤ 5,000 characters |

Two details from practice: decimal commas are accepted (`"18,5"` -> `18.5`), and
for the roast date the error message explicitly says that the DE1 app writes
`DD.MM.YYYY` while ISO is expected here. **That produces mixed formats in the
archive** — dates written by the machine stay German, ones written by us are
ISO. Accepted deliberately: a machine-readable format is worth more than
uniformity with an ambiguous one.

> §20.5 moves this ruleset onto Decaid's field names and adds three more
> rulesets. The principles below stay unchanged.

### 18.3 Write-through — the source stays the truth

Nothing is ever written locally alone. The chain, under the same lock as the
sync:

1. `GET /shots/{id}` — the before state **fresh from the API**, not from the
   local copy. That could be stale, and then the reported "before" would be a
   claim rather than a measurement.
2. `PATCH /shots/{id}` with the validated fields.
3. `GET /shots/{id}` again, upsert into the local database, recompute the shot's
   metrics — along the same path as in a sync run (`refresh_shot`). A dose change
   changes the ratio; without this step the metrics cache would stay wrong.

The response names `before` and `after` per field, **both from the read-back**.
If a field does not differ it lands in `unchanged` together with a note — that is
the only place where the silent discarding from §18.1.3 becomes visible.

### 18.4 Switch and log

`WRITE_ENABLED` (default `false`). When it is off the tool is **not registered**
— it does not appear in the tool list and does not refuse. A tool that exists
and refuses invites asking again; one that does not exist does not. Without a
connection to the source it stays away as well.

The docstring binds the model: only on an explicit user instruction, exactly the
fields named, confirmation from the values that come back.

One log line per write with the shot id, the **field names** and the duration —
no values. `espresso_notes` and `private_notes` can hold private things, and a
log is the wrong place for those.

### 18.5 Acceptance

Run through on shot `51c96e2c` (Tchibo Test): five invalid inputs refused
without an API call, then `espresso_enjoyment: 35` and a note set. The read-back
through `get_shot`, the local database and the metrics cache agree, and the
cross-check straight against `visualizer.coffee` shows both values.

---

## 19. Versioning and build identity

**Occasion.** During the M7 deployment `status()` reported version `0.1.0` and
milestone `M6` while M7 code was running. Both numbers were maintained by hand:
the version had sat unchanged in the source since M0, and the milestone field
had been forgotten during the bump. The obvious question — *is the old image
still running, or is only the field stale?* — could not be answered from the
response.

### 19.1 One source for the version

`__version__` comes through `importlib.metadata` from the package metadata, and
thus from `pyproject.toml`. No second number in the code — the second is the one
that gets forgotten. If the package is not installed (started straight from the
source tree) the version reads `0+unknown`; deliberately no guess from
`pyproject.toml`, because an invented number would be worse than a visible gap.

**The version is bumped per milestone**, minor version = milestone number:
M7 -> `0.7.0`. A test compares the minor version against the highest milestone
listed in §14 and fails when the bump was forgotten.

### 19.2 Milestone derived, not maintained

`milestone()` derives `M<minor>` as long as the major version is `0`. From
`1.0.0` on the milestone count is over and the field becomes `null` — a derived
value would mislead there.

### 19.3 Which build is running?

The version says *which milestone* was built. For *which commit* there is
`BUILD_REF`: a build argument in the Dockerfile that the workflow fills with
`${{ github.sha }}` and that lands in the image as an environment variable.
`status()` returns it unchanged, and `null` outside a built image.

That makes deployment unambiguous:

| `version` | `build_ref` | Meaning |
|---|---|---|
| as expected | expected SHA | the new image is running |
| stale | old SHA | Portainer did not re-pull |
| as expected | `null` | started locally, not from the image |

The smoke step in the workflow checks both inside the finished image: that the
package metadata is readable and that `BUILD_REF` was passed through.

---

## 20. Source = Decaid, everything local (M8)

> **Numbering:** the brief said §19; that was already taken by "versioning and
> build identity". M8 is therefore §20.

### 20.1 The goal

Until M7 visualizer.coffee was the source. That ran the archive chain through
someone else's cloud, and the one-month free-tier window determined what could be
fetched at all (§1). From M8 on:

```
        DE1 ──BLE──▶ Decaid (tablet, 10.100.100.171:8080)   ← master
                          │  REST + WebSocket, local network only
                          ▼
                 decentespresso-mcp (Docker)   ← archive and analysis
                          │  internal Docker network
                    cloudflared ──▶ Claude
                          ▲
                          └─ NEVER Decaid traffic (§20.6)

        Decaid ──optional──▶ visualizer.coffee   (community showcase,
                                                  not part of the archive chain)
```

**Decaid is the master.** No cloud in the chain between machine and archive. The
Visualizer plugin in Decaid stays permitted but is only a publication route — if
it fails, nothing about the archive changes.

Evidence for the gain, measured on 2026-09-14: Decaid holds **168 shots** going
back to 2026-06-24. The Visualizer archive of the same machine stood at 71, and
even a fresh sync from there only reached 69, because older shots had dropped out
of the free-tier window.

**The history reset is approved:** a new database file, freshly numbered
migrations, no migration path from the Visualizer data.

### 20.2 Verification results

Checked against the running instance on **2026-09-14**, Decaid **0.8.5+2624**
(commit `a08bc41e`, built 2026-09-02). The brief referred to a table T1–T15 from
a source-code verification; that was not attached, so this is our own measurement
against the real API.

| # | Checked | Result |
|---|---|---|
| T1 | Path prefix | **Everything under `/api/v1/`.** `/shots/ids`, `/shots/latest`, `/shots/<id>` without the prefix -> 404 |
| T2 | `GET /api/v1/info` | 200, `{version, fullVersion, commit, commitShort, buildTime, buildNumber, localIp, appStore, branch}` |
| T3 | `GET /api/v1/shots` envelope | `{items, total, limit, offset}` — `total` present |
| T4 | `limit` ceiling | **Caps silently at 100.** `limit=101` and `limit=500` each return 100 items without an error |
| T5 | `offset` | Works; `offset=0` and `offset=1` return different shots |
| T6 | `order` | `asc` -> oldest first (2026-06-24), `desc` -> newest. The default is `desc` |
| T7 | `beanId` filter | Works (`beanId=x` -> `total=0`). `profileId` is **ignored** (`total` unchanged) |
| T8 | Server-side time filter | **Does not exist.** `updated_after`, `updatedAfter`, `since`, `sort` are silently ignored, `total` stays 168 |
| T9 | `GET /api/v1/shots/ids` | 200, **all 168 ids unpaginated** as a flat array |
| T10 | `GET /api/v1/shots/latest` | 200, the full detail including `measurements` |
| T11 | `GET /api/v1/shots/<id>` | 200, the detail with `measurements` (184 points on the reference shot) |
| T12 | `measurements` structure | `{machine{…}, scale{…}, volume}` per point. **No `time` field** — the time axis comes from `machine.timestamp` minus the first. `profileFrame` sits under `machine`, not at point level |
| T13 | `PUT /shots/<id>` deep merge | Confirmed: sending only `annotations.espressoNotes` left the remaining `annotations` (`enjoyment`) and all 184 `measurements` untouched |
| T14 | Protected fields | `PUT` with `createdAt`/`measurements`/`id` -> **400**, nothing changed. Stricter than Visualizer, which discarded the impermissible silently |
| T15 | Server-side `updatedAt` | Set on a content change (`06:02:39Z` -> `15:37:24Z`), `createdAt` untouched |
| T16 | Beans / batches | Beans under `/api/v1/beans`. **Batches under `/api/v1/bean-batches`** or `/api/v1/beans/<id>/batches` — `/api/v1/batches` -> 404. Fields: `roastDate`, `buyDate`, `freezeDate`, `frozen`, `archived`, `beanId` |
| T17 | WebSocket | `/ws/v1/machine/shotState` -> **101 Upgrade**. `/ws/v1/machine/state` -> 404 |
| T18 | `enjoyment` scale | **0–100 as a float** (observed: 40.0, 50.0, 80.0, 100.0). No star scale, so **no ×20 mapping needed** |
| T19 | Retention / pruning | No sign of it: the archive reaches back to 2026-06-24 without gaps, and there is no endpoint for it |

**Write paths, verified on 2026-09-14** (every change reversed immediately and
the restore read back):

| # | Path | Finding |
|---|---|---|
| T20 | `PUT /shots/<id>` `annotations` | `actualDoseWeight`, `actualYield` are taken (read-back confirmed) |
| T21 | `PUT /beans/<id>` | `notes`, `processing` are taken |
| T22 | `PUT /bean-batches/<id>` | `frozen`, `freezeDate`, `roastDate` are taken. Dates come back with a time attached (`2026-09-01T00:00:00.000`), the input `2026-09-01` is accepted |
| T23 | **`unfreezeDate`** | **Does not exist.** Decaid keeps no thaw date — see §20.7 |
| T24 | `PUT /workflow` | `context.grinderSetting`, `context.targetDoseWeight`, `context.beanBatchId` are taken |
| T25 | Protected | `id` -> 400 ("ID in path does not match"), `createdAt`/`updatedAt` -> 400 ("system-managed") |
| T26 | **`timestamp`** | **Not protected.** A `PUT` carrying `timestamp` came back **200** and the value really did stand afterwards |

T26 changes the role of the block list in `writes.py`: for the telemetry fields
it is not the second line of defence but **the only one**. A test of its own
records that.

**Deviations from the brief** — reported rather than built around silently:

1. **Paths** (T1): the brief names `/shots/ids`, `/shots/latest`, `/shots/<id>`
   without the `/api/v1` prefix. They are not reachable that way.
2. **Batches** (T16): `/api/v1/batches` does not exist; the path is
   `/api/v1/bean-batches`.
3. **Protected fields** (T14): refused with `400` rather than ignored — better
   than the Visualizer behaviour from §18.1.3 and it allows a clearer error
   message.
4. **`enjoyment`** (T18): already 0–100. The ×20 star conversion the brief
   mentioned as a precaution is not needed.
5. **`pi_end` from `profileFrame`** — see §20.4, the derivation does not hold
   that way.
6. **Ingestion as one procedure rather than two** — see §20.3.
7. **Dose outliers** — see §20.7, the rule is not checkable as specified.

### 20.3 Ingestion

**Deviation from the brief: one procedure rather than two.** The shot list
returns everything except `measurements` per entry — `updatedAt` in particular.
Without a single detail request it is therefore known which shots changed;
backfill and incremental run are the same algorithm, and `full=true` only forces
a refetch. `GET /api/v1/shots/ids` is not needed for it.

**The list is always read in full.** The brief foresaw stopping the paging as
soon as a page contained only shots with `updatedAt <= cursor`. That does not
hold: the list is sorted by **shot time**, not by modification time. A shot
pulled in June whose note was added today still sits at the back — paging that
stops early would never see it. For 169 shots the full list costs two requests
on the local network; in exchange the sync also finds changes made after the
fact, which was the whole point of the cursor.

**Cap per run:** 60 detail requests. A detail weighs about 140 kB; the first
backfill would otherwise be a good 23 MB in one go over the tablet's Wi-Fi.
Measured on 2026-09-14: 169 shots across three runs, 12.7 s, no errors. A
started backfill only counts as complete once nothing is outstanding — otherwise
the next run would go incremental and leave the gap standing.

- **Event driven (optional):** subscribe to `/ws/v1/machine/shotState`; on shot
  end wait briefly, then `GET /api/v1/shots/latest`. Polling stays **active** as
  the fallback; the WebSocket is an acceleration, not a precondition.

**Operating rule: a tablet that is offline is not an error state.** If Decaid is
unreachable the sync enters the state `waiting_for_tablet`, waits with backoff
and catches up as soon as the tablet is back. The tablet sleeps, gets carried
around, struggles with the Wi-Fi — that is the normal case, not an exception.
There is therefore no error message, no notification and no red status;
`status()` simply shows when contact last existed.

### 20.4 Source schema and metrics

`measurements` are taken over in full, **including `targetPressure` and
`targetFlow`** — the targets are thereby available per data point for the first
time, not only as a profile curve.

**Time axis:** there is no `time` field (T12). `elapsed` is computed from
`machine.timestamp` minus the first data point; on the reference shot that is
184 points across 45.58 s, with a mean spacing of 0.249 s.

#### Two binding normalisations

Both arise from findings of the annotation migration in M8 (2/n) and are measured
against the whole archive.

**(1) Timestamps: the archive keeps UTC throughout.** Decaid does not do so
uniformly. Shots imported from the de1app already carry their timestamp in UTC,
ones recorded by Decaid itself carry local time without a zone. The
discriminator is the identifier (`de1app-<unix time>` = UTC), cross-checked
against `createdAt`; if the two disagree, Decaid has changed its behaviour, and
that produces a warning rather than a silent error. Conversion runs through
`Europe/Berlin` **with full daylight saving handling** — a fixed offset would be
wrong after the last Sunday in October (2026: 25 October). In the ambiguous hour
when the clocks go back the first reading applies. Per shot, `time_source`
records where the stamp came from. The time zone database hangs in the
dependencies as `tzdata`, because a slim base image does not guarantee
`/usr/share/zoneinfo`.

Across the archive (2026-09-14, 169 shots) this separates cleanly and without
overlap — exactly on the day Decaid took over the recording:

| `time_source` | Shots | Period |
|---|---|---|
| `utc` (import) | 88 | 2026-06-24 02:22 … 2026-08-30 12:02 |
| `local_berlin` (native) | 81 | 2026-08-30 18:55 … 2026-09-14 18:34 |

**(2) Rating: a `0.0` from the import era is not a rating.** Decaid creates
imported shots with `enjoyment: 0.0`. Measured across all shots: 75 of the 88
imported ones sit like that, real ratings run between 20 and 100, and **not a
single** natively recorded shot ever carries `0.0`. For shots with a de1app
identifier a 0 is therefore archived as `NULL`. Without this rule 75 phantom
ratings would enter the archive, and `audit_archive` along with the guards from
§20.7 would take them at face value. When **writing** the rule does not apply:
what the user explicitly sets to 0 is an input.

Result across the archive: 0 zeros in the archive, 24 real ratings preserved.

**`pi_end` — deviation from the brief.** The brief wants the derivation to come
primarily from `profileFrame` changes. The measurement shows that does not hold:

```
profileFrame:   (0.00, 2)  (0.99, 0)  (3.73, 1)  (21.10, 2)
state.substate: preparingForShot → preinfusion (0.99) → pouring (21.10)
```

The value at t=0 is a leftover from the preceding shot, and the 0->1 change at
3.73 s lies **inside preinfusion**. "The last `profileFrame` change" would land
correctly here by coincidence, but no longer on a profile with two pouring
frames. `state.substate` names the transition directly and is therefore the
dependable source.

Hence the order: `pi_end_source` = `substate` (the change from `preinfusion` to
`pouring`), failing that `profile_frame` (the last change before pouring
begins), then the derivations from §8 as the fallback. The remaining metric
definitions are unchanged, and `METRICS_VERSION` gets bumped.

**`curve_shape`** additionally gains target-vs-actual per segment, since
`targetFlow` and `targetPressure` are now available point by point.

**Profile versioning:** the source format is the embedded profile JSON under
`workflow.profile` (shape `{version, title, author, notes, beverage_type,
steps[], target_weight, …}`) — the same shape the TCL parser was already
cross-checked against in M2. `version_hash`/`semantic_hash` stay, the
canonicalisation moves to the JSON. **The TCL parser is marked deprecated, not
deleted.**

**How often each source carries** (169 shots, 2026-09-14). The three-level
hierarchy is not a precaution: the de1app never recorded the machine state,
Decaid does. Without the second stage 84 shots would have a guessed `pi_end`.

| Origin | `substate` | `profile_frame` | heuristic |
|---|---|---|---|
| imported (88) | — | 84 | 3 |
| native (81) | 77 | 2 | 2 |

`METRICS_VERSION` rises from 2 to 3 in the process; the cache recomputes on its
own. The expectations from §13 are redetermined against the Decaid reference
shot — the old numbers belonged to a shot that no longer exists in the archive.

**Profile versioning** now runs through the profile JSON embedded in the
workflow rather than through TCL. That saves the second request and the most
common source of failure in the Visualizer era: a profile the API would not hand
out (422), or one whose TCL the parser did not understand. `tcl_profile.py`
stays readable for the archived era but is no longer used.

### 20.5 Write tools

`update_shot` moves to `PUT /api/v1/shots/<id>`; the whitelist covers
`annotations` fields. The guard rails from §18 are unchanged: validation before
sending, read-back before->after, only on an explicit instruction, behind
`WRITE_ENABLED`.

**The whitelist holds `espressoNotes`, `enjoyment`, `actualDoseWeight` and
`actualYield`** — exactly the fields that were written against the real API and
read back (T20). The first two came from the annotation migration, the latter
two from the write verification on 2026-09-14. Better one field too few than one
that gets discarded silently.

Three further tools, each with its own ruleset:

- `update_bean` — `name`, `roaster`, `species`, `processing`, `notes`, `decaf`
  (T21).
- `update_batch` — `roastDate`, `buyDate`, `freezeDate`, `frozen` with date
  validation (T22). **`unfreezeDate` does not exist** (T23); to thaw, set
  `frozen` to false.
- `get_workflow` / `set_workflow` — grind setting, grinder, target dose, target
  yield, batch reference (T24). **No profile change in v1**: it changes brewing
  behaviour fundamentally and belongs at the machine.

The Visualizer tools disappear from the tool list; `visualizer_client.py` is
marked deprecated but not deleted.

### 20.6 Status and security

`status()` gains a `decaid` block: reachable yes/no, last reachable, and the
Decaid version from `/api/v1/info` with a warning on deviation from the verified
version (a constant in the code, updated with every verified upgrade).

**`DECAID_URL` is checked for private address ranges at startup** (RFC 1918,
loopback, link-local). A public address is a configuration error and prevents the
start. Hostnames are rejected as well: a name can be repointed later without the
configuration changing.

**Decaid traffic must under no circumstances go through the Cloudflare tunnel.**
The connection container -> tablet is local network only. The tunnel stays
exclusively the route by which Claude reaches the MCP endpoint.

### 20.7 Guards and audit

Four check rules as a module of their own (`guards.py`), pure functions over rows
— no network, no database, with the clock handed in. Every rule can be switched
off individually through `GUARD_RULES`; a rule that fires too often would
otherwise be ignored wholesale and take the others with it.

| Rule | Checks | Against the archive (169 shots) |
|---|---|---|
| `grind_not_adjusted` | batch changed without adjusting the grind | 5 findings |
| `bean_age` | bean age when pulled, frozen time subtracted | 0 — 6 of 8 batches have no `roastDate` |
| `missing_rating` | `enjoyment` not added once the grace period passed | 29 |
| `dose_outlier` | weights against the workflow target | 29 |

**Deviation 7: the dose is not checkable.** The brief names dose outliers against
the workflow target. The measurement shows there cannot be any: in **all 165**
cases with both values, `actualDoseWeight` is **exactly** equal to
`targetDoseWeight`. The DE1 does not weigh the dose, it adopts the target; the
comparison compared a number with itself. The scatter sits in the **yield** —
that is where the scale really measures, 8.9 g off target on average and 497 g in
the extreme. The rule keeps its name and its intent but checks the yield against
its target and the dose for plausibility only (0 g means the scale was not
connected).

**Bean age without a thaw date.** Time spent frozen does not count as ageing. But
Decaid keeps no `unfreezeDate` (T23): if `frozen` is false and a `freezeDate` is
set nonetheless, the batch was thawed at some point — nobody knows when. The
finding then carries `certain: false`, and the age is explicitly an **upper
bound**. A number that cannot be substantiated is not reported as certain.

**Two bounds instead of one deadline.** `missing_rating` reports only between
`RATING_GRACE_HOURS` (36 h; before that one drinks it first) and seven days.
Without an upper bound the rule reported 140 of the 169 shots — 83 percent of the
archive, because only about one shot in six gets rated. Whoever has not rated a
shot from the week before last will not do so now.

**Notification** over ntfy (`NTFY_URL`, `NTFY_TOPIC`, optional token), with three
caps that all serve the same purpose — that the messages keep being read:

- **at most one message per shot**, even when four rules fire; which shots have
  been reported lives in the sync state and survives a restart
- **only shots from the last 48 hours** — a notification says "something just
  went wrong"; what is further back sits in `audit_archive`
- **at most five messages per run**, newest first

Measured against the archive: `audit_archive` shows 63 findings, ntfy would
report 2.

**No content in findings.** A finding names the identifier, the rule and the
numbers it rests on — never the text of a note or a bean name. The findings leave
the house over ntfy; whatever once landed there stays there. A test checks this
per rule against an input carrying free text.

**Silence is not a failure.** If ntfy is down it gets logged and the sync carries
on; only successfully delivered messages count as reported. A notification is not
part of archiving.

The tool `audit_archive(since?, rule?, limit?)` applies the same rules to the
archive and returns findings **together with the thresholds in force** — a
finding without its yardstick cannot be placed.
