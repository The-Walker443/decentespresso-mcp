-- 003: Cache fuer die abgeleiteten Metriken (SPEC ss8).
--
-- Eigene Tabelle statt Spalten in shots: die Metriken aendern sich mit ihrer
-- Definition, nicht mit den Rohdaten. metrics_version macht eine Neuberechnung
-- nach einer Definitionsaenderung selbsttaetig - der Cache gilt nur fuer die
-- Version, mit der er entstanden ist.

CREATE TABLE IF NOT EXISTS shot_metrics (
  shot_id         TEXT PRIMARY KEY REFERENCES shots(id) ON DELETE CASCADE,
  metrics_version INTEGER NOT NULL,
  metrics_json    TEXT NOT NULL,
  computed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shot_metrics_version ON shot_metrics(metrics_version);
