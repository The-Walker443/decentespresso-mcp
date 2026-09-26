# Unset fields are omitted from responses, not returned as null - worth documenting

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801

## What happens

`rest_v1.yml` declares many fields `nullable: true`. In responses, an unset
field is not `null` - the key is absent. A batch with no `unfreezeDate`
returns no `unfreezeDate` key at all.

## Why it matters

A client reading responses to learn the shape concludes that the field does
not exist. We did exactly that, twice: we recorded `unfreezeDate` and
`weight`/`weightRemaining` as nonexistent, built around their absence, and
only found them when writing to them worked. One of the two cost a feature
(bean age stayed an upper bound where an exact value was available).

## Suggestion

Either return unset nullable fields as `null`, or say in `rest_v1.yml` that
unset fields are omitted and absence means null. The second costs one
sentence.
