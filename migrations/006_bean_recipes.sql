-- One recipe per bean (SPEC §11.6, M13).
--
-- A recipe is no longer an object someone names and pins; it is where the bean
-- was last dialled in: batch, grind, dose, yield and the profile as the
-- workflow ran it - embedded whole, so a profile tuned on the tablet lives in
-- the recipe itself and needs no stored copy. It is rewritten by every write
-- that changes the workflow, and keyed by the bean, so a new batch of the same
-- bean moves the recipe along instead of starting another.
--
-- `projected` is the dashboardVariables block last written into DYE2's recipe
-- list for this bean. A difference there means the tablet changed our entry
-- (Streamline's auto-save writes dose, drink and grind into it) - that is how a
-- dial-in on the tablet reaches the recipe without a step in the chat.

CREATE TABLE IF NOT EXISTS bean_recipes (
  bean_id         TEXT PRIMARY KEY,
  bean_batch_id   TEXT,
  grinder_setting TEXT,
  grinder_model   TEXT,
  dose_g          REAL,
  yield_g         REAL,
  profile_json    TEXT NOT NULL,     -- the workflow's profile, whole
  projected       TEXT,              -- JSON: dashboardVariables as last projected
  updated_at      TEXT NOT NULL
);

-- What there was: named recipes, several per bean possible. Per bean the one
-- changed last carries over, its snapshot as the profile. Recipes without a
-- bean cannot be keyed to one and are dropped.
INSERT OR IGNORE INTO bean_recipes
  (bean_id, bean_batch_id, grinder_setting, grinder_model, dose_g, yield_g,
   profile_json, updated_at)
SELECT r.bean_id, r.bean_batch_id, r.grind, r.grinder_model, r.dose_g, r.yield_g,
       r.profile_snapshot, r.updated_at
FROM recipes r
WHERE r.bean_id IS NOT NULL
  AND r.updated_at = (SELECT MAX(o.updated_at) FROM recipes o WHERE o.bean_id = r.bean_id);

-- The unsaved-profile guard is gone, and with it what it remembered.
DROP TABLE IF EXISTS recipes;
DROP TABLE IF EXISTS superseded_profiles;
