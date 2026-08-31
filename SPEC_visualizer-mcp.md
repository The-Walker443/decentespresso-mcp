# Spezifikation: `visualizer-mcp` — MCP-Server für Espresso-Shot-Analyse

**Version:** 1.2 · **Stand:** 2026-08-31 · **Zielgruppe:** Claude Code (Implementierung) + Betreiber (Matthias)

> **Änderungen 1.2 (2026-08-31):**
> - §17 (M6) Antwortökonomie: `curve_shape`, `compare_shots` in einem Aufruf,
>   gestraffte Docstrings, Messung je Aufruf.
> - §18 (M7) Schreibende Tools: `update_shot` hinter `WRITE_ENABLED`. Damit
>   ist „rein lesend" aus §1 kein Nicht-Ziel mehr — Löschen bleibt eines.
>
> **Änderungen 1.1 (2026-08-01)** — nach Abnahme von M1:
> - §6.2: Inkrementeller Sync läuft über `updated_after` statt über die
>   Seitenheuristik (die API stellt den Parameter bereit; er findet auch
>   nachträglich geänderte Shots).
> - §8: `pi_end` primär aus den Phasenmarken; `peak_pressure` aufgeteilt in
>   `peak_pressure_infusion` und `max_pressure_global`.
> - §13: Erwartungswerte des Referenz-Shots an §8 angeglichen (`end_pressure`
>   ≈ 5.3, `t_first_drops` ≈ 5.3), jeweils mit Begründung.

---

## 1. Ziel & Kontext

Ein selbst gehosteter MCP-Server (Docker, Homelab), der Claude in claude.ai/Claude-Apps
per Custom Connector Zugriff auf Espresso-Bezugsdaten gibt:

1. **Shots von visualizer.coffee synchronisieren und lokal archivieren** (SQLite),
   damit die volle Historie unabhängig vom 1-Monats-Limit des Visualizer-Free-Tiers
   erhalten bleibt.
2. **Profile (.tcl der de1app) automatisch mitladen, parsen und versionieren** —
   Claude bekommt Soll-Kurven/Phasen als Klartext-JSON, nie mehr manuelle Uploads.
3. **Kompakte, analysierbare Tool-Antworten** liefern: Shot-Listen (filterbar nach
   Bohne), einzelne Shots inkl. Kurve + Profil, abgeleitete Metriken, Vergleiche.

**Umgebung (vorhanden):**
- Docker-Stack im Heimnetz, `cloudflared`-Tunnel läuft bereits.
- Maschine: Decent DE1 Pro (Firmware/App 1.46), Upload via de1app/DYE → visualizer.coffee.
- Visualizer-Account: `the-walker` (Free-Tier). Shots aktuell öffentlich.
- Claude-Nutzung: claude.ai Web + Mobile-App (Custom Connectors: hinzufügen nur via
  Web/Desktop; Nutzung danach auch mobil möglich).

**Nicht-Ziele (v1):** Keine eigene Web-UI, kein Multi-User, kein Self-Hosting von
Visualizer selbst, **kein Löschen** von Bezügen.

*(Ursprünglich stand hier auch „kein Schreiben zu Visualizer". Das ist mit §18
eingelöst: ein einziges Tool, strikte Whitelist, standardmäßig abgeschaltet.
Gelesen wird weiterhin überwiegend — geschrieben nur auf ausdrückliche
Anweisung.)*

---

## 2. Architektur

```
de1app / DYE ──upload──▶ visualizer.coffee (Free-Tier, Upload-Ziel & Community)
                              │  REST-API (HTTP Basic Auth)
                              ▼
                   ┌────────────────────────┐
                   │  visualizer-mcp        │  Docker-Container
                   │  ├─ Sync-Worker        │  (Poll alle N Min + manuell)
                   │  ├─ SQLite  /data      │  (Shots, Kurven, Profile, Versionen)
                   │  └─ MCP-Server         │  Streamable HTTP  :8000
                   └───────────┬────────────┘
                               │  internes Docker-Netz
                     cloudflared (vorhanden)
                               │  https://<hostname>/<secret>/mcp
                               ▼
                  Claude (claude.ai / Desktop / Mobile)
                  Custom Connector; lesende Tools plus - nur mit
                  WRITE_ENABLED - update_shot (Write-through, §18)
```

Grundsatz: **visualizer.coffee bleibt Upload-Ziel und Community-Fenster; der
MCP-Container ist die Quelle der Wahrheit für Analysen** (volle Historie, Profile,
Metriken).

---

## 3. Tech-Stack (verbindlich, Abweichungen begründen)

| Baustein | Wahl | Begründung |
|---|---|---|
| Sprache | Python ≥ 3.12 | Ökosystem, TCL-Parsing via stdlib möglich |
| MCP-Framework | `fastmcp` (v2.x, jlowin/fastmcp) | Streamable-HTTP-Transport eingebaut, wenig Boilerplate |
| Transport | **Streamable HTTP** | Von Claude unterstützt; SSE gilt als Auslaufmodell |
| HTTP-Client | `httpx` | Async, Timeouts, Retries |
| DB | SQLite (Datei in Volume `/data`) | Single-User, einfaches Backup, kein Zusatzcontainer |
| Scheduler | `apscheduler` (oder asyncio-Loop) | periodischer Sync im selben Prozess |
| Container | `python:3.12-slim`, non-root User | klein, sicher |
| Tests | `pytest` + Fixture-Dateien (echte CSV/TCL-Beispiele) | deterministisch |

---

## 4. Visualizer-API — Referenz & Verifikationspflicht

**Authoritative Quelle: https://apidocs.visualizer.coffee/ — vor Implementierung
jedes Endpunkts dort Schema und Feldnamen verifizieren.** Stand der Recherche:

| Endpunkt | Auth | Status | Zweck |
|---|---|---|---|
| `GET /api/shots?page=&items=` | Basic | dokumentiert | paginierte Liste eigener Shots (Metadaten, IDs) |
| `GET /api/shots/{id}` bzw. `/api/shots/{id}/download` | Basic | in API-Doku prüfen | vollständige Shot-Daten inkl. Zeitreihen als JSON |
| `GET /api/shots/{id}/profile.csv` | öffentl. Shots: keine | **verifiziert** | Zeitreihen als CSV (Spalten s. §7.2) |
| `GET /api/shots/{id}/profile` | öffentl. Shots: keine | **verifiziert** | Profildatei, Content-Type `application/x-tcl` |

