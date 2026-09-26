# The workflow's beanBatchId is neither checked nor resolved to its bean

**Project:** decentespresso/decaid · **Version:** measured on 0.8.5+2624, not yet
re-probed on 0.8.6+2801 - worth checking before filing

Two findings about the same field, reported together because a fix for one
touches the other.

## 1. Any string is accepted

`PUT /api/v1/workflow` with `{"context":{"beanBatchId":"00000000-0000-4000-8000-000000000000"}}`
answers 200, and the value stands on the next `GET`. There is no batch with
that id. A typo in a client leaves the workflow pointing at nothing, and every
shot pulled afterwards records that reference.

## 2. Setting the batch does not update the labels

`context` holds `beanBatchId` next to the display strings `coffeeName` and
`coffeeRoaster`. Changing only the batch leaves the previous coffee's name on
the machine - measured: after moving `beanBatchId` from an "Arabica Honey
Process" batch to a decaf batch, `coffeeName` still read "Arabica Honey
Process", and a shot pulled then would have been labelled with the wrong
coffee.

`rest_v1.yml`'s own example ("Link to managed grinder and bean batch") sends
the id and both labels together, so this may well be intended client
behaviour. If so, one sentence in the description would have saved us the
discovery.

## Suggestion

- Reject an unknown `beanBatchId` with 400 (or 404), as the batch and bean
  endpoints already do for unknown ids.
- Either derive `coffeeName`/`coffeeRoaster` from the batch when only the id is
  sent, or document that clients must send all three.
