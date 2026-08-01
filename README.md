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

### Profile: `libtk8.6` ist Pflicht

SPEC ss7.1 parst mit `tkinter.Tcl()`. In `python:*-slim` ist `_tkinter`
einkompiliert, die Tk-Laufzeitbibliotheken fehlen aber — schon `import tkinter`
scheitert dort an `ImportError: libtk8.6.so: cannot open shared object file`.
Das Dockerfile installiert deshalb `libtk8.6`; wird die Zeile entfernt, macht
der Smoke-Step im Build-Workflow den Build rot.

Als zweite Sicherung ist der Import weich: faellt der Interpreter aus, parst
`tcl_profile.py` mit einem eigenen Listensplitter weiter, statt den Dienst
sterben zu lassen. Beide Wege werden auf **allen** Fixtures gegeneinander
geprueft, inklusive der Frage, welche Eingaben sie ablehnen; ein Test simuliert
zusaetzlich den Importfehler.

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

## Deployment A: docker compose auf dem Host

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

## Deployment B: Portainer + GitHub Container Registry

Siehe SPEC ss16. Der Unterschied zu A: das Image wird nicht auf dem Host gebaut,
sondern von GitHub Actions, und Portainer zieht es aus `ghcr.io`.

### Einmalig einrichten

1. **Repo auf GitHub pushen.** Der Workflow
   [.github/workflows/build-image.yaml](.github/workflows/build-image.yaml)
   laeuft bei jedem Push auf `main`: erst `ruff` und `pytest`, dann Build und
   Push nach `ghcr.io/<owner>/<repo>` mit den Tags `latest` und `sha-<commit>`.
   Ein eigenes Secret ist nicht noetig — der Runner bringt `GITHUB_TOKEN` mit.

2. **Ist das Repo privat**, ist auch das Paket privat. Entweder das Paket
   oeffentlich schalten (GitHub → Packages → Package settings → Change
   visibility; das Image enthaelt keine Credentials, die kommen erst zur
   Laufzeit) oder in Portainer unter *Registries → Add registry → Custom* die
   Registry `ghcr.io` mit GitHub-Login und einem PAT mit `read:packages`
   hinterlegen.

3. **Stack in Portainer anlegen:** *Stacks → Add stack → Repository* (auf
   [compose.portainer.yaml](compose.portainer.yaml) zeigen) oder *Web editor*
   mit dem Inhalt dieser Datei.

4. **Stack-Variablen setzen** (Portainer, Abschnitt *Environment variables*):

   | Variable | Beispiel |
   |---|---|
   | `IMAGE_REPOSITORY` | `ghcr.io/<owner>/visualizer-mcp` |
   | `IMAGE_TAG` | `latest` |
   | `VISUALIZER_EMAIL` | dein Visualizer-Login |
   | `VISUALIZER_PASSWORD` | dein Visualizer-Passwort |
   | `MCP_PATH_SECRET` | `openssl rand -hex 24` |
   | `PUBLIC_BASE_URL` | `https://<hostname>` |
   | `CLOUDFLARED_NETWORK` | Name des vorhandenen Tunnel-Netzes |
   | `SYNC_INTERVAL_MIN` | `15` (optional) |

5. **Deploy the stack.** Beim ersten Start legt Docker das benannte Volume
   `visualizer_mcp_data` an — mit der Eigentuemerschaft aus dem Image, das
   haendische `chown 10001` entfaellt hier also.

### Aktualisieren

Nach einem Push auf `main` wartet man den Workflow ab und drueckt in Portainer
*Stacks → visualizer-mcp → Update the stack* mit angehaktem **Re-pull image**.
Ohne den Haken bleibt der alte Layer liegen, weil sich der Tag `latest` nicht
geaendert hat.

Zurueckrollen: `IMAGE_TAG` auf `sha-<commit>` einer aelteren Version setzen und
den Stack aktualisieren.

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
- Die Redaction kennt nicht nur das Klartextpasswort, sondern auch den
  base64-Teil des `Authorization`-Headers — die Form, in der es tatsaechlich
  ueber die Leitung geht. Zusaetzlich maskiert ein Ausdruck jeden
  `Authorization`-Header unabhaengig vom Inhalt.