Regeln:
- **HTTP Basic Auth** mit `VISUALIZER_EMAIL` / `VISUALIZER_PASSWORD` (laut API-Doku
  für persönliche Automationen vorgesehen). Credentials ausschließlich aus Env.
- Bevorzugt den JSON-Download-Endpunkt für Zeitreihen nutzen (eine Anfrage statt
  CSV+Meta getrennt); CSV-Endpunkt als Fallback implementieren.
- Höflich pollen: Standardintervall 15 min, `If-None-Match`/ETag nutzen falls
  vorhanden, Backoff bei 429/5xx (exponentiell, max 1 h), User-Agent
  `visualizer-mcp/<version> (privat, Kontakt-Mail)` setzen.
- Alle Visualizer-Fehler loggen, aber Tools dürfen nie Credentials oder komplette
  HTTP-Header ausgeben.

---

## 5. Datenmodell (SQLite)

Migrationen als nummerierte SQL-Dateien (`migrations/001_init.sql`, …), beim Start
automatisch anwenden. `PRAGMA journal_mode=WAL;` setzen.

```sql
CREATE TABLE shots (
  id            TEXT PRIMARY KEY,          -- Visualizer-UUID
  started_at    TEXT NOT NULL,             -- ISO8601 UTC
  bean_brand    TEXT,                      -- z. B. "Tchibo"
  bean_type     TEXT,                      -- z. B. "Test"
  bean_notes    TEXT,
  profile_name  TEXT,
  profile_id    INTEGER REFERENCES profiles(id),
  grinder_model TEXT,
  grinder_setting TEXT,
  dose_g        REAL,                      -- kann fehlen → aus Download-JSON, sonst NULL
  yield_g       REAL,
  duration_s    REAL,
  ratio         REAL,                      -- yield/dose, berechnet
  drink_tds     REAL,
  drink_ey      REAL,
  enjoyment     INTEGER,                   -- Visualizer-Bewertung falls vorhanden
  notes         TEXT,
  raw_json      TEXT NOT NULL,             -- kompletter API-Response (Nachverarbeitung)
  synced_at     TEXT NOT NULL
);

CREATE TABLE shot_series (                 -- Zeitreihe, 1 Zeile pro Messpunkt
  shot_id   TEXT REFERENCES shots(id) ON DELETE CASCADE,
  elapsed   REAL NOT NULL,
  pressure  REAL, flow_in REAL, flow_out REAL,
  weight    REAL, temp_mix REAL, temp_basket REAL,
  PRIMARY KEY (shot_id, elapsed)
);

CREATE TABLE profiles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,             -- title aus TCL
  version_hash  TEXT NOT NULL UNIQUE,      -- sha256 des normalisierten TCL
  raw_tcl       TEXT NOT NULL,
  parsed_json   TEXT NOT NULL,             -- s. §7 Parser-Output
  profile_notes TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE sync_state (                  -- Key-Value: letzter Lauf, Cursor, Fehler
  key TEXT PRIMARY KEY, value TEXT
);

CREATE INDEX idx_shots_bean ON shots(bean_brand, bean_type, started_at DESC);
CREATE INDEX idx_shots_started ON shots(started_at DESC);
```

Designentscheidungen:
- `raw_json` immer speichern → spätere Schema-Erweiterungen ohne Re-Sync möglich.
- Profile werden **dedupliziert über `version_hash`**; ein Shot referenziert exakt
  die Profilversion, mit der er bezogen wurde. So bleibt nachvollziehbar, mit
  welchen Sollwerten ein alter Shot lief, auch wenn das Profil später geändert wurde.
- Löschen von Shots auf Visualizer löscht lokal **nichts** (Archiv-Zweck).

---

## 6. Sync-Logik

1. **Initial-Backfill:** `GET /api/shots` paginiert bis zum Ende durchlaufen; für
   jeden unbekannten Shot Detaildaten + Profil-TCL laden und speichern.
2. **Inkrementell (alle `SYNC_INTERVAL_MIN`, Default 15):**
   `GET /api/shots?sort=updated_at&updated_after=<cursor>` (Unix-Sekunden). Cursor
   ist der höchste bekannte `updated_at` minus 120 s Überlappung. Jede gelistete
   ID, die unbekannt ist **oder** deren `updated_at` sich geändert hat, wird
   nachgeladen. Bei Fehlern im Lauf bleibt der Cursor stehen.
   Das ersetzt die frühere Heuristik „Seite 1 bis nur noch bekannte IDs": die
   hätte nachträglich geänderte Shots (Notizen, Bewertung, TDS) nie gefunden.
3. **Dedupe:** Primärschlüssel = Visualizer-UUID. Erneuter Abruf eines bekannten
   Shots aktualisiert nur mutable Felder (Notizen, Bewertung, TDS) via Upsert.
4. **Profil-Verarbeitung pro Shot:** TCL laden → normalisieren (Whitespace/Zeilen-
   enden vereinheitlichen) → sha256 → falls Hash neu: parsen + `profiles`-Insert;
   sonst nur `last_seen` aktualisieren. Shot mit `profile_id` verknüpfen.
5. **Fehlerbehandlung:** Einzelner fehlerhafter Shot bricht den Lauf nicht ab;
   Fehler in `sync_state['last_errors']` (JSON-Liste, max 20) protokollieren.
6. **Uhrzeiten:** intern durchgehend UTC speichern; Ausgabe in Tools mit
   Zeitzonen-Suffix (Anzeige-TZ `Europe/Berlin` aus Env `TZ`).

---

## 7. Parsing

### 7.1 DE1-Profil (.tcl)

Format: flache Tcl-Key-Value-Struktur, u. a. `title`, `author`, `profile_notes`,
`beverage_type`, `settings_profile_type`, Temperatur-/Druck-/Flow-Settings sowie bei
Advanced-Profilen `advanced_shot { {step1…} {step2…} }` (Liste von Step-Dicts mit
`name`, `temperature`, `pressure`/`flow`, `seconds`, `transition`, `exit_*`-Feldern).

**Implementierung:** Kein Regex-Gefrickel — die stdlib mitnutzen:

```python
from tkinter import Tcl
tcl = Tcl()
pairs = tcl.splitlist(raw)            # top-level: abwechselnd key, value
steps = [dict_from(tcl.splitlist(s))  # advanced_shot: Liste von Step-Strings
         for s in tcl.splitlist(d["advanced_shot"])]
```

(Läuft headless ohne Display. Fallback bei Parse-Fehler: Rohtext speichern,
`profile_notes`/`title` per tolerantem Zeilen-Scan extrahieren, Flag
`parse_ok=false` im JSON.)

**Container-Umgebung (Fassung 1.1, nach einem Produktionsabsturz präzisiert):**
Der Entwurf lag richtig — `Tcl()` braucht kein X —, aber `python:*-slim`
braucht trotzdem eine Zutat. `_tkinter` ist dort einkompiliert, die
Tk-Laufzeitbibliotheken fehlen jedoch; schon `import tkinter` scheitert an

```
ImportError: libtk8.6.so: cannot open shared object file
```

Das Dockerfile installiert deshalb `libtk8.6`
(`apt-get install -y --no-install-recommends libtk8.6`, zieht `libtcl8.6` und
die nötigen X11-Bibliotheken als Abhängigkeiten mit). Das Metapaket `tk` mit
`wish` und Werkzeugen ist nicht nötig.

Zwei Sicherungen, damit derselbe Fehler nicht zweimal in Produktion auffällt:

- Der Import von `tkinter` in `tcl_profile.py` ist weich. Fällt der Interpreter
  aus, setzt das Modul `TCL_INTERPRETER_AVAILABLE = False`, hält den Grund in
  `TCL_IMPORT_ERROR` fest und parst mit einem eigenen Listensplitter
  (`_split_tcl_list`) weiter — blanke Wörter, `{...}` mit Verschachtelung und
  Zeilenumbrüchen, `"..."` mit Backslash-Ersetzungen, keine Ersetzung innerhalb
  von Klammern. Ein Profilparser ist kein Grund, den Dienst nicht zu starten.
  Ein Test simuliert den Importfehler und prüft genau das.
- Der Smoke-Step im Build-Workflow **erzwingt** den Interpreter im fertigen
  Image. Fehlt die apt-Zeile, wird der Build rot statt der Container.

Beide Parser-Wege werden auf allen Fixtures gegeneinander geprüft, inklusive
der Frage, welche Eingaben sie ablehnen.

**Parser-Output (`parsed_json`):**

```json
{
  "title": "D-Flow / default", "author": "Damian", "type": "advanced|pressure|flow",
  "notes": "…profile_notes…", "target_weight_g": 36.0, "target_temp_c": 88.0,
  "steps": [
    {"name": "Fill", "mode": "flow", "target": 6.0, "temp_c": 88.0,
     "duration_s": 25, "transition": "fast",
     "exit": {"type": "pressure_over", "value": 4.0}}
  ],
  "parse_ok": true
}
```

### 7.2 Zeitreihen (CSV-Fallback)

Spalten: `information_type, elapsed, pressure, current_total_shot_weight, flow_in,
flow_out, water_temperature_boiler, water_temperature_in, water_temperature_basket,
metatype, metadata, comment`. Zeilen `information_type=meta` → Metadaten-Map,
`=moment` → Messpunkte. Achtung: `dose` ist im CSV **nicht** enthalten (nur im
Detail-JSON bzw. auf der Shot-Seite); Boiler-Temp ist oft leer.

---

## 8. Abgeleitete Metriken (deterministisch definieren!)

Modul `metrics.py`, pro Shot einmal berechnen und cachen (Spalte oder Tabelle
`shot_metrics`, JSON). Definitionen — exakt so implementieren, damit Werte über
Shots vergleichbar sind:

| Metrik | Definition |
|---|---|
| `t_first_drops` | kleinstes `elapsed` mit `weight > 0.3 g` |
| `pi_end` | **primär** aus `shot_series.state_change`: Zeitpunkt der Phasenmarke, die das Ende der Präinfusion markiert. **Fallback** nur wenn der Shot keine Phasenmarken hat: kleinstes `elapsed` mit `pressure ≥ 0.6 × max_pressure_global`. Welcher Weg griff, steht in `pi_end_source` (`state_change` \| `heuristic`) |
| `peak_pressure_infusion`, `t_peak` | Druckmaximum im Fenster `[0, pi_end + 2 s]` + Zeitpunkt. Das ist der Druck, der den Puck aufbaut — bei ansteigenden Profilen (D-Flow) liegt das globale Maximum am Shot-Ende und sagt darüber nichts aus |
| `max_pressure_global` | globales Druckmaximum über den ganzen Shot, eigenes Feld |
| `pressure_dip_after_peak` | `peak_pressure_infusion − min(pressure)` im Fenster `[t_peak, t_peak+4 s]` (Kanal-/Puck-Nachgeben-Indikator) |
| `avg_flow_pour` | Mittel `flow_out` über `[pi_end, ende]` |
| `flow_stability` | Variationskoeffizient von `flow_out` im selben Fenster |
| `end_pressure` | Mittel `pressure` der letzten 2 s |
| `pressure_trend_pour` | lineare Steigung (bar/s) von `pressure` über `[pi_end, ende]` |
| `temp_basket_mean/std` | über `[pi_end, ende]` |
| `duration_s`, `ratio` | letztes `elapsed`; `yield/dose` (dose aus Meta, sonst null) |

Alle Werte gerundet (Druck 0.1, Flow 0.01, Zeit 0.1 s). Fehlende Grundlagen → Feld
`null` + `warnings`-Liste.

---

## 9. MCP-Interface

Server-Name: `visualizer-espresso`. Alle Tools **read-only** außer `sync_now`.
Antwortbudget: Standard ≤ ~15 kB; Kurven immer downsampeln (§9.1).

### 9.1 Downsampling-Regel

Parameter `max_points` (Default 120, Max 400). Gleichmäßige zeitbasierte Ausdünnung;
zusätzlich garantiert enthalten: erster Punkt, letzter Punkt, Punkt von
`max_pressure_global` und Punkt von `peak_pressure_infusion` (hier geht es um
Kurventreue, deshalb beide Maxima).
Rückgabe als kompakte Arrays (`t[]`, `p[]`, `fo[]`, `fi[]`, `w[]`, `tb[]`), nicht
als Objektliste — spart ~60 % Tokens.

