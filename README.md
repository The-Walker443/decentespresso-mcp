# visualizer-mcp

MCP-Server, der Espresso-Bezuege der Decent DE1 von
[visualizer.coffee](https://visualizer.coffee) lokal archiviert (SQLite) und Claude
per Custom Connector zur Analyse bereitstellt.

Vollstaendige Spezifikation: [SPEC_visualizer-mcp.md](SPEC_visualizer-mcp.md).

## Stand: Milestone M0 (Geruest)

Vorhanden:

- Konfiguration aus Env inkl. Startup-Validierung (`config.py`)
- Strukturiertes key=value-Logging auf stdout mit Secret-Redaction (`logging_setup.py`)
- FastMCP-Server (Streamable HTTP) unter `/<MCP_PATH_SECRET>/mcp`, `/healthz`,
  alle anderen Pfade `404` ohne Body (`server.py`)
- `status`-Tool (Platzhalter, meldet ehrlich `milestone: M0`)
- Datenbankschema als `migrations/001_init.sql` (noch nicht angewendet)
- Dockerfile + compose.yaml, non-root, read-only Root-FS

Noch nicht vorhanden: Visualizer-Client, Sync, TCL-Parser, Metriken, die Tools aus
SPEC ss9.2. Siehe SPEC ss14 (M1-M5).

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

## Konfiguration

| Variable | Default | Bedeutung |
|---|---|---|
| `VISUALIZER_EMAIL` | — | Visualizer-Login (Basic Auth), landet auch im User-Agent |
| `VISUALIZER_PASSWORD` | — | Visualizer-Passwort |
| `MCP_PATH_SECRET` | — | ≥32 Zeichen `[A-Za-z0-9_-]`, ersetzt die Authentifizierung |
| `SYNC_INTERVAL_MIN` | `15` | Poll-Intervall in Minuten (1–1440), ab M1 wirksam |
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

## Backup (ab M1 relevant)

```bash
sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"
```

Als naechtlichen Host-Cronjob einrichten, 7 Tage Rotation.
