-- Recipes of our own: a coffee and how it is brewed, kept here and not in
-- Decaid's plugin store.
--
-- The store's recipe keys belong to DYE2 (dye2.reaplugin/recipes). Its
-- contract (docs/KV_CONTRACT.md in decentespresso/dye2) makes DYE2 the single
-- writer and every other consumer read-only, and the store has no ETag and no
-- field-level write - a second writer would clobber concurrent DYE2 edits
-- without either side noticing. So DYE2's recipes are read from there and ours
-- live here, shaped after the contract's items so the two read alike.
--
-- The profile is referenced by title and snapshotted besides. A profile's id is
-- its content (SPEC T37) and changes with every tuning, so an id would point at
-- nothing after the first update_profile; the title follows the tuning. The
-- snapshot is what `pin_profile` freezes on, and what is left if the title ever
-- stops resolving.

CREATE TABLE IF NOT EXISTS recipes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
  bean_id          TEXT,
  bean_name        TEXT,              -- denormalised, as the contract does
  bean_roaster     TEXT,
  bean_batch_id    TEXT,
  profile_title    TEXT NOT NULL,
  profile_snapshot TEXT NOT NULL,     -- JSON: the profile as captured or last re-pinned
  pin_profile      INTEGER NOT NULL DEFAULT 0,
  dose_g           REAL,              -- dashboardVariables.dose
  yield_g          REAL,              -- dashboardVariables.drink
  grind            TEXT,              -- dashboardVariables.grind, free text as typed
  grinder_model    TEXT,
  captured_at      TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

-- Profile contents this server replaced on purpose, by update_profile.
--
-- The unsaved-profile guard protects a workflow profile that exists nowhere
-- else. After update_profile the workflow still runs the previous version, and
-- that version exists nowhere else too - Decaid replaced the record (T37). The
-- guard then blocked the very set_workflow update_profile offers. A content
-- recorded here was given up deliberately and is not what the guard is for;
-- a profile tuned on the tablet never lands in this table.
CREATE TABLE IF NOT EXISTS superseded_profiles (
  brew_hash    TEXT PRIMARY KEY,     -- sha256 of the normalised brewing content
  title        TEXT,
  replaced_by  TEXT,                 -- the profile id it became
  replaced_at  TEXT NOT NULL
);