- Ist das Passwort kuerzer als 8 Zeichen, warnt der Server beim Start: so kurze
  Werte laesst der Filter bewusst durch, weil er sonst zufaellige
  Uebereinstimmungen im Text zerschiessen wuerde.
- uvicorn-Access-Logs sind aus — sie wuerden den Secret-Pfad jeder Anfrage
  protokollieren.
- `/healthz` ist ueber den Tunnel erreichbar, solange das Ingress den Hostnamen
  pauschal weiterleitet. Die Route liefert nur `ok` — keine Zahlen, keine
  Version, kein Hinweis auf den Secret-Pfad. Wer sie schliessen will, ergaenzt
  im `cloudflared`-Ingress vor der Catch-all-Regel einen Eintrag mit
  `path: ^/healthz$` und `service: http_status:404` (SPEC ss10.1).
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
kanonisierte Form der bruehrelevanten Felder aus `parsed_json`:

- Typ, Getraenkeart, Zielgewicht, Zieltemperatur
- bei Advanced-Profilen die Schritte mit Modus, Zielwert, Temperatur, Dauer,
  Uebergang und *aktiver* Abbruchbedingung
- bei Legacy-Profilen der Block `legacy_settings` — Zieldruck bzw. Ziel-Flow,
  Hold- und Decline-Zeiten, Praeinfusionsparameter, Begrenzer und die
  Temperaturstufen, sofern eingeschaltet

Nicht enthalten: `title`, `author`, `notes`, der Name eines Schrittes, alle per
`exit_if 0` deaktivierten Schwellwerte und die Kopf-Sollwerte des jeweils
*anderen* Profiltyps — die DE1-App schreibt `flow_profile_*` auch in ein
Druckprofil, wertet sie dort aber nicht aus. Bei nicht parsebaren Profilen ist
der Wert `NULL`.

Anlass fuer den Hash war ein Echtfall: zwei Versionen des Default-Profils
unterschieden sich nur durch zwei leere Zusatzschluessel und ein doppeltes
Leerzeichen in den Notizen. Anlass fuer `legacy_settings` war die Gegenprobe —
ohne die Kopf-Sollwerte waeren zwei Versionen mit 8,6 und 8,9 bar Bruehdruck
semantisch gleich gewesen.

### Neu-Parsen nach Parseraenderungen

`parsed_json` traegt eine `parser_version`. Aendert sich, welche Felder der
Parser liefert, wird `PARSER_VERSION` in `tcl_profile.py` hochgezaehlt; der
naechste Start parst dann alle Profile aus dem gespeicherten `raw_tcl` neu und
schreibt `parsed_json`, `semantic_hash`, Name und Notizen fort. `raw_tcl` und
`version_hash` bleiben unberuehrt — die Identitaet einer Version haengt an der
Datei, nicht an unserer Deutung. Kein Re-Sync, keine Migration.

## Abnahme auf dem Host

Vier der sechs Kriterien aus SPEC ss13 laufen automatisch
([tests/test_acceptance.py](tests/test_acceptance.py), je Test mit seiner
Nummer). Zwei brauchen die echte Maschine bzw. Docker. Diese Reihenfolge
abarbeiten:

### 0. Vorbedingung — Image existiert

```bash
docker compose build                       # Deployment A
# oder: Workflow auf GitHub gruen, Paket unter ghcr.io sichtbar (Deployment B)
```

**Erfolg:** Build laeuft ohne Fehler durch. Erwartete Stolpersteine: fehlendes
`tk` (sollte *nicht* auftreten — `tkinter.Tcl()` laeuft ohne X, unter Windows
verifiziert, im slim-Image aber erst hier bewiesen) und ein `pip install`, das
Netzzugriff braucht.

### 1. Erststart

```bash
docker compose up -d && docker compose logs -f
```

