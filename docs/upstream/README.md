# Upstream issue drafts

Findings from verifying this server against Decaid and DYE2 that belong with
the people who maintain them. **Drafts, not filed.** Each was measured on the
live instance; the finding number (T…) points into the specification's table.

| # | Project | Finding | Draft |
|---|---|---|---|
| 1 | decaid | T39 | [A profile POST answers 201 for content that already exists](01-profile-post-201-for-existing-content.md) |
| 2 | decaid | T40 | [Deleting a bean with batches fails with a foreign-key 500](02-delete-bean-does-not-cascade.md) |
| 3 | decaid | T41 | [Creating a bean without name or roaster answers with a Dart type error](03-create-bean-type-cast-error.md) |
| 4 | decaid | T28, T29 | [The workflow's beanBatchId is neither checked nor resolved](04-workflow-bean-batch-unchecked.md) |
| 5 | decaid | T34 | [weightRemaining is initialised and never counted down](05-weight-remaining-static.md) |
| 6 | decaid | T32 | [Unset fields are omitted, not null - undocumented](06-unset-fields-omitted.md) |
| 7 | dye2 | KV contract | [A documented path for a second writer of recipes](07-dye2-external-writer-path.md) |
| 8 | dye2 | T42, T44 | [A stored workflow.profile is often a name without a profile](08-dye2-workflow-profile-stub.md) |

Number 8 was found during the M11 acceptance, after the other seven were
written, and is the one with the most consequence for a consumer: following the
contract exactly can leave the machine brewing on a different profile than the
one it names.

Checked before drafting that none duplicates an existing item: the searches
turned up decaid #106, #201, #379, #450 and #501, all about other things.

T10 (`/shots/latest` dropping its measurements in 0.8.6) was a candidate and is
not here: `rest_v1.yml` documents it, so it is a deliberate change.

Environment: Decaid on an Android tablet with the DYE2 plugin as bundled.
Findings 1-3, 5 and 7 were measured on **0.8.6+2801** (2026-09-26); 4 and 6 on
0.8.5+2624 (2026-09-14 to 2026-09-16) and are marked where they were not probed
again on 0.8.6.
