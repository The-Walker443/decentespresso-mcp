-- What a batch and a bean carry beyond dates, once somebody fills it in.
--
-- All of it existed in the API the whole time and went unnoticed because every
-- batch in this archive was empty. The moment one was filled in properly, seven
-- fields appeared that had no column: roast level, harvest season, cupping
-- score, price, currency, and the batch's own notes, plus decafProcess on the
-- bean.
--
-- harvest_date is TEXT and not a date: the API documents it as "harvest date or
-- season" and the real value is "2026". Parsing that as a date would either
-- fail or invent a first of January.

ALTER TABLE bean_batches ADD COLUMN roast_level   TEXT;
ALTER TABLE bean_batches ADD COLUMN harvest_date  TEXT;  -- a year or a season
ALTER TABLE bean_batches ADD COLUMN quality_score REAL;  -- cupping score
ALTER TABLE bean_batches ADD COLUMN price         REAL;
ALTER TABLE bean_batches ADD COLUMN currency      TEXT;
ALTER TABLE bean_batches ADD COLUMN notes         TEXT;

ALTER TABLE beans ADD COLUMN decaf_process TEXT;
