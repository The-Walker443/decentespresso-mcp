-- Beanie's freeze/thaw history of a batch (SPEC T57; M14).
--
-- Beanie keeps it as `extras.storageEvents` ([{type: "frozen"|"thawed", at:
-- ISO}], in order) and sets neither freezeDate nor unfreezeDate. Kept here as
-- the JSON array it arrives as; freezing.py reads it where the fields are unset.
ALTER TABLE bean_batches ADD COLUMN storage_events TEXT;
