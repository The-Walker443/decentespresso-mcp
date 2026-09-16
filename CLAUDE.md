# Working on decentespresso-mcp

Conventions for this repository. They are not style preferences - each one comes
out of something that went wrong here at least once.

## Language

Everything in this repository is written in **English**: code, docstrings,
comments, log and error messages, validation texts, tests, documentation and
commit messages. The one exception is user data - notes, bean names and anything
else the owner typed - which is never translated. Changelog entries written
before the switch stay in German; new ones are English.

## Verify before you build

The spec describes intent. The API describes reality. Where they disagree,
reality wins and the spec gets corrected, never the other way round.

Before implementing against any external interface, probe it and write the
findings down in the spec as a numbered table (T1, T2, ...). Every deviation
from the brief gets reported to the user with its evidence, not silently built
around. This has paid for itself repeatedly:

- `/api/v1/batches` does not exist; the path is `/api/v1/bean-batches`.
- The shot list caps silently at 100 items.
- `actualDoseWeight` is always exactly equal to `targetDoseWeight`, which made a
  whole planned guard rule meaningless.

### Absence in a response proves nothing

**A field counts as nonexistent only once a write to it was refused.** A field
missing from a read response proves nothing: Decaid leaves unset fields out
entirely rather than sending `null` (T32). To establish that something does not
exist, write to it and see it rejected - or find it absent from
`assets/api/rest_v1.yml`, the canonical description.

Two findings were recorded as verified and were wrong in exactly this way:

- **T23** claimed Decaid keeps no thaw date. `unfreezeDate` exists and is
  writable. The wrong finding cost the archive the one fact that turns bean age
  from an upper bound into an exact number, and it was enshrined in a block
  list, a test and the spec.
- **T27** claimed a batch carries no weight. `weight` and `weightRemaining`
  exist. A whole feature was dropped on that basis.

Both were read off a response where the field happened to be unset. Neither
survived five minutes of writing to it.

### Mutating probes and tests against live data

Some things can only be learned by writing. Probing and testing write paths
against the operator's real Decaid data is **allowed**: a clean verification of
what the API actually does is worth more here than an untouched history. Two
duties remain.

**Snapshot and restore where you can.** Read the current value first, write the
probe, read back to see what the API did, then put the original back and read
that back too. Best effort - if a value cannot be recovered, that is not a
reason to skip the probe, only a reason to say so.

**Report everything you changed.** Every report lists, in full, which entities
were touched, which fields on them, and whether they are restored. No
exceptions, not even when the restore succeeded and nothing appears to have
happened.

The reporting duty exists because of one incident: during the M8 verification a
probe set a shot's `timestamp` to `2020-01-01`. The field turned out to be
writable - a finding worth having, and one that changed the code, because for
telemetry fields the whitelist in `writes.py` is the only protection rather than
a second line of defence. The original was recoverable only to within a few
milliseconds, from the first measurement point. Everything observable came back,
but that was luck. Luck is not something a later reader can verify; a list of
what was touched is.

## Writing to the archive

- A whitelist per endpoint. A field goes on it only after it was written against
  the real API **and read back**. Better one field too few than one that gets
  silently discarded.
- Always read back after writing. A 200 does not prove a field was taken.
  Report what came back, not what was sent.
- Blocked fields carry a reason, not just a refusal.
- Write tools live behind `WRITE_ENABLED`. When it is off they do not exist -
  they are absent from the tool list rather than present and refusing.

## Tests

The test suite is the acceptance. A change is done when the suite is green, ruff
is clean, and no intermediate state was committed.

- Tests name what they protect, in the docstring, with the reason. `"""Otherwise
  every batch would keep reporting until someone touches the grinder."""` is
  worth more than the test name.
- Numbers pinned in tests come from measurement, and the comment says where.
- Fixtures are anonymised real responses, never invented ones.
- When behaviour and its test change together, they go in the same commit.

## Response economy

The tool definitions travel with every request. The telling figure is the size
**per tool**, not the sum: more capability necessarily costs more, verbosity
does not. Shared semantics belong in the server `INSTRUCTIONS`, which are in
context once, not repeated in every docstring.

## Guards

Guard rules are pure functions over rows - no network, no database, and the
clock is handed in. A finding never carries free text: findings leave the house
over ntfy, and notes stay here.

A rule that fires on most of the archive is broken, not strict. Both binding
corrections in M8 came from measuring that: `missing_rating` reported 83 % of
all shots until it got an upper bound, and the dose rule compared a number with
itself.

## Commits

One load-bearing block per commit, not half of everything. The message says what
changed and **why**, names deviations from the brief with their evidence, and
reports own mistakes found along the way. End with:

```
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
```

## Data that must not be touched

- User data: notes, ratings, bean names.
- Git history: no rewrites.
- `shots-visualizer-era.db` keeps its filename - it names exactly where the data
  came from.
