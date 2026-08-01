# visualizer-mcp

MCP-Server, der Espresso-Bezuege der Decent DE1 von
[visualizer.coffee](https://visualizer.coffee) lokal archiviert (SQLite) und Claude
per Custom Connector zur Analyse bereitstellt.

Vollstaendige Spezifikation: [SPEC_visualizer-mcp.md](SPEC_visualizer-mcp.md).

## Stand: Milestone M4 (MCP-Tools)

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
- TCL-Profilparser auf Basis von `tkinter.Tcl()`, Versionierung ueber den
  sha256 des normalisierten Roh-TCL, Verknuepfung Shot -> Profilversion
  (`tcl_profile.py`)
- Abgeleitete Metriken nach SPEC ss8 (Fassung 1.1) mit Cache in `shot_metrics`
  (`metrics.py`)
- Alle neun Tools aus SPEC ss9.2 (`server.py`)

Noch nicht vorhanden: die optionalen MCP-Prompts aus SPEC ss9.3
(`dial_in_check`, `bean_history`) und die Haertung aus M5. Siehe SPEC ss14.

## Tools

| Tool | Zweck |
|---|---|
| `list_beans()` | Bohnen mit Bezugszahl, Zeitraum, Muehleneinstellungen |
| `list_shots(bean?, roaster?, profile?, since?, until?, limit, cursor?)` | kompakte Liste, neueste zuerst |
| `get_shot(id\|"latest", bean?, include_curve, max_points)` | Metadaten + Metriken + Kurve + Profil-Kurzfassung |
| `get_shot_metrics(id)` | nur die Metriken |
| `compare_shots(ids[2..4], include_curves)` | Gegenueberstellung mit Differenz zum ersten |
| `list_profiles()` | Profile mit ihren Versionen |
| `get_profile(shot_id? \| name? \| version_hash?)` | vollstaendige Sollwerte |
| `sync_now()` | sofortiger Abgleich (einzige schreibende Operation, idempotent) |
| `status()` | Bestand, letzter Sync, Warnungen |

Filter sind Teilstrings ohne Beachtung der Gross-/Kleinschreibung. `bean` trifft
Marke *oder* Sorte, `roaster` nur die Marke. `since`/`until` nehmen ISO8601 oder
relative Kuerzel (`12h`, `7d`, `2w`, `1m`, `1y`).

### Was die Docstrings leisten muessen

Claude bekommt die Zahlen ohne Einheiten und muss sie trotzdem richtig deuten.
Deshalb steht die Begriffserklaerung zweimal: einmal vollstaendig im
Server-Prompt (`INSTRUCTIONS` in `server.py`, immer im Kontext) und einmal
verkuerzt in jedem Tool, das die betroffenen Felder liefert. Erklaert werden
Einheiten, die Semantik von `pi_end` samt `pi_end_source`, der Unterschied
zwischen `peak_pressure_infusion` und `max_pressure_global` sowie die Bedeutung
von `warnings` (Feld ist `null`, nicht 0).

Tools ohne diese Felder (`list_beans`, `status`) wiederholen die
Metrik-Erklaerung nicht — sie waere dort Ballast, den jede Tool-Liste mitschleppt.

### Antwortbudget

SPEC ss9 setzt ~15 kB je Antwort. Gemessen am echten Archiv:

| Aufruf | Groesse |
|---|---|
| `status()` | 0,7 kB |
| `list_shots(limit=10)` | 3,3 kB |
| `get_shot("latest")` | 5,1 kB |
| `compare_shots(3 ids, include_curves)` | 8,9 kB |

Kurven kommen als parallele Arrays (`t`, `p`, `fi`, `fo`, `w`, `tb`). Das spart
gegenueber einer Objektliste mit denselben Kurzschluesseln rund 49 %, gegenueber
einer mit sprechenden Spaltennamen rund 70 %. Ein Test haelt beide Schranken
fest.

### Frische-Check bei `get_shot("latest")`

Liegt der letzte Abgleich mehr als zwei Minuten zurueck, synchronisiert der
Server vor der Antwort — ein eben gezogener Bezug ist damit sofort da. Das
Ergebnis steht im Feld `freshness`. Schlaegt der Abgleich fehl, kommt trotzdem
eine Antwort aus dem Archiv, mit Hinweis: veraltete Daten sind besser als keine.
Hintergrundschleife, `sync_now` und der Frische-Check teilen sich ein Lock, damit
nie zwei Laeufe gleichzeitig schreiben.

### Metriken: `state_change` und `pi_end`

`espresso_state_change` ist **keine** Phasennummer, wie SPEC ss8 urspruenglich
annahm, sondern eine Rechteckwelle: sie springt bei jedem Phasenwechsel zwischen
zwei Sentinelwerten (`-10000000` und `+10000000`). Nicht der Wert traegt
Information, sondern der **Wechsel**. An den Echtdaten geprueft: die Zahl der
Wechsel ist `Schritte - 1`, und die Zeitpunkte decken sich exakt mit den
Schrittdauern des Profils. Der erste Wechsel liegt bei t < 0.1 s und markiert
den Shot-Start, nicht einen Phasenwechsel.

Daraus folgt fuer `pi_end`: die Marken sagen *dass* gewechselt wurde, nicht
*wozu*. Welche Grenze die Praeinfusion beendet, geht aus ihnen allein nicht
hervor. Der Druckanstieg dient deshalb als **Anker**, nicht als Ergebnis:

> `pi_end` = die letzte Phasengrenze vor dem Moment, in dem der Druck erstmals
> `0.6 x max_pressure_global` erreicht.

Der zurueckgegebene Wert ist damit immer ein von der Maschine gemeldeter
Phasenwechsel. Ohne Marken faellt es auf den Ankerzeitpunkt selbst zurueck (die
Heuristik der urspruenglichen SPEC); welcher Weg griff, steht in
`pi_end_source` (`state_change` | `heuristic`). Im gesamten Archiv griff bisher
ausschliesslich `state_change`.

### Metriken: Plausibilitaet der Waage

Drei Faelle machen abgeleitete Werte unbrauchbar, statt sie stillschweigend
falsch zu melden (SPEC ss8: fehlende Grundlage -> `null` plus `warnings`):

- **Waage nicht tariert** — zeigt sie in der ersten halben Sekunde schon mehr
  als 0.3 g, kann das nicht der Bezug sein (die Maschine praeinfundiert
  sekundenlang). `t_first_drops` wird `null`.
- **Mittlerer Bezugsfluss <= 0** — ein Variationskoeffizient um einen
  Mittelwert <= 0 waere negativ und damit sinnlos. `flow_stability` wird `null`.
- **Gewicht wird negativ** — Waage angestossen. Die Werte bleiben stehen, aber
  eine Warnung weist darauf hin.

### Metrik-Cache

`shot_metrics` haelt das Ergebnis je Shot als JSON, zusammen mit der
`metrics_version`, unter der es entstand. Aendert sich eine Definition, wird die
Konstante `METRICS_VERSION` in `metrics.py` hochgezaehlt — der naechste Start
rechnet dann alles neu, ohne Migration und ohne Re-Sync. Ein erneuter Upsert
eines Shots verwirft dessen Cache ebenfalls, weil Zeitreihe, Dosis und
Bezugsgewicht sich geaendert haben koennen.

### Profile: was der Parser leistet und was nicht

Der eigene Parser ist die Referenz — an dem TCL, das er liest, haengt auch der
Versionshash. Visualizers `format=json` dient in den Tests als Gegenprobe
(`tests/test_tcl_profile.py`); weicht eine Seite ab, schlaegt der Test fehl.
Zwei Unterschiede sind bekannt und dort dokumentiert:

1. **Legacy-Profile (`settings_2a`/`2b`) haben im TCL keine Schritte.**
   `advanced_shot` ist leer. Visualizer *synthetisiert* fuer solche Profile
   sechs Schritte aus den `flow_profile_*`- und `preinfusion_*`-Settings. Das
   ist eine Rekonstruktion, keine Information aus der Datei — wir bilden sie
   nicht nach. Die Sollwerte stehen als Kopffelder in `parsed_json`.
2. **Weissraum in den Notizen.** Visualizers JSON enthaelt an einer Stelle die
   Zeichenfolge `\n` plus acht Leerzeichen, wo im TCL ein einzelnes Leerzeichen
   steht. Ursache ist Visualizers Serializer.

`settings_2a` -> `pressure` und `settings_2c` -> `advanced` sind gegen echte
Daten verifiziert; `settings_2b` -> `flow` folgt der DE1-Konvention, liegt aber
noch nicht als Beleg vor.

Ein nicht parsebares Profil bricht nichts ab: `raw_tcl` wird trotzdem
gespeichert, `parsed_json` bekommt `parse_ok: false` samt Fehlertext, und Titel
und Notizen kommen aus einem toleranten Zeilenscan.

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

Die Fixtures unter `tests/fixtures/` sind echte, anonymisierte API-Antworten
(Kontokennungen ersetzt) — darunter der Referenz-Shot aus SPEC ss13, ein Shot
mit defekter Waage und drei Versionen desselben Profils.

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
einzelne Shots fehlgeschlagen sind.

Die Zusammenfassung trennt `errors` von `warnings`:

- **`errors`** sind voruebergehende Probleme (Netz, Schreibfehler). Solange
  welche auftreten, bleibt der Cursor stehen — sonst bliebe ein fehlgeschlagener
  Shot dauerhaft ungeholt.
- **`warnings`** sind deterministische Befunde (`parse_ok=false`, Shot ohne
  hinterlegtes Profil). Ein Retry wuerde daran nichts aendern, deshalb laeuft
  der Cursor weiter. Shots ohne Profil werden in
  `sync_state['shots_without_profile']` gemerkt und nicht erneut abgefragt.

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

`002` ergaenzt `profiles.semantic_hash`, `003` legt `shot_metrics` an. Beide
werden beim Start automatisch angewendet und rueckwirkend befuellt.

### `version_hash` vs. `semantic_hash`

`version_hash` (sha256 des normalisierten Roh-TCL) ist die **Identitaet** einer
Profilversion — daran haengt die Verknuepfung eines Shots, und sie bleibt
unangetastet. `semantic_hash` ist reine **Gruppierung**: er laeuft ueber eine
kanonisierte Form der bruehrelevanten Felder aus `parsed_json` (Schritte mit
Modus, Zielwert, Temperatur, Dauer, Uebergang und *aktiver* Abbruchbedingung;
dazu Typ, Getraenkeart, Zielgewicht und Zieltemperatur).

Nicht enthalten: `title`, `author`, `notes`, der Name eines Schrittes und alle
per `exit_if 0` deaktivierten Schwellwerte. Anlass war ein Echtfall — zwei
Versionen des Default-Profils unterschieden sich nur durch zwei leere
Zusatzschluessel und ein doppeltes Leerzeichen in den Notizen. Bei nicht
parsebaren Profilen ist der Wert `NULL`.

## Backup

```bash
sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"
```

Als naechtlichen Host-Cronjob einrichten, 7 Tage Rotation.
