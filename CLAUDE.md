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

### Mutating probes

Some things can only be learned by writing. When a probe has to change real
data:

1. **Read the current value first** and keep it.
2. Write a distinctive, reversible probe value.
3. **Read back** to see what the API actually did.
4. **Restore the original immediately**, and read back again to confirm the
   restore.
5. Report what was touched, even when the restore succeeded.

Never probe a field whose original value cannot be recovered. During the M8
verification a probe set a shot's `timestamp` to `2020-01-01`; the field turned
out to be writable, and the original was only recoverable to within a few
milliseconds from the first measurement point. Everything observable came back,
but that was luck, not method.

The finding itself was worth having - Decaid refuses `id` and `createdAt` with
400 but accepts `timestamp` - and it changed the code: for telemetry fields the
whitelist in `writes.py` is the only protection, not a second line of defence.

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