**Erfolg:** in den Logs erscheinen nacheinander
`migrations applied files=001_init.sql,002_...,003_...`,
`sync worker started`, dann `sync done mode=backfill new=<n> ... errors=0`.
Das deckt **Kriterium 1** ab. Kein `docker logs`-Eintrag darf das Passwort, den
`MCP_PATH_SECRET` oder einen `Authorization`-Header enthalten (**Kriterium 6**,
Stichprobe):

```bash
docker compose logs | grep -iE 'authorization|<die-ersten-8-zeichen-des-secrets>'
```

**Erfolg:** keine Treffer, oder nur Zeilen mit `***REDACTED***`.

### 2. Healthcheck

```bash
docker inspect --format '{{.State.Health.Status}}' visualizer-mcp
```

**Erfolg:** `healthy` (kann bis zu 75 s dauern — `start_period` 15 s plus ein
Intervall).

### 3. Kriterium 5 — Neustart ohne Datenverlust

```bash
docker compose exec visualizer-mcp python -c \
  "import sqlite3;print(sqlite3.connect('/data/shots.db').execute('select count(*) from shots').fetchone())"
docker compose restart
# 30 s warten, dann denselben Befehl erneut
```

**Erfolg:** gleiche Anzahl vorher und nachher, Healthcheck wieder `healthy`,
keine `migrations applied`-Zeile beim zweiten Start (die Migrationen sind
bereits verbucht).

### 4. Connector einbinden

```bash
docker compose exec visualizer-mcp visualizer-mcp --print-connector-url
```

Die ausgegebene URL in claude.ai unter Einstellungen → Connectors → *Add custom
connector* eintragen, OAuth-Felder leer lassen.

**Erfolg:** Der Connector verbindet sich, und im Chat erscheinen unter „+" neun
Tools. Testfrage: *„Wie ist der Stand meines Espresso-Archivs?"* → `status()`
antwortet mit der Shot-Zahl. Verbindet er sich nicht, zuerst pruefen:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<hostname>/<secret>/mcp
```

**Erfolg:** `400` oder `405`, **nicht** `404`. Ein `404` heisst falscher Pfad
oder falsches Secret; ein `502` heisst, cloudflared erreicht den Container nicht
(gleiches Docker-Netz? Servicename im Ingress korrekt?).

### 5. Kriterium 2 — neuer Bezug erscheint rechtzeitig

Einen Espresso ziehen und per DYE zu Visualizer hochladen. Uhrzeit des Uploads
notieren. Dann in claude.ai fragen: *„Zeig mir meinen letzten Shot."*

**Erfolg:** `get_shot("latest")` liefert den neuen Bezug. Das darf **sofort**
klappen — der Frische-Check synchronisiert, wenn der letzte Abgleich mehr als
zwei Minuten her ist, und `freshness.synced` steht dann auf `true`. Ohne
Nachfrage taucht der Bezug spaetestens nach `SYNC_INTERVAL_MIN` + 1 min in
`list_shots` auf; das ist die Schranke aus Kriterium 2.

Kommt nichts an:

```bash
docker compose exec visualizer-mcp visualizer-mcp --sync-once
```

Die JSON-Ausgabe zeigt `new_shots`, `errors` und `warnings` im Klartext.

### 6. Kriterium 4 — Profilaenderung an der Maschine

Ein Profil auf der DE1 aendern (z. B. Temperatur um 1 Grad), einen Bezug ziehen,
hochladen. Dann in claude.ai: *„Welche Profilversionen gibt es?"*

**Erfolg:** `list_profiles()` zeigt fuer dieses Profil eine Version mehr als
vorher, und der aeltere Bezug haengt weiterhin an seinem alten `version_hash`.
Aendert sich nur der `version_hash`, nicht aber der `semantic_hash`, war die
Aenderung kosmetisch — dann hat die Maschine die Datei umformatiert, ohne dass
sich am Bezug etwas aendert.

## Backup

```bash
sqlite3 ./data/shots.db ".backup ./data/backup/shots-$(date +%F).db"
```

Als naechtlichen Host-Cronjob einrichten, 7 Tage Rotation.
