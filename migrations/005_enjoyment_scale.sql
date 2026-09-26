-- Ratings move to Decaid's own 0-10 scale (SPEC T46).
--
-- decentespresso/decaid#887 turns annotations.enjoyment from a pass-through of
-- de1app's and Visualizer's 0-100 into Decaid's own 0-10 field, and its schema-6
-- migration rescales what is stored. Everything archived so far was read from a
-- tablet on 0-100, so it is converted here by the same rule, and the archive
-- then holds what the tablet will hold after its update:
--
--   * over 10: divided by ten, always - it cannot be a 0-10 rating;
--   * 10 and below: divided only on an untouched de1app import (id prefix
--     `de1app-`, createdAt != timestamp, updatedAt <= createdAt), read off the
--     stored response exactly as Decaid reads its own columns;
--   * anything else from 1 to 10 is left as it is and marked ambiguous: a low
--     0-100 rating, DYE2's raw star index (dye2#7) and a 0-10 write by DYE2
--     0.1.15 on a 0.8.6 tablet all look the same. Decaid does not guess either.
--
-- 0 is 0 on both scales. The CHECK on `enjoyment` still reads 0-100; SQLite
-- cannot narrow a CHECK without rebuilding the table, and the code keeps 0-10.

ALTER TABLE shots ADD COLUMN enjoyment_ambiguous INTEGER NOT NULL DEFAULT 0;

-- One statement: SET sees the old values on both sides, so the flag is
-- decided on the value before it was divided.
UPDATE shots SET
  enjoyment_ambiguous = CASE
    WHEN enjoyment > 0 AND enjoyment <= 10 AND NOT (
      id LIKE 'de1app-%'
      AND json_extract(raw_json, '$.createdAt') IS NOT NULL
      AND json_extract(raw_json, '$.updatedAt') IS NOT NULL
      AND json_extract(raw_json, '$.createdAt') != json_extract(raw_json, '$.timestamp')
      AND json_extract(raw_json, '$.updatedAt') <= json_extract(raw_json, '$.createdAt')
    ) THEN 1 ELSE 0 END,
  enjoyment = CASE
    WHEN enjoyment > 10 OR (enjoyment > 0 AND
      id LIKE 'de1app-%'
      AND json_extract(raw_json, '$.createdAt') IS NOT NULL
      AND json_extract(raw_json, '$.updatedAt') IS NOT NULL
      AND json_extract(raw_json, '$.createdAt') != json_extract(raw_json, '$.timestamp')
      AND json_extract(raw_json, '$.updatedAt') <= json_extract(raw_json, '$.createdAt')
    ) THEN MIN(ROUND(enjoyment / 10.0, 3), 10.0)
    ELSE enjoyment END
WHERE enjoyment IS NOT NULL;