### 9.2 Tools

| Tool | Parameter | Rückgabe (Kern) |
|---|---|---|
| `list_beans()` | – | Bohnen mit `brand, type, shot_count, first/last_shot, letzte grinder_settings` |
| `list_shots(bean?, roaster?, profile?, since?, until?, limit=10, cursor?)` | Filter case-insensitive, Teilstring-Match | kompakte Zeilen: `id, started_at, bean, profile, grind, dose, yield, ratio, duration, peak_pressure_infusion, notes_kurz`; `next_cursor` |
| `get_shot(id \| "latest", include_curve=true, max_points=120)` | `latest` optional mit `bean`-Filter | Metadaten + Metriken (§8) + Kurve (§9.1) + Profil-Kurzfassung (`title, version_hash[:8], steps kompakt`) |
| `get_shot_metrics(id)` | – | nur §8-Metriken + Warnungen |
| `compare_shots(ids[2..4], include_curves=false)` | müssen existieren | Tabelle der Metriken nebeneinander + Delta-Spalte zum ersten Shot; optional Kurven (dann `max_points=60` je Shot) |
| `list_profiles()` | – | je Profil: `name, versionen[] (hash8, first/last_seen, shot_count)` |
| `get_profile(shot_id? \| name?, version_hash?)` | genau eine Angabe | `parsed_json` vollständig + `notes`; bei `name` ohne Version: neueste |
| `sync_now()` | – | `{new_shots, updated, new_profile_versions, duration_ms, errors[]}` |
| `status()` | – | DB-Statistik, letzter Sync, Version, Free-Tier-Hinweis falls letzter Visualizer-Shot > 25 Tage alt |

Konventionen:
- Fehler als strukturierte MCP-Tool-Errors mit klarer Meldung
  (`shot_not_found`, `visualizer_unreachable`, `auth_failed`, …).
- Jede Tool-Beschreibung (Docstring) erklärt Einheiten (bar, ml/s, g, °C, s) —
  das liest das Modell und interpretiert die Zahlen dann korrekt.
- Datumsparameter: ISO8601 oder relative Kürzel (`"7d"`, `"1m"`).

### 9.3 Optionale MCP-Prompts (nice-to-have)

- `dial_in_check(shot_id)` — vorformulierter Analyseauftrag (Metriken vs. Profil-Soll).
- `bean_history(bean)` — Verlaufsauswertung einer Bohne.

---

## 10. Sicherheit

**Bedrohungsmodell:** Der Endpoint ist öffentlich erreichbar (Claude ruft ihn aus
Anthropics Infrastruktur auf — Heimnetz-/VPN-Beschränkung funktioniert daher NICHT).
Die Daten sind unkritisch (Espresso-Shots, read-only), die Visualizer-Credentials
sind kritisch.

Maßnahmen (v1, pragmatisch):
1. **Geheimer Pfad statt Auth:** MCP-Endpoint unter
   `https://<hostname>/<MCP_PATH_SECRET>/mcp` mit `MCP_PATH_SECRET` = 32+ Zeichen
   zufällig (`openssl rand -hex 24`). Alle anderen Pfade → 404 ohne Body.

   **Ausnahme `/healthz`** (Stand der Umsetzung, korrigiert gegenüber dem
   Entwurf): Die Route liegt auf derselben ASGI-App wie der MCP-Endpoint und
   ist damit über den Tunnel erreichbar, sofern das Ingress den Hostnamen
   pauschal weiterleitet — was die Vorlage in §11.4 tut. Sie liefert
   ausschließlich `ok` als Text: keine Bestandszahlen, keine Version, kein
   Hinweis auf den Secret-Pfad. Der Informationsgewinn für einen Scanner
   beschränkt sich darauf, dass hinter dem Hostnamen überhaupt etwas läuft —
   das verrät ein 404 mit TLS-Handshake ohnehin.

   Wer sie dennoch schließen will, ergänzt vor der Catch-all-Regel im
   `cloudflared`-Ingress:

   ```yaml
   - hostname: coffee-mcp.example.com
     path: ^/healthz$
     service: http_status:404
   ```

   Der Docker-Healthcheck bleibt davon unberührt, er spricht `127.0.0.1:8000`
   im Container an und geht nie durch den Tunnel.
2. **Kein Cloudflare Access davor** — Access würde Claudes Verbindungsaufbau
   blockieren. Stattdessen in Cloudflare: Rate-Limiting-Regel (z. B. 100 req/min)
   und Bot-Fight-Mode für den Hostname aus/anpassen, WAF-Standardregeln an.
3. **Credentials:** nur via Env/`.env` (nicht ins Git; `.env.example` committen).
   Tools/Logs geben niemals Credentials, Auth-Header oder vollständige URLs mit
   Secret aus. Log-Filter dafür implementieren.
4. **Container-Härtung:** non-root User, `read_only: true` Root-FS, nur `/data`
   beschreibbar, `no-new-privileges`, keine Ports am Host publishen (nur internes
   Docker-Netz zum cloudflared-Container).
5. **Schreiben ist die Ausnahme:** Von den Tools mutiert einzig `update_shot`
   etwas bei Visualizer, und das nur, wenn `WRITE_ENABLED` gesetzt ist —
   sonst wird es gar nicht erst registriert (§18.4). Es ist idempotent, hat
   eine geschlossene Feldliste und kann nicht löschen. `sync_now` bleibt
   ebenfalls idempotent und schreibt nichts nach außen.

**Upgrade-Pfad (v2, optional):** FastMCP bringt Auth-Provider mit (OAuth 2.1 /
Token-Verifier). Claude unterstützt authlose UND OAuth-basierte Remote-Server;
wenn gewünscht, später OAuth nachrüsten und den Secret-Pfad ablösen. Als Issue im
Repo anlegen, nicht in v1 bauen.

---

## 11. Deployment

### 11.1 Repo-Struktur

