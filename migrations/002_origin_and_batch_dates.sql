-- Origin on the bean, the remaining dates and weights on the batch.
--
-- None of this is new in Decaid; the archive simply had no column for it, so
-- the data arrived in raw_json and stopped there. Two consequences that were
-- reported as bugs: a bean's country was invisible, and `buy_date` stayed null
-- because Decaid sends `openDate` for the day a bag was opened.
--
-- Arrays are stored as JSON text. SQLite has no array type, the values are
-- short, and nothing here is ever filtered on - so a column that round-trips
-- the API shape beats a join table nobody queries.

ALTER TABLE beans ADD COLUMN country   TEXT;
ALTER TABLE beans ADD COLUMN region    TEXT;
ALTER TABLE beans ADD COLUMN producer  TEXT;
ALTER TABLE beans ADD COLUMN variety   TEXT;  -- JSON array of strings
ALTER TABLE beans ADD COLUMN altitude  TEXT;  -- JSON [min, max] in metres

ALTER TABLE bean_batches ADD COLUMN open_date          TEXT;
ALTER TABLE bean_batches ADD COLUMN best_before_date   TEXT;
-- Both exist in the API (BeanBatch.weight / .weightRemaining) and are simply
-- unset on this machine. They are carried so that a later stock estimate has
-- a basis, and so that "no weight recorded" is distinguishable from "no such
-- field" - a distinction an earlier verification got wrong.
ALTER TABLE bean_batches ADD COLUMN weight_g           REAL;
ALTER TABLE bean_batches ADD COLUMN weight_remaining_g REAL;
