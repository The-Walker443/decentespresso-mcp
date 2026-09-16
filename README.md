# decentespresso-mcp

**Your espresso archive, and someone to talk to about it.**

Every shot your Decent DE1 pulls is already measured in fine detail — pressure,
flow, weight and temperature about four times a second, next to the targets the
profile asked for. [Decaid](https://github.com/decentespresso/decaid) — Decent's
new control app, the one replacing the ten-year-old de1app — records all of that
and, the part that matters here, serves it over a local REST API. This server
reads that API, keeps everything on a machine you own, and hands it to Claude so
you can just ask.

> **Decaid is required.** This project speaks to Decaid and to nothing else. If
> you still run the original de1app, which is what most DE1 owners use today,
> there is nothing here for you yet — see
> [Which app do you run?](#which-app-do-you-run) below.
>
> **Independent community project.** Despite the name, this is not a Decent
> Espresso product and has nothing to do with Decent Espresso International.
> They make the machine and they make Decaid; this server is an outside client
> of Decaid's public API, written by someone who owns one of their machines.
> Nobody there endorses, reviews or supports it.

```
You:    Why was my shot this morning sour and bitter at the same time?

Claude: Sour and bitter together is uneven extraction, so I looked at the puck
        rather than the recipe.

        The machine did exactly what it was told: it tracked its pressure
        target to within 0.03 bar for the whole pour. The puck did not. Its
        resistance fell steadily — 0.62 per second across the 22 seconds of
        pour, the steepest band the diagnostics have. Water found a path and
        widened it, so part of the bed over-extracted while the rest barely got
        wet. That is your sour and bitter at once.

        The other channeling signs were quiet: no pressure dip after the peak,
        flow steady to 0.015 ml/s. Two more need the scale and could not be
        computed for this shot.

        One thing to correct, though — you noted it probably ran too hot. It
        ran 1.6 °C *below* target on average and 2.1 °C under at the worst
        point. Temperature was not your problem here.
```

That exchange is real, from shot `a0b7eb21`. None of the numbers were typed by
hand.

---

## What you get

**A complete archive, independent of the tablet.** Every shot Decaid holds, with
the full measurement series and the exact profile version it ran on, pulled over
`/api/v1` and kept in SQLite. Decaid is the source; this is the memory. The sync
is incremental and notices shots you edited after the fact, the tablet may be
off whenever it likes, and nothing in that chain leaves your network.

**Metrics from the machine's own phase markers.** The end of preinfusion is read
off what the DE1 reports, not guessed from a pressure threshold. Where a shot
does not report it, the fallback says so — you always know whether a number was
measured or inferred.

**Puck diagnostics.** Resistance modelled as pressure over flow squared, and
five independent channeling indicators: a pressure dip after the peak, flow that
will not settle, the pump out-delivering the scale, drops arriving too early,
and a bed losing resistance while the pressure is held. Each keeps its own raw
value and threshold, so you can disagree with the verdict and still use the
evidence. An indicator that could not be measured says so rather than passing
quietly.

**Target versus actual, per data point.** Decaid records what the machine was
*asked* for alongside what it did. Profile compliance here is therefore
measured, not estimated — and a shot that followed its profile perfectly and
still tasted wrong tells you to look at the coffee instead of the machine.

**Three levels of detail.** `summary` to triage, `per_phase` to find a cause,
`detailed` for the raw curve. Each step costs roughly twice the one before, so
deep questions stay cheap until you actually ask one.

**Profile versioning that understands cosmetics.** Two hashes: one identifies a
version exactly, the other groups versions that brew identically. Reformat a
profile and nothing moves; change the pressure by 0.3 bar and it does.

**A watchman.** Four rules run after every sync — changed the batch but not the
grind, beans past their prime (frozen time subtracted), a shot you never rated,
weights that miss their target. With optional ntfy push, capped so the messages
stay worth reading: at most one per shot, only the last 48 hours, at most five
per run.

**Statistics over a period.** What you pulled, from which beans, on which
profiles, how the grind moved, how it tasted — and the same figures for the
period before.

```
You:    How was the last month?

Claude: 117 shots in 30 days, just under four a day, with 13 flushes and aborts
        left out. You rated 18 of them, averaging 53. Typical shot: 17.9 g in,
        38.4 g out, 1:2.1 in 29 seconds.

        The incahuasi accounts for 57 of those and you moved the grinder eleven
        times on it — 4.3 down to 2.5, then settling around 2.7. It averages 47
        across nine ratings, while the Arabica Honey Process sits at 67 across
        four. That is the one to keep.

        Busiest day was 28 August: 34 pulls, 27 of them coffee.
```

**Writing back, by asking.** "Give that one a 90 and note it was the best this
week." Behind a switch that is off by default, with a whitelist of fields
verified against the real API, and every change read back before it is reported
to you.

---

## Which app do you run?

Decaid is new. Most DE1 owners are still on **de1app**, the Tcl application that
has run the machine for a decade, and for them this server has nothing to offer:
de1app keeps no local API to read. There is no workaround planned — the shape of
this project follows from what Decaid exposes, and half of what it does would be
impossible without it.

| | de1app | Decaid |
|---|---|---|
| A local API to archive from | — | REST + WebSocket |
| Per-point `targetPressure` / `targetFlow` | — | yes, and profile compliance is built on it |
| Machine phase markers (`state.substate`) | — | yes, so `pi_end` is read off rather than guessed |
| Profile as structured JSON | Tcl, parsed by hand | yes, with a version hash per shot |
| Beans, batches, the next shot's setting | — | yes, and writable |

**Your old history is not lost.** Decaid imports de1app shots, and this server
recognises them (id `de1app-<unix time>`, timestamps already in UTC, the
phantom `enjoyment: 0.0` of the import normalised back to "not rated"). They
just carry less: no machine phase markers, so `pi_end` falls back to the profile
step boundary or, failing that, to a pressure heuristic that says so in
`warnings`. In this archive, 84 of 88 imported shots land on the step boundary
and 3 on the heuristic, while 77 of 81 shots Decaid recorded itself are read
straight off the machine.

If you are moving over, import into Decaid first and point this at Decaid — not
the other way round.

## How it fits together

```
     Decent DE1 ──BLE──▶ Decaid (tablet, or wherever    ← the source
                              │    you run it)
                              │  /api/v1, local network only
                              ▼
                  decentespresso-mcp (Docker)          ← the memory
                              │  internal Docker network
                        cloudflared
                              │  https://<host>/<secret>/mcp
                              ▼
                           Claude                      ← the interface
```

The tunnel exists so Claude can reach the server. Decaid is never reached
through it — that connection is local network only, and the server refuses to
start if you point it anywhere else.

Decaid does not need to be running. It is up while you are making coffee; the
rest of the time the server waits and catches up afterwards, because the sync is
incremental and Decaid keeps the history. A tablet that is off is a normal
state, not an error, and nothing will nag you about it.

---

## Requirements

- A **Decent DE1** running **Decaid**, reachable on your network. Everything
  here is verified against Decaid 0.8.5+2624 — twenty-seven findings about what
  its API really does are tabulated in the specification, and `status()` says so
  when the version it meets differs from the one this was checked against.
- **Docker** on a machine on the same network as the tablet.
- A **Cloudflare tunnel** or equivalent, if you want to reach it from
  claude.ai. Optional — everything works locally without one.
- Or just Python 3.12, if you would rather run it without Docker.

---

## Quickstart

```bash
git clone https://github.com/The-Walker443/decentespresso-mcp
cd decentespresso-mcp
cp .env.example .env
```

Two things have to be filled in:

```dotenv
DECAID_URL=http://10.100.100.171:8080   # your tablet, private IP only
MCP_PATH_SECRET=<openssl rand -hex 24>  # stands in for authentication
```

Then:

```bash
mkdir -p data && sudo chown -R 10001:10001 data   # the container runs as UID 10001
docker compose up -d
docker compose logs -f
```

The first start backfills everything Decaid has. Point your tunnel at the
container:

```yaml
ingress:
  - hostname: coffee.example.com
    service: http://decentespresso-mcp:8000
  - service: http_status:404
```

Fetch the connector URL — it carries the secret, so it is never logged:

```bash
docker compose exec decentespresso-mcp decentespresso-mcp --print-connector-url
```

Paste it into claude.ai under **Settings → Connectors → Add custom connector**,
leaving the OAuth fields empty. Then ask it something.

Prefer Portainer and a prebuilt image from `ghcr.io`? `compose.portainer.yaml`
and [the specification](SPEC_decentespresso-mcp.md#13-deployment) cover that
route, including the stack variables.

---

## Security, in three sentences

The connection to your tablet is local network only — `DECAID_URL` accepts
private IP literals and nothing else, and that traffic never touches the tunnel.
The MCP endpoint hides behind a secret path instead of a login, every other path
returns an empty 404, and access logs are off so the path never lands in a log
file. The container runs non-root on a read-only filesystem, publishes no host
port, and the log formatter redacts the only two secrets it holds — including
inside tracebacks from libraries that know nothing about it.

---

## The tools

| | |
|---|---|
| `list_beans` · `list_shots` | what is in the archive |
| `get_shot` · `get_shot_metrics` | one shot, at the detail you ask for |
| `compare_shots` | two to four side by side, with deltas |
| `list_profiles` · `get_profile` | profile versions and their targets |
| `get_workflow` | what the next shot would run on, live from the tablet |
| `list_batches` · `get_batch` | which batch, roasted when, how much is gone |
| `audit_archive` | what the watchman found |
| `stats` | a period, with a comparison |
| `sync_now` · `status` | housekeeping |
| `update_shot` · `update_bean` · `update_batch` · `set_workflow` | writing, behind the switch |

Fourteen tools cost 9.9 kB of definitions, eighteen with write mode on cost
12.6 kB — about 700 B each, because the shared vocabulary lives in the server
prompt rather than being repeated in every docstring. That budget is pinned by
tests; it is meant to stay that way.

---

## Configuration

Everything is an environment variable. The server validates all of it at startup
and lists every problem at once, rather than one per restart.

| Variable | Default | |
|---|---|---|
| `DECAID_URL` | — | your tablet, private IP literal only |
| `MCP_PATH_SECRET` | — | ≥32 characters, stands in for authentication |
| `SYNC_INTERVAL_MIN` | `15` | `0` switches the background sync off |
| `WRITE_ENABLED` | `false` | unlocks the four write tools |
| `GUARD_RULES` | all | comma-separated rule names, or `none` |
| `NTFY_URL` · `NTFY_TOPIC` · `NTFY_TOKEN` | — | guard notifications; empty means none |
| `BEAN_AGE_WARN_DAYS` · `RATING_GRACE_HOURS` · `DOSE_TOLERANCE_G` | `42` · `36` · `1.0` | guard thresholds |
| `PUBLIC_BASE_URL` | — | only for printing the connector URL |
| `DB_PATH` · `LOG_LEVEL` · `TZ` | `/data/shots.db` · `INFO` · `Europe/Berlin` | storage is always UTC |
| `HOST` · `PORT` | `0.0.0.0` · `8000` | bind address inside the container |

Syncing by hand, if you ever need to:

```bash
docker compose exec decentespresso-mcp decentespresso-mcp --backfill    # everything
docker compose exec decentespresso-mcp decentespresso-mcp --sync-once   # new and changed
```

Both print a JSON summary. `waiting_for_tablet` means the tablet was off — not
an error, just nothing to fetch.

---

## Honest limits

- **It cannot taste.** The diagnostics describe what the machine and the puck
  did. The connection to sour, bitter or thin is inference, and a shot that
  looks clean can still taste wrong.
- **Channeling indicators rarely agree.** Across 171 real shots no two of them
  ever fired together, which is why they are reported individually rather than
  collapsed into one score you would have to trust blindly.
- **Thresholds are calibrated against one archive** — one machine, one grinder,
  one operator. They are percentile-based and documented one by one in the
  specification, so they can be re-derived rather than believed.
- **Decaid only, and Decaid is young.** No de1app, no visualizer.coffee, no CSV
  import. The API this depends on is new enough that a Decaid update could move
  something; when the version stops matching the one it was verified against,
  `status()` says so rather than pretending.
- **Imported shots know less.** Anything de1app recorded lacks the machine's
  phase markers, so its preinfusion boundary is inferred instead of read off.
  The metric says which, per shot, and refuses comparisons it cannot support.
- **It does not write profiles.** Reading and versioning them, yes; changing
  them belongs at the machine.
- **Nothing is ever deleted.** The API can do it. This project does not build
  it.

---

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"    # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check src tests
```

Fixtures are anonymised real responses, never invented ones. Numbers pinned in
tests come from measurement, and the comment says where. The conventions this
project runs on — verify before you build, whitelist discipline, what a test has
to say — are in [CLAUDE.md](CLAUDE.md); the system itself is described in
[the specification](SPEC_decentespresso-mcp.md).

`data/shots.db` in a checkout is a development copy, not the archive. Numbers
from it are never a statement about the real one — for that, ask `status()`.

---

## Project status

Personal project, built spec-driven with Claude Code; provided as-is, no support
promised, issues welcome.

Puck-diagnostic physics and threshold rationales are informed by
[gaggimate-mcp](https://github.com/julianleopold/gaggimate-mcp) (MIT) — the
resistance model, the idea of independent channeling signatures, the temperature
bands and the three-level detail system. The thresholds themselves are measured
against this archive rather than adopted: the sampling rates differ, and this
data carries per-point targets that theirs does not.

**Not affiliated with Decent Espresso International.** The name says what the
project is for, not who made it. Decent builds the DE1 and Decaid; this is an
independent community project by an owner of one of their machines, connected to
them only by reading Decaid's public API. It is not endorsed, reviewed or
supported by Decent Espresso, and any problem you have with it belongs in this
issue tracker rather than in theirs.