```
visualizer-mcp/
├─ compose.yaml
├─ Dockerfile
├─ .env.example
├─ migrations/001_init.sql
├─ src/visualizer_mcp/
│  ├─ server.py            # FastMCP-App, Tools, Prompts
│  ├─ sync.py              # Scheduler + Sync-Worker
│  ├─ visualizer_client.py # httpx-Client, Auth, Retry/Backoff
│  ├─ tcl_profile.py       # §7.1
│  ├─ metrics.py           # §8
│  ├─ db.py                # Schema, Migrationen, Queries
│  └─ config.py            # Env-Parsing, Validierung beim Start
├─ tests/
│  ├─ fixtures/            # echte CSV-, TCL-, JSON-Beispiele
│  └─ test_*.py
└─ README.md               # Betriebshandbuch (Kurzfassung dieser Spec §11–13)
```

### 11.2 compose.yaml (Vorlage)

```yaml
services:
  visualizer-mcp:
    build: .
    container_name: visualizer-mcp
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: Europe/Berlin
    volumes:
      - ./data:/data
    networks:
      - cloudflared_net          # an das vorhandene Netz des Tunnels anpassen
    read_only: true
    tmpfs: [/tmp]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval: 60s
      timeout: 5s
      retries: 3

networks:
  cloudflared_net:
    external: true
```

### 11.3 .env.example

```dotenv
VISUALIZER_EMAIL=you@example.com
VISUALIZER_PASSWORD=change-me
MCP_PATH_SECRET=<openssl rand -hex 24>
SYNC_INTERVAL_MIN=15
DB_PATH=/data/shots.db
LOG_LEVEL=INFO
PUBLIC_BASE_URL=https://coffee-mcp.example.com   # nur für Log-Ausgabe der Connector-URL
```

### 11.4 Cloudflare-Tunnel (Ergänzung der vorhandenen Config)

```yaml
ingress:
  - hostname: coffee-mcp.example.com
    service: http://visualizer-mcp:8000
  # …bestehende Regeln…
  - service: http_status:404
```

DNS: CNAME `coffee-mcp` → `<tunnel-id>.cfargotunnel.com` (proxied).

### 11.5 Einbindung in Claude

1. claude.ai (Web) → Einstellungen → Connectors → „Add custom connector".
2. URL: `https://coffee-mcp.example.com/<MCP_PATH_SECRET>/mcp` — OAuth-Felder leer.
3. Im Chat über „+" → Connectors aktivieren; Tools erscheinen automatisch.
4. Mobile: Connector wird übernommen, Hinzufügen neuer Server geht nur via Web/Desktop.
5. Projekt-Anweisung ergänzen (ersetzt den bisherigen Visualizer-Link-Workflow):
   „Für Shot-Analysen nutze den Connector visualizer-espresso: erst list_shots/
   get_shot, Profile über get_profile."

---

## 12. Betrieb & Wartung

**Backup:** Nächtlicher Host-Cronjob:
`sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"` +
Rotation (7 Tage behalten). Optional später: Litestream-Replikation.

**Updates:** Manuell `git pull && docker compose build && docker compose up -d`.
Kein Auto-Update (Watchtower) für diesen Container — API-Breaking-Changes lieber
bewusst einspielen.

**Logs:** strukturiert (JSON oder key=value) auf stdout; `docker logs`. Sync-Läufe
mit Zusammenfassung loggen (`new=2 updated=1 profiles=0 dur=1.2s`).

**Troubleshooting:**

| Symptom | Prüfen |
|---|---|
| Claude: „couldn't connect" | URL exakt (inkl. `/mcp`)? Tunnel-Ingress? `curl -s https://…/<secret>/mcp` liefert MCP-Antwort/405 statt 404? |
| Tools fehlen im Chat | Connector im Chat aktiviert? Server neu verbunden nach Tool-Änderungen? |
| 401 im Sync-Log | Visualizer-Credentials; Login im Browser testen |
| 502 vom Hostname | Container läuft? Gleiches Docker-Netz wie cloudflared? Service-Name in Ingress korrekt? |
| Leere Shot-Liste | `status()` → letzter Sync? `sync_now()` ausführen; Backfill-Log prüfen |
| Neue Shots fehlen | Free-Tier: Shot älter als Sync-Lücke + 1 Monat? → verloren; Intervall verkürzen |
| Profil `parse_ok=false` | TCL-Rohtext in DB ansehen; Parser-Fixture ergänzen, Issue |

**Free-Tier-Wächter:** `status()` warnt, wenn `now − letzter_sync > 7 Tage`
(Archivlücken-Risiko durch das 1-Monats-Fenster von Visualizer Free).

---

## 13. Tests & Abnahmekriterien

**Unit (pytest, offline mit Fixtures):**
- TCL-Parser: D-Flow-Beispiel → erwartete Steps/Notes; kaputtes TCL → `parse_ok=false` ohne Exception.
- Metriken: Referenz-Shot (Fixture = realer Shot `6eb25d36…`) → erwartete Werte:
  `peak_pressure_infusion` ≈ 4.1 bar, `end_pressure` ≈ 5.3 bar,
  `t_first_drops` ≈ 5.3 s, `duration` ≈ 22.9 s, `pi_end` ≈ 6.2 s
  (Quelle `state_change`).
  Bei diesem D-Flow-Shot ist `max_pressure_global` ≈ 5.4 bar und fällt mit dem
  Schlusspunkt zusammen — genau deshalb sind Infusions- und Globalmaximum
  getrennte Felder.

  Zwei dieser Werte wurden mit Fassung 1.1 an §8 angeglichen, nicht umgekehrt:
  - `end_pressure` ≈ 5.3 statt 5.4: §8 mittelt über die letzten 2 s (hier
    5.320). Der letzte Einzelmesswert allein wäre 5.43, reagiert aber auf
    einen einzigen Ausreißer — der Mittelwert ist das robustere Maß.
  - `t_first_drops` ≈ 5.3 statt 4.8: die Schwelle bleibt bei `weight > 0.3 g`.
    Das erste Gewicht überhaupt fällt bei 4.77 s mit 0.20 g an, liegt damit
    aber im Rauschband der Waage (Auflösung ~0.1 g, Tropfenaufprall und
    Vibration erzeugen dort Ausschläge). Eine Schwelle unterhalb 0.3 g würde
    je nach Waage und Tassenstellung schwanken und die Werte über Shots
    hinweg unvergleichbar machen — genau das soll §8 verhindern.

- pi_end-Fallback: eigener Fixture-Test mit einer Zeitreihe **ohne**
  Phasenmarken → `pi_end_source == "heuristic"`. Der Pfad bleibt geprüft, auch
  solange im Archiv ausschließlich `state_change` greift.
