# PUT /api/v1/shots/{id} accepts arbitrary timestamp mutation

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801 (first seen on 0.8.5+2624)

## What happens

A shot's `timestamp` - when it was pulled - can be overwritten through the
annotation endpoint. The request is accepted and the new value stands:

```bash
curl -s -X PUT $DECAID/api/v1/shots/$SHOT -H 'Content-Type: application/json' \
  -d '{"timestamp":"2020-01-01T00:00:00"}'          # 200
curl -s $DECAID/api/v1/shots/$SHOT | jq .timestamp   # "2020-01-01T00:00:00.000"
```

`id`, `createdAt`, `updatedAt` and `measurements` are refused with 400 on the
same endpoint ("system-managed"), so the protection exists - `timestamp` is
simply not on it.

## Why it matters

The timestamp is machine telemetry, not an annotation. Changed, it moves the
shot in every list sorted by time, detaches it from its measurements' own
clock (the measurement points keep theirs), and breaks anything keyed on when a
shot happened. A client bug - or a careless merge of a whole shot object into
an annotation update - rewrites history without any error.

In our own case a verification probe changed one shot's timestamp and the
original could only be recovered to within a few milliseconds, from the first
measurement point.

`rest_v1.yml` agrees that it should not be: `ShotUpdateRequest` lists
`annotations`, `stopReason` and the two deprecated aliases as the editable
fields, and `timestamp` is not among them.

## Suggestion

Refuse `timestamp` on `PUT /shots/{id}` with 400, like `createdAt` and
`updatedAt`.
