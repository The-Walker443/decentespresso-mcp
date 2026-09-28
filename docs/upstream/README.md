# Upstream issue drafts

Findings from verifying this server against Decaid and DYE2 that belong with
the people who maintain them. **Drafts, not filed.** Each was measured on the
live instance; the finding number (T…) points into the specification's table.

## What to file - a strict triage

The operator files bugs only: reproducible, clearly wrong against the
documented behaviour, no design opinions. Reviewed once more on 2026-09-26 with
that filter.

**File as bugs (four):**

| Draft | Project | Why it is clearly a bug |
|---|---|---|
| 02 | decaid | Documented as "deletes a bean and all its batches"; answers 500 with a raw SQLite foreign-key error, deletes nothing |
| 03 | decaid | `name` and `roaster` are documented as required; empty strings are accepted and a nameless bean is created. A missing one fails with a Dart type-cast message |
| 09 | decaid | `timestamp` is not an editable field in `ShotUpdateRequest`, yet a PUT changes it; `createdAt`/`updatedAt` are refused as intended |
| 12 | streamline | The profile-drift guard compares `workflow.profile.id`, which Decaid never returns, so it can never fire - a guard that protects nothing  **Filed as streamline-js#90, fixed in fe73b4a (2026-09-28)** |

**Verify on the tablet first, then file if confirmed (two):**

- **08 (DYE2) - checked 2026-09-26, not filed.** On the tablet, with Blooming
  Espresso running, the operator applied "Seniman House Blend" through DYE2's
  Settings -> Favourites; afterwards the workflow ran a full D-Flow (6 steps),
  not Blooming Espresso with a new name. So DYE2's own apply path does change
  the profile, and the draft's central claim does not hold for DYE2's users.
  What was measured stays true for the API alone: a `PUT /workflow` with the
  stored `{id: null, title}` stub renames the running profile and keeps its
  steps (Adaptive v3 kept its steps under the title "D-Flow", same day). That
  only concerns a second client applying the stored item as it is - this
  server, which guards against it (T44). Nothing to report.
- **T45 (DYE2, in draft 08, third observation)** - six favourites store a bean
  id where a batch id belongs. That may be what an older DYE2 wrote. Check:
  open one of them in DYE2, save it again, and see whether the new entry stores
  a batch id (`list_recipes(source="dye2_favs")` shows it, or the store URL in
  the contract). If a fresh save still stores a bean id, file it; if it stores
  a batch id, the old entries are just stale and worth re-saving, not a report.

**Do not file - not bugs (six):**

| Draft | Why not |
|---|---|
| 01 | Content-addressed profile ids are a design choice; the 201 for existing content and the dropped title are arguably wrong, but a maintainer can fairly answer "by design". Debatable, so out |
| 04 | Missing reference validation and label derivation - a wish, not a defect. If T45 is confirmed, one sentence about the unchecked id belongs in that report instead |
| 05 | `weightRemaining` is documented as initialised on creation; nothing says it counts down. A question, not a bug |
| 06 | A documentation note |
| 07 | A feature proposal |
| 10 | Licence hygiene, not a bug |
| 11 | Streamline's auto-save writes DYE2's keys knowingly - the code comments say so. A policy disagreement between two projects, not a defect to report from outside |

**13 (decaid) - a gap, not a bug; the operator decides.** A workflow PUT
gives no way to know the profile reached the machine (T47). Nothing documented
is violated, so under the filter above it is a feature request - but it is the
one with a real consequence: a shot started too early runs the old profile and
is stored under the new one. Written because M12 asked for it.

The drafts stay here for reference; the ones to file are 02, 03, 09 and 12.

| # | Project | Finding | Draft |
|---|---|---|---|
| 1 | decaid | T39 | [A profile POST answers 201 for content that already exists](01-profile-post-201-for-existing-content.md) |
| 2 | decaid | T40 | [Deleting a bean with batches fails with a foreign-key 500](02-delete-bean-does-not-cascade.md) |
| 3 | decaid | T41 | [Creating a bean accepts an empty name and roaster; a missing one fails with a type-cast error](03-create-bean-type-cast-error.md) |
| 4 | decaid | T28, T29 | [The workflow's beanBatchId is neither checked nor resolved](04-workflow-bean-batch-unchecked.md) |
| 5 | decaid | T34 | [weightRemaining is initialised and never counted down](05-weight-remaining-static.md) |
| 6 | decaid | T32 | [Unset fields are omitted, not null - undocumented](06-unset-fields-omitted.md) |
| 7 | dye2 | KV contract | [A documented path for a second writer of recipes](07-dye2-external-writer-path.md) |
| 8 | dye2 | T42, T44, T45 | [A stored workflow.profile is often a name without a profile](08-dye2-workflow-profile-stub.md) |
| 9 | decaid | T26 | [PUT /api/v1/shots/{id} accepts arbitrary timestamp mutation](09-shot-timestamp-writable.md) |
| 10 | decaid | - | [LICENSE.txt references the full GPL text but does not include it](10-license-without-gpl-text.md) |
| 11 | streamline | KV contract | [Auto-save writes DYE2's keys, which the contract reserves for DYE2](11-streamline-autosave-writes-dye2-keys.md) |
| 12 | streamline | T38 | [The auto-save's profile-drift guard never fires](12-streamline-drift-guard-never-fires.md) |
| 13 | decaid | T47 | [A workflow PUT gives a client no way to know the profile reached the machine](13-workflow-put-no-upload-completion.md) |

Streamline's repository is `allofmeng/streamline_project`, which GitHub
redirects to `decentespresso/streamline-js`; drafts 11 and 12 refer to the
code at `721bd5fabe`.

**T34 stays** (draft 5): it may be intended, and the draft asks rather than
asserts. **T41 is an issue**, not a documentation note: an empty name and
roaster were accepted with 201 (measured, and deleted), which lets invalid data
into the store; the type-cast message is the lesser half.

Number 8 was found during the M11 acceptance, after the other seven were
written, and is the one with the most consequence for a consumer: following the
contract exactly can leave the machine brewing on a different profile than the
one it names.

Checked before drafting that none duplicates an existing item: the searches
turned up decaid #106, #201, #379, #450 and #501, all about other things.

T10 (`/shots/latest` dropping its measurements in 0.8.6) was a candidate and is
not here: `rest_v1.yml` documents it, so it is a deliberate change.

Environment: Decaid on an Android tablet with the DYE2 plugin as bundled.
Findings 1-3, 5, 7-10 were measured on **0.8.6+2801** (2026-09-26); 4 and 6 on
0.8.5+2624 (2026-09-14 to 2026-09-16) and are marked where they were not probed
again on 0.8.6.
