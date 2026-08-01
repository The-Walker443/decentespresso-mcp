# visualizer-mcp

MCP-Server, der Espresso-Bezuege der Decent DE1 von
[visualizer.coffee](https://visualizer.coffee) lokal archiviert (SQLite) und Claude
per Custom Connector zur Analyse bereitstellt.

Vollstaendige Spezifikation: [SPEC_visualizer-mcp.md](SPEC_visualizer-mcp.md).

## Stand: Milestone M1 (Client, DB, Backfill)

Vorhanden:

- Konfiguration aus Env inkl. Startup-Validierung (`config.py`)
- Strukturiertes key=value-Logging auf stdout mit Secret-Redaction (`logging_setup.py`)
- FastMCP-Server (Streamable HTTP) unter `/<MCP_PATH_SECRET>/mcp`, `/healthz`,
  alle anderen Pfade `404` ohne Body (`server.py`)
- Visualizer-Client mit Basic Auth, Ratelimiter, ETag und Backoff
  (`visualizer_client.py`)
- SQLite mit Migrationsrunner, Upserts, Sync-Zustand (`db.py`)
- Sync-Worker: Backfill ueber alle Seiten, inkrementell via `updated_after`,
  Hintergrundschleife alle `SYNC_INTERVAL_MIN` (`sync.py`)
- `status`-Tool mit echten Bestandszahlen

Noch nicht vorhanden: TCL-Parser und Profilversionierung (M2), Metriken (M3), die
Analyse-Tools aus SPEC ss9.2 (M4). Siehe SPEC ss14.

## Visualizer-API — verifizierter Stand

Gegen <https://apidocs.visualizer.coffee/> geprueft am 2026-08-01
(OpenAPI 3.1, Visualizer API v1.15.0), zusaetzlich mit echten Requests bestaetigt:

| Endpunkt | Liefert |
|---|---|
| `GET /api/me` | `{id, name, public, avatar_url}` — prueft die Credentials |
| `GET /api/shots?page=&items=` | pro Shot **nur** `{id, clock, updated_at}` plus `paging{count,page,limit,pages}` |
| `GET /api/shots/{id}` | volles Detail inkl. `timeframe[]` und `data.espresso_*[]` |
| `GET /api/shots/{id}/profile` | Roh-TCL, `application/x-tcl` |
| `GET /api/shots/{id}/profile?format=json` | von Visualizer geparstes Profil |

Was dabei anders ist als in der Spec angenommen:

- **Zeitreihen kommen als Strings**, nicht als Zahlen — auch `timeframe`.
- **`updated_after` (Unix-Sekunden) + `sort=updated_at` existieren.** Der
  inkrementelle Sync nutzt das statt der Seitenheuristik aus SPEC ss6.2 und
  findet damit auch nachtraeglich geaenderte Shots.
- **ETag/`If-None-Match` liefert 304** auf Liste und Detail.
- **Ratelimits:** 50 req/min pro IP, 200 req/10 min pro IP und pro Nutzer. Der
  Client haelt mit einem Sliding-Window-Limiter Abstand (40/min, 170/10 min).
- **Die Dosis steht im Detail-JSON** als `bean_weight` — der Vorbehalt in
  SPEC ss7.2 gilt nur fuer den CSV-Weg.
- **`brewdata` ist bei DE1-Uploads leer**; das Profil braucht einen eigenen Request.
- Leere Felder (`private_notes`, `metadata`) fehlen im Response komplett.

Das vollstaendige Feldmapping steht als Tabelle im Docstring von
`shot_row_from_detail()` in [visualizer_client.py](src/visualizer_mcp/visualizer_client.py).

## Entwicklung

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"    # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check src tests
```

Lokal starten (ohne Docker):

```bash
cp .env.example .env    # ausfuellen, MCP_PATH_SECRET erzeugen
set -a; . ./.env; set +a
python -m visualizer_mcp
```

## Deployment

```bash
cp .env.example .env
openssl rand -hex 24            # -> MCP_PATH_SECRET
mkdir -p data && sudo chown -R 10001:10001 data   # Container laeuft als UID 10001
docker compose build && docker compose up -d
docker compose logs -f
```

`compose.yaml` haengt den Container ins externe Netz `cloudflared_net` und
veroeffentlicht bewusst **keinen** Host-Port. Netzname ggf. an den vorhandenen
Tunnel-Stack anpassen.

Cloudflare-Tunnel-Ingress ergaenzen:

```yaml
ingress:
  - hostname: coffee-mcp.example.com
    service: http://visualizer-mcp:8000
  # ...bestehende Regeln...
  - service: http_status:404
```

`/healthz` sollte nicht ueber den Tunnel erreichbar sein (SPEC ss10.1) — der
Docker-Healthcheck spricht den Container direkt an.

### Connector-URL

Die URL enthaelt das Secret und wird deshalb **nicht** beim Start geloggt. Bei
Bedarf abrufen:

```bash
docker compose exec visualizer-mcp visualizer-mcp --print-connector-url
```

Ergibt `https://<host>/<MCP_PATH_SECRET>/mcp` — diese URL in claude.ai unter
Einstellungen → Connectors → „Add custom connector" eintragen, OAuth-Felder leer.

### Sync von Hand

Der Server synchronisiert im Hintergrund selbst; der erste Lauf ist automatisch ein
Backfill. Manuell:

```bash
docker compose exec visualizer-mcp visualizer-mcp --backfill    # alle Seiten
docker compose exec visualizer-mcp visualizer-mcp --sync-once   # nur Neues/Geaendertes
```

Beide geben eine JSON-Zusammenfassung aus und beenden sich mit Exit-Code 1, wenn
einzelne Shots fehlgeschlagen sind. Bei Fehlern wird der Cursor **nicht**
weitergeschoben — sonst bliebe ein fehlgeschlagener Shot dauerhaft ungeholt.

## Konfiguration

| Variable | Default | Bedeutung |
|---|---|---|
| `VISUALIZER_EMAIL` | — | Visualizer-Login (Basic Auth), landet auch im User-Agent |
| `VISUALIZER_PASSWORD` | — | Visualizer-Passwort |
| `MCP_PATH_SECRET` | — | ≥32 Zeichen `[A-Za-z0-9_-]`, ersetzt die Authentifizierung |
| `SYNC_INTERVAL_MIN` | `15` | Poll-Intervall in Minuten (0–1440); `0` schaltet die Hintergrundschleife ab |
| `DB_PATH` | `/data/shots.db` | SQLite-Datei, muss absolut sein |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `TZ` | `Europe/Berlin` | Anzeige-Zeitzone; gespeichert wird immer UTC |
| `PUBLIC_BASE_URL` | — | nur fuer `--print-connector-url` |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind-Adresse im Container |

Fehlt etwas oder ist es unplausibel, startet der Server nicht und listet **alle**
Probleme auf einmal auf.

## Sicherheit

- Kein Secret in Logs oder Tool-Antworten: die Redaction sitzt im Log-Formatter und
  erfasst auch Tracebacks und Fremdbibliotheken. Getestet in
  `tests/test_logging_redaction.py`.
- uvicorn-Access-Logs sind aus — sie wuerden den Secret-Pfad jeder Anfrage
  protokollieren.
- Container: non-root (UID 10001), `read_only: true`, `no-new-privileges`, nur
  `/data` und `/tmp` beschreibbar.

## Abweichungen vom Datenmodell der Spec

Gegenueber SPEC ss5 hat `001_init.sql` drei Ergaenzungen. Alle drei wurden vor dem
ersten Backfill eingezogen, damit spaeter kein Re-Sync noetig wird:

- `shots.private_notes` — Visualizer liefert dem Eigentuemer ein eigenes Notizfeld
  neben `espresso_notes`. Ohne Spalte waere die interessantere der beiden Notizen
  nur im `raw_json` gelandet.
- `shots.updated_at` — Unix-Sekunden, dient als Cursor fuer `updated_after` und
  als Erkennung, ob ein bekannter Shot neu geladen werden muss.
- `shot_series.state_change` — `espresso_state_change` ist die Phasenmarke, die
  SPEC ss8 fuer `pi_end` bevorzugt. Der Sentinel `-10000000.0` wird zu `NULL`.

Ausserdem: `drink_tds`, `drink_ey` und `enjoyment` werden auf `NULL` gesetzt, wenn
Visualizer `0` liefert — das heisst dort „nicht erfasst", und eine TDS von 0 %
wuerde jede Auswertung verzerren.

## Backup

```bash
sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"
```

Als naechtlichen Host-Cronjob einrichten, 7 Tage Rotation.
