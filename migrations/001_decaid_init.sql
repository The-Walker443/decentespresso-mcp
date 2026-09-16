-- 001_decaid_init: the schema, with Decaid as the source (SPEC §5).
--
-- db.py applies the files here idempotently and refuses to open a database
-- written against a superseded schema. PRAGMAs are set on the connection, not
-- here - migrations stay pure DDL.

CREATE TABLE IF NOT EXISTS shots (
  id              TEXT PRIMARY KEY,          -- Decaid UUID or de1app-<unix time>
  started_at      TEXT NOT NULL,             -- ISO8601, always UTC
  -- Where the timestamp came from before it became UTC: 'utc' for shots
  -- imported from the de1app, 'local_berlin' for ones Decaid recorded itself.
  -- Decaid delivers both in the same field without a zone; without this column
  -- it could not be traced afterwards what was converted.
  time_source     TEXT NOT NULL CHECK (time_source IN ('utc', 'local_berlin')),
  created_at      TEXT,                      -- Decaid's createdAt, UTC
  updated_at      TEXT,                      -- Decaid's updatedAt, UTC; the sync cursor
  duration_s      REAL,
  stop_reason     TEXT,

  workflow_id     TEXT,
  profile_name    TEXT,
  profile_id      INTEGER REFERENCES profiles(id),

  -- A shot names only its batch; ingestion resolves bean_id through
  -- bean_batches. Plain text alongside, so an analysis stays readable even
  -- after a bean is renamed in Decaid.
  -- Deliberately without a foreign key: a shot must not fail to be archived
  -- because its batch was deleted in Decaid. The identifier then stays as a
  -- trace, and the plain text next to it carries the meaning.
  bean_batch_id   TEXT,
  bean_id         TEXT,
  bean_name       TEXT,
  bean_roaster    TEXT,

  grinder_model   TEXT,
  grinder_setting TEXT,                      -- free text, contains decimal commas
  basket_name     TEXT,

  target_dose_g   REAL,                      -- target, from the workflow
  target_yield_g  REAL,
  dose_g          REAL,                      -- actual, from the annotations
  yield_g         REAL,
  ratio           REAL,                      -- yield/dose, computed

  -- 0-100. NULL means "not rated". Decaid creates imported shots with 0.0;
  -- those zeros arrive here as NULL (see decaid_mapping).
  enjoyment       REAL CHECK (enjoyment IS NULL OR (enjoyment >= 0 AND enjoyment <= 100)),
  notes           TEXT,

  raw_json        TEXT NOT NULL,             -- the full response, without the series
  synced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_series (     -- one row per measurement point
  shot_id             TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  elapsed             REAL NOT NULL,         -- s from the first point (Decaid has no time field)

  pressure            REAL,
  flow_in             REAL,                  -- from the pump
  flow_out            REAL,                  -- derived from the scale
  weight              REAL,
  temp_mix            REAL,
  temp_basket         REAL,
  volume              REAL,

  -- Target per data point. This is what makes profile compliance measurable
  -- rather than estimated.
  target_pressure     REAL,
  target_flow         REAL,
  target_temp_mix     REAL,
  target_temp_basket  REAL,

  -- Phase provenance. substate is the dependable source for the end of
  -- preinfusion; profile_frame still holds the previous shot's value at the
  -- start and is ignored there (SPEC §8.2).
  state               TEXT,
  substate            TEXT,
  profile_frame       REAL,

  PRIMARY KEY (shot_id, elapsed)
);

CREATE TABLE IF NOT EXISTS beans (
  id         TEXT PRIMARY KEY,
  name       TEXT,
  roaster    TEXT,
  species    TEXT,
  processing TEXT,
  decaf      INTEGER,
  archived   INTEGER,
  notes      TEXT,
  created_at TEXT,
  updated_at TEXT,
  raw_json   TEXT NOT NULL,
  synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bean_batches (
  id            TEXT PRIMARY KEY,
  bean_id       TEXT,
  roast_date    TEXT,
  buy_date      TEXT,
  -- Being frozen does not count as ageing, so the bean-age guard subtracts
  -- that time (SPEC §10.2).
  freeze_date   TEXT,
  unfreeze_date TEXT,
  frozen        INTEGER,
  archived      INTEGER,
  created_at    TEXT,
  updated_at    TEXT,
  raw_json      TEXT NOT NULL,
  synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  version_hash  TEXT NOT NULL UNIQUE,        -- identity of one profile version
  semantic_hash TEXT,                        -- groups versions that brew alike
  -- Where the profile came from. 'decaid' is the JSON embedded in the
  -- workflow, which is the only source now.
  source        TEXT NOT NULL DEFAULT 'decaid',
  raw_json      TEXT NOT NULL,
  parsed_json   TEXT NOT NULL,
  profile_notes TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_metrics (
  shot_id         TEXT PRIMARY KEY REFERENCES shots(id) ON DELETE CASCADE,
  metrics_version INTEGER NOT NULL,
  metrics_json    TEXT NOT NULL,
  computed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (      -- last run, cursor, errors
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_shots_started ON shots(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_updated ON shots(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_bean ON shots(bean_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_batch ON shots(bean_batch_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_profile ON shots(profile_id);
CREATE INDEX IF NOT EXISTS idx_batches_bean ON bean_batches(bean_id);
CREATE INDEX IF NOT EXISTS idx_profiles_semantic ON profiles(semantic_hash);
CREATE INDEX IF NOT EXISTS idx_shot_metrics_version ON shot_metrics(metrics_version);
