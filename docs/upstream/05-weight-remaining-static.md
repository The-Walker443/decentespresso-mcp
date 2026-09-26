# weightRemaining is initialised from weight and never counted down

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801

## What happens

`POST /beans/{id}/batches` with `weight: 250` sets `weightRemaining: 250`, as
documented. After that nothing changes it. A batch with `weight: 500` read
`weightRemaining: 500` after 27 shots recorded against it with a total dose of
486 g.

## Question rather than bug

The description says "Remaining weight in grams (initialized to weight on
creation)" - which is exactly what happens, so this may be intended, with the
decrement left to a plugin or skin. If so, it would help to say that no
component of Decaid updates it; a consumer reading the field today cannot tell
a full bag from a bag nobody is tracking.

## Suggestion

Either decrement it by the recorded dose when a shot references the batch, or
document that it is a user-maintained field. We now report both figures side
by side - Decaid's and one derived from recorded doses - which is the safe
thing to do until the intended meaning is clear.
