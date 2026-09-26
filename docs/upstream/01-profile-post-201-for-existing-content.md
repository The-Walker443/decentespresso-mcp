# POST /api/v1/profiles answers 201 for content that already exists, and drops title, parentId and metadata

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801

## What happens

A profile's id is a hash of its brewing content (`ProfileRecord.id`), and the
title is not part of it. Posting a profile whose content already exists under
another title returns **201 Created** with the *existing* record. The new
`title`, the `parentId` and the `metadata` in the request are silently
discarded. Nothing is created.

## Reproduce

```bash
# any existing, non-default profile
curl -s $DECAID/api/v1/profiles/profile:198546fc983546de03b7 > p.json
jq '{profile: (.profile + {title: "A copy under a new name"}),
     parentId: .id, metadata: {note: "mine"}}' p.json > body.json
curl -si -X POST $DECAID/api/v1/profiles -H 'Content-Type: application/json' -d @body.json
```

Response: `201`, body `id: profile:198546fc983546de03b7`, `title: "D-Flow"`,
`metadata: {"brewTemperature": 90.5}` - the original, unchanged.

## Why it matters

A client that trusts the status code reports "created 'A copy under a new
name'" while the tablet lists nothing new. Deduplicating by content is a good
design; answering as if something was created is the problem.

## Suggestion

Either answer `200` with the existing record (it was found, not created), or
`409 Conflict` naming the existing id. Documenting the deduplication next to
`POST /profiles` in `rest_v1.yml` would help as well - today it is only
implied by the `id` description.