- Downsampling: Peak-Punkt bleibt stets enthalten; `len ≤ max_points`.
- Sync-Dedupe: zweifacher Lauf derselben Daten → keine Duplikate.

**Integration (manuell):**
- MCP Inspector (`npx @modelcontextprotocol/inspector`) gegen den lokalen Container:
  alle Tools aufrufbar, Schemas valide.
- End-to-end: Connector in claude.ai einbinden → „Zeig meine letzten Shots mit
  Tchibo Test" liefert korrekte Daten.

**Abnahme (Definition of Done):**
1. Backfill lädt alle vorhandenen Shots inkl. Profilversionen fehlerfrei.
2. Neuer Shot auf der DE1 erscheint ≤ `SYNC_INTERVAL_MIN` + 1 min in `list_shots`.
3. `get_shot("latest")` liefert Metadaten + Metriken + Kurve + Profil in einer Antwort ≤ 15 kB.
4. Profiländerung an der Maschine erzeugt nach nächstem Shot eine neue Profilversion; alter Shot bleibt mit alter Version verknüpft.
5. Container übersteht Neustart ohne Datenverlust; Healthcheck grün.
6. Kein Secret in Logs/Tool-Outputs (Stichprobe).

---

## 14. Build-Reihenfolge für Claude Code

- **M0** Gerüst: Repo, Dockerfile, compose, config.py, healthz, CI-freies pytest-Setup.
- **M1** `visualizer_client.py` + `db.py` + Backfill (nur Metadaten + Zeitreihen).
- **M2** `tcl_profile.py` + Profilversionierung, Verknüpfung Shot↔Profilversion.
- **M3** `metrics.py` + Caching.
- **M4** MCP-Tools (§9) + Inspector-Test.
- **M5** Härtung (read-only FS, Log-Filter), README/Betriebsteil, Abnahmetests.
- **M6** Antwortökonomie (§17): `curve_shape`, `compare_shots` in einem Aufruf,
  gestraffte Docstrings, Messung je Aufruf.
- **M7** Schreibende Tools (§18): `update_shot` mit Whitelist, Validierung und
  Write-through, hinter `WRITE_ENABLED`.

Nach jedem Milestone: Tests grün, kurzer Commit. API-Schemas in M1 zuerst gegen
https://apidocs.visualizer.coffee/ verifizieren, bevor Felder festgezurrt werden.

---

## 15. Bewusst verschoben (Backlog)

- OAuth statt Secret-Pfad (FastMCP-Auth-Provider).
- MCP-Prompts aus §9.3 (`dial_in_check`, `bean_history`).
- Wochen-/Bohnen-Reports als MCP-Resource.
- TDS-/EY-Erfassung strukturiert (falls Refraktometer angeschafft wird).
- Import weiterer Quellen (Beanconqueror) — Schema ist darauf vorbereitet (`raw_json`).
- ~~Schreibende Tools (Notizen/Bewertung zurück zu Visualizer)~~ — eingelöst
  in §18 (M7). Löschen bleibt bewusst draußen.

---

## 16. Deployment via Portainer + GHCR

Ergänzt §11 für den Fall, dass der Stack nicht per `docker compose` auf dem Host,
sondern über Portainer verwaltet wird. §11 bleibt gültig für lokale Läufe.

### 16.1 Bildbau in GitHub Actions

`.github/workflows/build-image.yaml`: bei jedem Push auf `main` (außer reinen
Doku-Änderungen) und auf Knopfdruck.

1. **Job `test`** — `ruff check` und `pytest`. Bewusst als Tor: ein Image mit
   roten Tests soll nicht in der Registry landen.
2. **Job `build`** — `docker/build-push-action` baut aus dem vorhandenen
   `Dockerfile` und pusht nach `ghcr.io/<owner>/<repo>`.

Tags: `latest` und `sha-<commit>`. Der SHA-Tag erlaubt ein Zurückrollen auf eine
bestimmte Version, ohne den Stack umzubauen. Authentifizierung läuft über das
vom Runner bereitgestellte `GITHUB_TOKEN` mit `packages: write` — kein eigenes
Secret nötig. Der Buildcache liegt in `type=gha`.

### 16.2 Stack-Datei

`compose.portainer.yaml`, drei Unterschiede zu `compose.yaml`:

| Punkt | `compose.yaml` (lokal) | `compose.portainer.yaml` |
|---|---|---|
| Image | `build: .` | `image: ${IMAGE_REPOSITORY}:${IMAGE_TAG}` |
| Daten | Bind-Mount `./data`, braucht `chown 10001` | benanntes Volume `visualizer_mcp_data` |
| Config | `env_file: .env` | `${VAR}` aus den Portainer-Stack-Variablen |

Das benannte Volume ist der wichtigere der drei: Docker legt es mit der
Eigentümerschaft an, die `/data` im Image hat (UID 10001), womit der
Stolperstein „Container läuft als non-root, Bind-Mount gehört root" entfällt.

Alles andere aus §10.4 bleibt: `read_only: true`, `tmpfs: /tmp`,
`no-new-privileges`, keine Host-Ports. Zusätzlich ein Deckel auf die
Logrotation — der Container läuft dauerhaft.

### 16.3 Private Repositories

Ist das GitHub-Repo privat, ist auch das Paket in GHCR privat, und Portainer
kann es nicht ohne Anmeldung ziehen. Zwei Wege:

- **Paket öffentlich schalten** (GitHub → Packages → Package settings → Change
  visibility). Das Image enthält keine Credentials — die kommen erst zur
  Laufzeit aus den Stack-Variablen. Für dieses Projekt ausreichend.
- **Registry in Portainer hinterlegen** (Registries → Add registry → Custom,
  URL `ghcr.io`, Benutzername = GitHub-Login, Passwort = PAT mit
  `read:packages`). Nötig, wenn das Paket privat bleiben soll.

### 16.4 Abnahme auf dem Host

Kriterium 2 und 5 aus §13 lassen sich nur dort prüfen. Die Schrittfolge samt
Erfolgskriterien steht im README unter „Abnahme auf dem Host".

---

## 17. Antwortökonomie (M6)

