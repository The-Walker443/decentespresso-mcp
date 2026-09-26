# POST /api/v1/beans accepts an empty name and roaster, and a missing one fails with a type-cast error

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801

`name` and `roaster` are required (`rest_v1.yml`). Two things about how that is
enforced.

## 1. Empty strings pass - a bean without a name is created

```bash
curl -si -X POST $DECAID/api/v1/beans -H 'Content-Type: application/json' \
  -d '{"name":"","roaster":""}'
```

`201` with `{"name":"","roaster":"", ...}` - a bean that shows as a blank row
in every list and can only be told apart by its id. Measured 2026-09-26 and
deleted straight after.

## 2. A missing field fails with an implementation detail

```bash
curl -si -X POST $DECAID/api/v1/beans -H 'Content-Type: application/json' -d '{"name":"x"}'
```

`400 {"error":"type 'Null' is not a subtype of type 'String' in type cast"}` -
refused, which is right, but it does not say which field is missing. Nothing is
created (checked).

## Suggestion

Validate before parsing: required means present and non-empty after trimming,
and the refusal names the field, e.g.
`400 {"error":"Invalid request","message":"roaster is required"}`. The same
pattern may exist on other create endpoints; beans were the ones probed.

## Assessment

Worth an issue rather than a documentation note: (1) lets invalid data into
the store, and the documented contract ("required") already says what should
happen. (2) alone would be a polish item.
