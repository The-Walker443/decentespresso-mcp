# DELETE /api/v1/beans/{id} fails with a foreign-key 500 when the bean has batches

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801

## What happens

`rest_v1.yml` says the endpoint "permanently deletes a bean and all its
batches". With at least one batch, it answers **500**:

```
{"error":"SqliteException(787): while executing statement, FOREIGN KEY
constraint failed, constraint failed (code 787)
  Causing statement: DELETE FROM "beans" WHERE "id" = ?; ..."}
```

Nothing is deleted - bean and batch both still answer 200 afterwards.

## Reproduce

```bash
BEAN=$(curl -s -X POST $DECAID/api/v1/beans -H 'Content-Type: application/json' \
  -d '{"name":"cascade-test","roaster":"cascade-test"}' | jq -r .id)
curl -s -X POST $DECAID/api/v1/beans/$BEAN/batches -H 'Content-Type: application/json' -d '{}'
curl -si -X DELETE $DECAID/api/v1/beans/$BEAN        # 500
```

Deleting the batch first and then the bean works (both 200).

## Suggestion

Delete the bean's batches in the same transaction (`ON DELETE CASCADE` on the
foreign key, or explicitly), as documented - or, if keeping batches is
intended, answer 409 with a message and correct the description.