**Problem.** Jede Runde eines Gesprächs verarbeitet den gesamten bisherigen
Kontext neu. Zwei Dinge treiben ihn: Antworten, die mehr liefern als die Frage
braucht, und Fragen, die mehrere Aufrufe kosten. Beides ist behebbar, ohne an
der Analysequalität zu sparen.

**Nicht angetastet:** die Metrikdefinitionen aus §8 (`METRICS_VERSION` bleibt),
die Tool-Namen und die Semantik in den `INSTRUCTIONS`.

### 17.1 Kurvenform statt Rohzahlen

Neues Feld `curve_shape`, immer mitgeliefert von `get_shot` und `compare_shots`.
Es beschreibt den Verlauf abschnittsweise entlang der Phasenmarken aus
`state_change` — dieselbe Quelle wie `pi_end`:

```json
{"segments": [{"from": 6.2, "to": 22.9,
               "p":  {"from": 3.0, "to": 5.4,  "dir": "rising",  "linear": false},
               "fo": {"from": 2.09,"to": 1.8,  "dir": "falling", "linear": false}}],
 "markers": {"t_first_drops": 5.3, "pi_end": 6.2, "t_peak": 7.2},
 "source": "state_change"}
```

`dir` vergleicht Anfangs- und Endwert (`flat` unterhalb 0.2 bar bzw. 0.15 ml/s),
`linear` beschreibt den Weg dazwischen: maximale Abweichung von der Geraden,
bezogen auf die Spannweite im Abschnitt, Grenze 15 %. Beides zusammen — ein
Abschnitt kann `flat` und trotzdem nicht linear sein, wenn er eine Delle hat.
`source` hält fest, woher die Grenzen stammen: `state_change` (Maschinenmarken),
`markers` (ersatzweise `pi_end`) oder `none`.

Folgerichtig kehren sich die Vorgaben aus §9.1 um: **`include_curve` in
`get_shot` hat jetzt Default `false`**, `max_points` den Default 60 statt 120.
Die Punktarrays bleiben unverändert verfügbar, sind aber die Ausnahme.

**Korrektur zu §9.2:** Dort steht „optional Kurven (dann `max_points=60` je
Shot)". Vier Shots mit je 60 Punkten ergeben zusammen mit `curve_shape` und den
Profilen 18,2 kB und sprengen damit das Antwortbudget. `compare_shots` verteilt
deshalb ein Gesamtbudget von 100 Punkten auf die verglichenen Shots (mindestens
20 je Shot): 2 Shots → 47, 3 → 30, 4 → 23 Punkte. Der schlimmste Fall liegt
damit bei 13,4 kB. Ein Test prüft genau diesen Fall.

### 17.2 `compare_shots` in einem Aufruf

Liefert je Shot zusätzlich die Profil-Kurzfassung (Titel, `version_hash[:8]`,
`semantic_hash[:8]`, Typ, Kopf-Sollwerte, Schrittzahl) und setzt
`profile_notice`, wenn die Bezüge nicht auf denselben Sollwerten liefen. Drei
Fälle, absichtlich unterschiedlich scharf formuliert:

| Lage | Hinweis |
|---|---|
| gleicher `version_hash` | keiner |
| verschiedene Version, gleicher `semantic_hash` | „rein kosmetisch" |
| abweichender `semantic_hash` | „Achtung … Unterschiede können vom Profil kommen" |
| mindestens ein Profil fehlt | „Vergleich der Sollwerte ist unvollständig" |

Der dritte Fall ist der Grund für das Feld: ohne ihn liest man einen
Metrikunterschied leicht als Folge des Mahlgrads, obwohl das Profil ein anderes
war. Abschaltbar über `include_profile=false`.

### 17.3 Docstrings ohne Doppelung

Die vollständige Begriffserklärung steht in den `INSTRUCTIONS` — sie sind
ohnehin immer im Kontext. Die Tool-Docstrings nennen nur noch Zweck,
Parameterbedeutung und verweisen für die Begriffe dorthin. Die ausführlichen
Warntexte zu `pi_end`, den beiden Druckmaxima und `warnings` sind aus den
einzelnen Tools entfernt.

Ein Test hält eine Obergrenze für die Summe aller Tool-Definitionen fest und
prüft, dass die Glossartexte nicht in einzelne Docstrings zurückwandern.

### 17.4 Messbarkeit

`telemetry.py` loggt je Tool-Aufruf `tool`, `dur_ms` und `bytes` — **keine**
Parameterwerte, keine URL, keinen Pfad. Argumente sind der wahrscheinlichste
Weg, auf dem irgendwann etwas Vertrauliches in eine Logzeile gerät.

Gemessen am selben Archiv, vorher/nachher in Byte:

| Aufruf | vor M6 | nach M6 | |
|---|---:|---:|---|
| Tool-Definitionen (alle 9, gehen bei jeder Anfrage mit) | 10 399 | 6 876 | −34 % |
| `get_shot("latest")` | 5 021 | 2 092 | −58 % |
| `get_shot(id)` | 4 626 | 1 868 | −60 % |
| `compare_shots(2)` inkl. Profile | 1 680 + 2×1 013¹ | 4 234 | 3 Aufrufe → 1 |
| `get_shot(id, include_curve=true)` | 4 626 | 3 959² | −14 % |
| `list_shots(limit=10)` | 3 277 | 3 277 | ±0 |

¹ Vor M6 lieferte `compare_shots` keine Profile; derselbe Informationsstand
kostete zwei zusätzliche `get_profile`-Aufrufe und damit drei Gesprächsrunden.

² Enthält jetzt zusätzlich `curve_shape` und ist trotzdem kleiner, weil
`max_points` von 120 auf 60 gesunken ist. Wer mehr Punkte braucht, fordert sie
weiterhin an (Maximum 400).

Von den verbleibenden 6 876 B der Tool-Definitionen sind rund 3 600 B
JSON-Schema der Parameter. Tiefer kommt man nur über weniger Parameter, nicht
über kürzere Texte.

---

## 18. Schreibende Tools (M7)

Löst den Backlog-Punkt „Schreibende Tools" aus §15 ein. **Nicht-Ziel bleibt
Löschen** — die API kann es (`DELETE /shots/{id}`), v1 baut es nicht.

### 18.1 Verifikation der API (2026-08-01, v1.17.1)

Wie in §4 gefordert erst geprüft, dann gebaut. Der Endpunkt ist
`PATCH /api/shots/{id}` mit `{"shot": {…}}`. Vier Befunde, drei davon
undokumentiert:

