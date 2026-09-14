-- 001_init: Grundschema (SPEC ss5).
-- Wird beim Start von db.py idempotent angewendet. PRAGMAs setzt db.py auf der
-- Verbindung, nicht hier - Migrationen bleiben reines DDL.

CREATE TABLE IF NOT EXISTS shots (
  id              TEXT PRIMARY KEY,          -- Visualizer-UUID
  started_at      TEXT NOT NULL,             -- ISO8601 UTC
  bean_brand      TEXT,
  bean_type       TEXT,
  bean_notes      TEXT,
  profile_name    TEXT,
  profile_id      INTEGER REFERENCES profiles(id),
  grinder_model   TEXT,
  grinder_setting TEXT,
  dose_g          REAL,
  yield_g         REAL,
  duration_s      REAL,
  ratio           REAL,                      -- yield/dose, berechnet
  drink_tds       REAL,
  drink_ey        REAL,
  enjoyment       INTEGER,
  notes           TEXT,                      -- espresso_notes (oeffentlich)
  private_notes   TEXT,                      -- nur fuer den Eigentuemer geliefert
  raw_json        TEXT NOT NULL,             -- kompletter API-Response
  updated_at      INTEGER NOT NULL,          -- Unix-Sekunden, Cursor fuer updated_after
  synced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_series (     -- 1 Zeile pro Messpunkt
  shot_id     TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  elapsed     REAL NOT NULL,
  pressure    REAL,
  flow_in     REAL,
  flow_out    REAL,
  weight      REAL,
  temp_mix    REAL,
  temp_basket REAL,
  state_change REAL,                         -- espresso_state_change, Phasenmarke fuer pi_end (M3)
  PRIMARY KEY (shot_id, elapsed)
);

CREATE TABLE IF NOT EXISTS profiles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,               -- title aus TCL
  version_hash  TEXT NOT NULL UNIQUE,        -- sha256 des normalisierten TCL
  raw_tcl       TEXT NOT NULL,
  parsed_json   TEXT NOT NULL,
  profile_notes TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (      -- Key-Value: letzter Lauf, Cursor, Fehler
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_shots_bean ON shots(bean_brand, bean_type, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_started ON shots(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_updated ON shots(updated_at DESC);
