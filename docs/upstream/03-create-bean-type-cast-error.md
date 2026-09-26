# POST /api/v1/beans without name or roaster answers with a Dart type-cast error

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801

## What happens

Both fields are required (`rest_v1.yml`), and omitting either is refused - good.
The refusal reads:

```
400 {"error":"type 'Null' is not a subtype of type 'String' in type cast"}
```

It does not say which field is missing, and it exposes an implementation
detail rather than a validation result.

## Reproduce

```bash
curl -si -X POST $DECAID/api/v1/beans -H 'Content-Type: application/json' -d '{"name":"x"}'
curl -si -X POST $DECAID/api/v1/beans -H 'Content-Type: application/json' -d '{"roaster":"x"}'
```

Both: the message above. Nothing is created (checked).

## Suggestion

Validate before parsing and answer like other endpoints do, e.g.
`400 {"error":"Invalid request","message":"roaster is required"}`. The same
pattern - a cast error where a field is missing - may exist on other create
endpoints; this one was the one probed.

An empty string passes the cast and is stored as a bean without a name, which
a check for "non-empty" would catch at the same place.