1. **`Accept: application/json` ist Pflicht.** Ohne den Header antwortet die
   API mit `422 {"error":"Request must be JSON."}` — auch bei korrektem
   `Content-Type`. Steht in keiner Doku.

2. **Die dokumentierte Feldliste ist unvollständig.** `ShotUpdateRequest` listet
   nur `profile_title`, `barista`, `bean_weight`, `bean_notes`,
   `espresso_notes`, `private_notes`, die acht Sensorik-Noten, `coffee_bag_id`,
   `tag_list` und `metadata`. Tatsächlich schreibbar sind (einzeln geprüft,
   jeweils geschrieben, zurückgelesen und zurückgesetzt):

   | Feld | dokumentiert | schreibbar |
   |---|:--:|:--:|
   | `bean_brand`, `bean_type`, `roast_date`, `roast_level` | – | ✅ |
   | `grinder_setting`, `drink_weight` | – | ✅ |
   | `espresso_enjoyment`, `drink_tds`, `drink_ey` | – | ✅ |
   | `bean_weight`, `bean_notes`, `espresso_notes`, `barista` | ✅ | ✅ |
   | `private_notes` | ✅ | ❌ (400, Premium) |
   | `id`, `start_time` | – | ❌ (400) |

3. **Nicht erlaubte Felder werden stillschweigend verworfen.** `400` kommt nur,
   wenn nach dem Filtern *nichts* Erlaubtes übrig bleibt (`"param is missing or
   the value is empty or invalid: shot"`). Ein Aufruf mit `private_notes`
   **plus** einem erlaubten Feld liefert also `200` — ohne die Notiz zu
   schreiben. Deshalb ist der Read-back-Vergleich in §18.3 keine Kür.

4. **Die API validiert keine Wertebereiche.** `espresso_enjoyment: 999` und
   `-5` wurden anstandslos gespeichert. Die Prüfung in §18.2 ist der einzige
   Schutz, nicht eine zweite Absicherung.

### 18.2 Ein Tool, strikte Whitelist

`update_shot(id, fields)`. Erlaubt sind ausschließlich:

| Gruppe | Felder |
|---|---|
| Bohne | `bean_brand`, `bean_type`, `roast_date`, `roast_level`, `bean_notes` |
| Zubereitung | `grinder_setting`, `bean_weight`, `drink_weight` |
| Bewertung | `espresso_enjoyment`, `espresso_notes`, `private_notes`, `drink_tds`, `drink_ey` |
| Sonstiges | `barista` |

Unbekannte Felder → Fehler mit der vollständigen Liste. Ausdrücklich gesperrt
sind Kennung, Zeitstempel, Telemetrie und Profil; sie bekommen eine eigene
Meldung, die den Grund nennt statt nur „unbekannt". `null` löscht ein Feld.

Validierung **vor** dem ersten API-Aufruf, alle Verstöße gesammelt:

| Feld | Regel |
|---|---|
| `espresso_enjoyment` | Ganzzahl 0–100 |
| `bean_weight` | 5–30 g |
| `drink_weight` | 10–100 g |
| `drink_tds` / `drink_ey` | 0–30 % / 0–50 % |
| `roast_date` | ISO `YYYY-MM-DD`, nicht in der Zukunft |
| Freitextfelder | ≤ 5 000 Zeichen |

Zwei Details aus der Praxis: Kommazahlen werden akzeptiert (`"18,5"` → `18.5`),
und beim Röstdatum nennt die Fehlermeldung ausdrücklich, dass die DE1-App
`TT.MM.JJJJ` schreibt, hier aber ISO erwartet wird. **Damit entstehen gemischte
Formate im Bestand** — von der Maschine geschriebene Datumsangaben bleiben
deutsch, von uns geschriebene sind ISO. Bewusst in Kauf genommen: ein
maschinenlesbares Format ist mehr wert als Einheitlichkeit mit einem
mehrdeutigen.

### 18.3 Write-through — Visualizer bleibt die Wahrheit

Es wird nie nur lokal geschrieben. Die Kette, unter demselben Lock wie der Sync:

1. `GET /shots/{id}` — Vorher-Stand **frisch von der API**, nicht aus der
   lokalen Kopie. Die könnte veraltet sein, und dann wäre das gemeldete
   „vorher" eine Behauptung statt einer Messung.
2. `PATCH /shots/{id}` mit den validierten Feldern.
3. `GET /shots/{id}` erneut, Upsert in die lokale DB, Metriken des Shots neu
   rechnen — über denselben Pfad wie im Sync-Lauf (`refresh_shot`). Eine
   Dosisänderung ändert die Ratio; ohne diesen Schritt bliebe der Metrik-Cache
   falsch.

Die Antwort nennt je Feld `before` und `after`, **beides aus dem Read-back**.
Weicht ein Feld nicht ab, landet es in `unchanged` samt Hinweis auf den
Premium-Vorbehalt — das ist die einzige Stelle, an der der stille Verwurf aus
§18.1.3 sichtbar wird.

### 18.4 Schalter und Protokoll

`WRITE_ENABLED` (Default `false`). Ist er aus, wird das Tool **nicht
registriert** — es steht nicht in der Tool-Liste und lehnt nicht ab. Ein Tool,
das existiert und ablehnt, lädt zum Nachfragen ein; eines, das es nicht gibt,
nicht. Ohne Visualizer-Verbindung bleibt es ebenfalls weg.

Der Docstring bindet das Modell: nur auf ausdrückliche Nutzeranweisung, genau
die genannten Felder, Bestätigung anhand der zurückgelieferten Werte.

Je Schreibvorgang eine Logzeile mit Shot-ID, **Feldnamen** und Dauer — keine
Werte. In `espresso_notes` und `private_notes` kann Privates stehen, und ein
Log ist der falsche Ort dafür.

### 18.5 Abnahme

An Shot `51c96e2c` (Tchibo Test) durchlaufen: fünf ungültige Eingaben
abgewiesen ohne API-Aufruf, dann `espresso_enjoyment: 35` und eine Notiz
gesetzt. Read-back über `get_shot`, lokale DB und Metrik-Cache stimmen überein,
und die Gegenprobe direkt gegen `visualizer.coffee` zeigt beide Werte.
