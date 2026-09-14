-- 001_decaid_init: Grundschema mit Decaid als Quelle (SPEC ss20.4).
--
-- Frischer Anfang. Die Migrationen der Visualizer-Aera liegen unter
-- migrations/visualizer-era/ und werden nicht mehr angewendet; es gibt bewusst
-- keinen Weg von dort hierher, weil Decaid die vollstaendigeren Daten hat und
-- bis zum 2026-06-24 zurueckreicht. Was aus der alten Ablage erhaltenswert war
-- - Notizen und Bewertungen -, hat scripts/migrate_visualizer_annotations.py
-- vorher nach Decaid uebertragen.
--
-- db.py wendet die Dateien hier idempotent an und weigert sich, eine Datei aus
-- der Visualizer-Aera zu oeffnen. PRAGMAs setzt db.py auf der Verbindung.

CREATE TABLE IF NOT EXISTS shots (
  id              TEXT PRIMARY KEY,          -- Decaid-UUID oder de1app-<unixzeit>
  started_at      TEXT NOT NULL,             -- ISO8601, immer UTC
  -- Woher der Zeitstempel kam, bevor er UTC wurde: 'utc' fuer aus der de1app
  -- importierte Bezuege, 'local_berlin' fuer von Decaid aufgezeichnete. Decaid
  -- liefert beides ohne Zeitzonenangabe im selben Feld; ohne diese Spalte
  -- waere spaeter nicht mehr nachvollziehbar, was umgerechnet wurde.
  time_source     TEXT NOT NULL CHECK (time_source IN ('utc', 'local_berlin')),
  created_at      TEXT,                      -- Decaids createdAt, UTC
  updated_at      TEXT,                      -- Decaids updatedAt, UTC; Sync-Cursor
  duration_s      REAL,
  stop_reason     TEXT,

  workflow_id     TEXT,
  profile_name    TEXT,
  profile_id      INTEGER REFERENCES profiles(id),

  -- Der Bezug nennt nur die Charge; bean_id loest die Ingestion ueber
  -- bean_batches auf. Klartext daneben, damit eine Auswertung auch dann
  -- lesbar bleibt, wenn eine Bohne in Decaid spaeter umbenannt wird.
  -- Bewusst ohne Fremdschluessel: ein Bezug darf nicht am Archiv scheitern,
  -- weil seine Charge in Decaid inzwischen geloescht wurde. Die Kennung bleibt
  -- dann als Spur stehen, der Klartext daneben traegt die Aussage.
  bean_batch_id   TEXT,
  bean_id         TEXT,
  bean_name       TEXT,
  bean_roaster    TEXT,

  grinder_model   TEXT,
  grinder_setting TEXT,                      -- Freitext, enthaelt Kommazahlen
  basket_name     TEXT,

  target_dose_g   REAL,                      -- Soll aus dem Workflow
  target_yield_g  REAL,
  dose_g          REAL,                      -- Ist aus den Annotationen
  yield_g         REAL,
  ratio           REAL,                      -- yield/dose, berechnet

  -- 0-100. NULL heisst "nicht bewertet". Decaid legt importierte Bezuege mit
  -- 0.0 an; diese Nullen kommen als NULL hier an (siehe decaid_mapping).
  enjoyment       REAL CHECK (enjoyment IS NULL OR (enjoyment >= 0 AND enjoyment <= 100)),
  notes           TEXT,

  raw_json        TEXT NOT NULL,             -- vollstaendige Antwort ohne Messreihe
  synced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_series (     -- 1 Zeile pro Messpunkt
  shot_id             TEXT NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  elapsed             REAL NOT NULL,         -- s ab erstem Messpunkt (Decaid hat kein time-Feld)

  pressure            REAL,
  flow_in             REAL,                  -- Pumpe
  flow_out            REAL,                  -- aus der Waage abgeleitet
  weight              REAL,
  temp_mix            REAL,
  temp_basket         REAL,
  volume              REAL,

  -- Soll je Messpunkt. Gab es in der Visualizer-Aera nicht; darauf beruht der
  -- Soll-Ist-Vergleich in curve_shape.
  target_pressure     REAL,
  target_flow         REAL,
  target_temp_mix     REAL,
  target_temp_basket  REAL,

  -- Phasenherkunft. substate ist die verlaessliche Quelle fuer das Ende der
  -- Praeinfusion; profile_frame steht am Shot-Anfang noch auf dem Wert des
  -- Vorgaengers und wird dort ignoriert (SPEC ss20.5).
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
  -- Eingefroren zaehlt nicht als Alterung. Das Bohnenalter rechnet der
  -- Waechter in M8 (4/n) darum ohne die Gefrierzeit.
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
  version_hash  TEXT NOT NULL UNIQUE,        -- Identitaet einer Profilversion
  semantic_hash TEXT,                        -- gruppiert Versionen, die gleich bruehen
  -- Quelle des Profils: 'decaid' ist das im Workflow eingebettete JSON. 'tcl'
  -- bleibt nur fuer Altbestand lesbar (tcl_profile.py ist ab M8 ueberholt).
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

CREATE TABLE IF NOT EXISTS sync_state (      -- letzter Lauf, Cursor, Fehler
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
