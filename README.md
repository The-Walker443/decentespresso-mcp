# visualizer-mcp

MCP-Server, der Espresso-Bezuege der Decent DE1 lokal archiviert (SQLite)
und Claude per Custom Connector zur Analyse bereitstellt.

Quelle ist seit M8 **Decaid auf dem Tablet an der Maschine**, angesprochen
ueber das eigene Netz. Damit laeuft kein Teil der Archivkette mehr ueber
eine fremde Cloud. Der Upload nach [visualizer.coffee](https://visualizer.coffee)
kann als Community-Schaufenster weiterlaufen, ist aber nicht mehr noetig.

Vollstaendige Spezifikation: [SPEC_visualizer-mcp.md](SPEC_visualizer-mcp.md).

## Stand: Milestone M8 (Quelle = Decaid, alles lokal)

Vorhanden:

- Konfiguration aus Env inkl. Startup-Validierung (`config.py`)
- Strukturiertes key=value-Logging auf stdout mit Secret-Redaction (`logging_setup.py`)
- FastMCP-Server (Streamable HTTP) unter `/<MCP_PATH_SECRET>/mcp`, `/healthz`,
  alle anderen Pfade `404` ohne Body (`server.py`)
- Decaid-Client im LAN mit Backoff; ein ausgeschaltetes Tablet ist kein
  Fehlerzustand (`decaid_client.py`)
- Normalisierung der Quelldaten: alles in UTC, Bewertungen ohne die
  Scheinnullen der Import-Aera (`decaid_mapping.py`)
- SQLite mit Migrationsrunner, Upserts, Sync-Zustand (`db.py`)
- Abgleich: vollstaendige Bezugsliste, Details nur fuer Geaendertes,
  Hintergrundschleife alle `SYNC_INTERVAL_MIN` (`sync.py`)
- Profilversionierung aus dem eingebetteten Workflow-JSON
  (`decaid_profile.py`); der TCL-Parser bleibt nur fuer Altbestand lesbar
- Abgeleitete Metriken nach SPEC ss8 mit Cache in `shot_metrics`
  (`metrics.py`)
- Vier Waechterregeln ueber dem Bestand mit ntfy-Benachrichtigung
  (`guards.py`, `notify.py`)
- Alle Tools aus SPEC ss9.2 und ss20, dazu `curve_shape` und die
  Antwortoekonomie aus SPEC ss17 (`server.py`, `telemetry.py`)
- Vier Schreibtools hinter `WRITE_ENABLED` (SPEC ss18, ss20.5, `writes.py`)
- Haertung, Abnahmetests und Portainer-Deployment (SPEC ss16)

Noch nicht vorhanden: die optionalen MCP-Prompts aus SPEC ss9.3
(`dial_in_check`, `bean_history`). Siehe SPEC ss15.

## Tools

| Tool | Zweck |
|---|---|
| `list_beans()` | Bohnen mit Bezugszahl, Zeitraum, Muehleneinstellungen |
| `list_shots(bean?, roaster?, profile?, since?, until?, limit, cursor?)` | kompakte Liste, neueste zuerst |
| `get_shot(id\|"latest", bean?, include_curve, max_points)` | Metadaten + Metriken + Kurvenform + Profil-Kurzfassung |
| `get_shot_metrics(id)` | nur die Metriken |
| `compare_shots(ids[2..4], include_profile, include_curves)` | Gegenueberstellung mit Differenz zum ersten, Profilen und Abweichungshinweis |
| `list_profiles()` | Profile mit ihren Versionen |
| `get_profile(shot_id? \| name? \| version_hash?)` | vollstaendige Sollwerte |
| `sync_now()` | sofortiger Abgleich mit dem Tablet, idempotent |
| `status()` | Bestand, letzter Abgleich, Decaid-Zustand, Warnungen |
| `audit_archive(since?, rule?, limit?)` | Befunde der Waechter samt geltender Schwellen |
| `get_workflow()` | Einstellung fuer den naechsten Bezug, live vom Tablet |
| `update_shot(id, fields)` | Notiz, Bewertung, Gewichte — **nur mit `WRITE_ENABLED=true`** |
| `update_bean(id, fields)` | Stammdaten einer Bohne — **nur mit `WRITE_ENABLED=true`** |
| `update_batch(id, fields)` | Roestdatum, Gefrierzustand — **nur mit `WRITE_ENABLED=true`** |
| `set_workflow(fields)` | Mahlgrad, Ziele, Charge — **nur mit `WRITE_ENABLED=true`** |

Filter sind Teilstrings ohne Beachtung der Gross-/Kleinschreibung. `bean` trifft
Bohnenname *oder* Roesterei, `roaster` nur die Roesterei. `since`/`until` nehmen ISO8601 oder
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

### Verlauf: `curve_shape` statt Rohzahlen

Jeder Bezug bringt eine abschnittsweise Beschreibung seines Verlaufs mit,
abgeleitet aus den Phasenmarken der Maschine — je Abschnitt Anfangs- und
Endwert fuer Druck und Waagenfluss, Richtung und ob der Verlauf linear ist.
Beim Referenz-Shot sind das 579 B gegenueber rund 2 900 B Punktarrays, und
Fragen nach Anstieg, Plateau oder Phasendauer lassen sich damit ohne eine
einzige Rohzahl beantworten.

Die Punktarrays gibt es weiterhin, aber nur auf Anforderung
(`include_curve=true` bzw. `include_curves=true`). Sie kommen als parallele
Listen (`t`, `p`, `fi`, `fo`, `w`, `tb`) — das spart gegenueber einer
Objektliste mit denselben Kurzschluesseln rund 49 %, gegenueber einer mit
sprechenden Spaltennamen rund 70 %.

### Antwortbudget

SPEC ss9 setzt ~15 kB je Antwort, SPEC ss17 zieht die Schrauben an. Gemessen am
**Entwicklungsabzug vom 2026-08-01** (14 Shots) — beide Spalten am selben Stand,
sonst traegt der Vergleich nicht:

| Aufruf | vor M6 | nach M6 |
|---|---:|---:|
| Tool-Definitionen (alle 9, gehen bei jeder Anfrage mit) | 10,4 kB | 6,9 kB |
| `status()` | 0,7 kB | 0,7 kB |
| `list_shots(limit=10)` | 3,3 kB | 3,3 kB |
| `get_shot("latest")` | 5,0 kB | **2,1 kB** |
| `get_shot(id, include_curve=true)` | 4,6 kB | 4,0 kB |
| `compare_shots(2)` inkl. Profile | 3 Aufrufe, 3,7 kB | **1 Aufruf, 4,2 kB** |

Tests halten diese Schranken fest — sie sollen anschlagen, wenn etwas
zurueckwaechst.

Bei Produktionsgroesse gegengemessen (Abzug vom 2026-08-31, 69 Shots, 3 Bohnen):
`list_beans()` 0,9 kB, `list_shots(limit=10)` 3,7 kB, `status()` 0,7 kB, der
Rest unveraendert. Mit dem Bestand waechst nur `list_beans` (Zahl
**verschiedener Bohnen**, nicht der Bezuege) und `total_matching` in
`list_shots` — die Schranken tragen also auch im Betrieb.

### Waechter (SPEC ss20.7)

Vier Regeln pruefen nach jedem Abgleich, ob etwas nicht zusammenpasst. Sie
sind reine Funktionen ueber Tabellenzeilen - kein Netz, keine Datenbank, die
Uhr wird uebergeben. Jede ist ueber `GUARD_RULES` einzeln abschaltbar.

| Regel | Prueft |
|---|---|
| `grind_not_adjusted` | Chargenwechsel ohne Mahlgradaenderung |
| `bean_age` | Bohnenalter beim Bezug, Gefrierzeit herausgerechnet |
| `missing_rating` | Bewertung nach Ablauf der Frist nicht nachgetragen |
| `dose_outlier` | Gewichte passen nicht zum Soll des Workflows |

Zwei Befunde aus dem echten Bestand haben die Regeln geformt:

- **Die Dosis ist nicht pruefbar.** In allen 165 Faellen ist
  `actualDoseWeight` exakt gleich `targetDoseWeight` - die DE1 wiegt die
  Dosis nicht, sie uebernimmt den Sollwert. Die Regel prueft deshalb das
  **Bezugsgewicht** gegen sein Soll und die Dosis nur noch auf
  Plausibilitaet (0 g heisst: Waage nicht verbunden).
- **Eine offene Frist waere wertlos.** 145 von 169 Bezuegen sind unbewertet;
  ohne Obergrenze meldete `missing_rating` 83 Prozent des Archivs. Sie meldet
  darum nur zwischen 36 Stunden und sieben Tagen.

Die Benachrichtigung ueber ntfy hat drei Deckel, damit die Meldungen gelesen
bleiben: hoechstens eine Nachricht je Bezug, nur Bezuege der letzten 48
Stunden, hoechstens fuenf Nachrichten je Lauf. Gemessen am Bestand zeigt
`audit_archive` 63 Befunde, ntfy wuerde 2 melden. Ein Befund nennt nie
Freitext - die Nachrichten verlassen das Haus.

### Schreiben (SPEC ss18, ss20.5)

Standardmaessig **aus**. `WRITE_ENABLED=true` schaltet vier zusaetzliche
Tools frei: `update_shot`, `update_bean`, `update_batch`, `set_workflow`.
Aufgenommen ist in jede Whitelist nur, was gegen die echte API geschrieben
**und wieder gelesen** wurde. Ein Profilwechsel ist ausdruecklich nicht
moeglich - der gehoert an die Maschine.

Bei der Verifikation am 2026-09-14 zeigte sich, dass Decaid den
**Bezugszeitstempel annimmt**, waehrend es `id` und `createdAt` mit 400
abweist. Fuer die Telemetriefelder ist die Blockliste in `writes.py` damit
nicht die zweite Sicherung, sondern die einzige.

Ist der Schalter aus, tauchen die Tools nicht in
der Tool-Liste auf — sie lehnen nicht ab, sie existieren nicht.

Erlaubt sind ausschliesslich diese Felder; alles andere wird mit der
vollstaendigen Liste abgewiesen:

| Tool | Felder |
|---|---|
| `update_shot` | `espressoNotes`, `enjoyment` (0–100), `actualDoseWeight`, `actualYield` |
| `update_bean` | `name`, `roaster`, `species`, `processing`, `notes`, `decaf` |
| `update_batch` | `roastDate`, `buyDate`, `freezeDate`, `frozen` |
| `set_workflow` | `grinderSetting`, `grinderModel`, `targetDoseWeight`, `targetYield`, `beanBatchId` |

Kennungen, Zeitstempel, Telemetrie und das Profil sind gesperrt. Geloescht
wird nie — die API kann es, dieses Projekt baut es nicht.

**Write-through:** Geschrieben wird immer zuerst in Decaid, danach wird
frisch nachgelesen, lokal upserted und der Metrik-Cache neu gefuellt. Die
Antwort nennt je Feld `before` und `after` aus diesem Read-back — nicht aus
der Annahme, was gesendet wurde. Steht ein Feld unter `unchanged`, hat Decaid
es nicht uebernommen.

Zwei Eigenheiten, die das Design bestimmen (am 2026-09-14 gegen Decaid 0.8.5
verifiziert, jede Probe sofort zurueckgesetzt):

1. Decaid weist `id` und `createdAt`/`updatedAt` mit `400` ab — besser als
   das stillschweigende Verwerfen, das Visualizer betrieb. **Den
   Bezugszeitstempel nimmt es aber an**: ein `PUT` mit `timestamp` kam mit
   `200` zurueck und der Wert stand danach wirklich so da. Fuer die
   Telemetriefelder ist die Blockliste damit die einzige Sicherung.
2. Die API prueft **keine** Wertebereiche. Die Validierung in `writes.py`
   ist der einzige Schutz gegen einen Tippfehler im Archiv.

**Kein Auftaudatum.** Decaid fuehrt kein `unfreezeDate`. Beim Auftauen
`frozen` auf false setzen; das Bohnenalter rechnet ab da weiter, und
`audit_archive` kann es fuer spaetere Bezuege nur noch nach oben begrenzen
(`certain: false`). Wer es genau braucht, notiert den Tag in den
Bohnennotizen.

Beim Roestdatum wird ISO (`YYYY-MM-DD`) erwartet, die DE1-App schreibt
`TT.MM.JJJJ` — im Bestand koennen dadurch beide Formate stehen.

Je Schreibvorgang eine Logzeile mit Shot-ID, Feldnamen und Dauer — **ohne
Werte**, weil in Notizen Privates stehen kann.

### Welche Fassung laeuft?

`status()` beantwortet das mit drei Feldern:

| Feld | Herkunft |
|---|---|
| `version` | Paketmetadaten aus `pyproject.toml` (`importlib.metadata`) |
| `milestone` | abgeleitet aus der Nebenversion: `0.7.x` -> `M7` |
| `build_ref` | Commit-SHA, beim Image-Bau als `BUILD_REF` eingebacken |

Anlass war ein Fehlgriff beim M7-Deployment: `status()` meldete Version `0.1.0`
und `M6`, obwohl M7-Code lief — beide Zahlen waren von Hand gepflegt und
veraltet. Ob das alte Image lief oder nur das Feld hinterherhinkte, liess sich
nicht unterscheiden.

Jetzt gilt: stimmt `build_ref` nicht mit dem erwarteten Commit ueberein, hat
Portainer nicht neu gezogen (**Re-pull image** vergessen). Ist `build_ref`
`null`, laeuft der Server nicht aus einem gebauten Image.

**Beim Meilenstein die Version bumpen** — Nebenversion = Meilensteinnummer.
Ein Test vergleicht sie mit dem hoechsten in SPEC ss14 gelisteten Meilenstein
und schlaegt fehl, wenn der Bump fehlt.

### Messung je Aufruf

`telemetry.py` loggt pro Tool-Aufruf `tool`, `dur_ms` und `bytes`. Bewusst
**keine** Parameterwerte, keine URL, keinen Pfad: Argumente sind der
wahrscheinlichste Weg, auf dem irgendwann etwas Vertrauliches in eine Logzeile
geraet.

### Frische-Check bei `get_shot("latest")`

Liegt der letzte Abgleich mehr als zwei Minuten zurueck, synchronisiert der
Server vor der Antwort — ein eben gezogener Bezug ist damit sofort da. Das
Ergebnis steht im Feld `freshness`. Schlaegt der Abgleich fehl, kommt trotzdem
eine Antwort aus dem Archiv, mit Hinweis: veraltete Daten sind besser als keine.
Hintergrundschleife, `sync_now` und der Frische-Check teilen sich ein Lock, damit
nie zwei Laeufe gleichzeitig schreiben.

### Metriken: Phasenmarken und `pi_end`

Decaid meldet den Maschinenzustand im Klartext: `state.substate` laeuft
`preparingForShot` -> `preinfusion` -> `pouring`. Das Ende der Praeinfusion ist
damit **abgelesen statt erschlossen** - der Zeitpunkt, an dem die Maschine
`pouring` meldet.

Drei Quellen in dieser Reihenfolge; `pi_end_source` nennt die tatsaechlich
genutzte:

| Quelle | Woher | Guete |
|---|---|---|
| `substate` | Zustandsangabe der Maschine | abgelesen |
| `profile_frame` | letzte Schrittgrenze vor dem Druckanker | erschlossen |
| `heuristic` | Druck erreicht erstmals `0.6 x max_pressure_global` | Naeherung |

Die Hierarchie ist kein Vorratsbeschluss. Die de1app hat den Maschinenzustand
nie aufgezeichnet, Decaid tut es - und das Archiv enthaelt beides:

| Herkunft | `substate` | `profile_frame` | Heuristik |
|---|---|---|---|
| importiert (88) | — | 84 | 3 |
| nativ (81) | 77 | 2 | 2 |

Ohne die zweite Stufe haetten 84 Bezuege einen geratenen `pi_end`. Bei
`heuristic` ist der Wert eine Naeherung und taugt nicht fuer Vergleiche auf die
Zehntelsekunde; die Metrik sagt das ueber `warnings` auch selbst.

`profileFrame` steht auf dem ersten Messpunkt noch auf dem Wert des
vorangegangenen Bezugs - dieser Wechsel wird verworfen.

Die Rechteckwelle der Visualizer-Aera (`espresso_state_change`, Sprung zwischen
`-10000000` und `+10000000` bei jedem Wechsel) wird nicht mehr gelesen. Sie
sagte *dass* gewechselt wurde, nicht *wozu*; ein Test auf den alten Fixtures
haelt fest, dass sie ignoriert wird und diese Bezuege ueber den Heuristikpfad
laufen.

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

## Visualizer-API — verifizierter Stand (historisch)

> Seit M8 ist Decaid die Quelle; dieser Abschnitt beschreibt die Aera davor.
> Er bleibt, weil der Upload nach visualizer.coffee als Schaufenster
> weiterlaufen kann und die Fixtures dazu im Repo sind.

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

**`data/shots.db` ist eine Entwicklungskopie, kein Bestand.** Sie enthaelt, was
ein Sync zum Zeitpunkt X von der API holen konnte, und driftet danach beliebig
weit von der Produktionsinstanz weg. Zwei Gruende: sie wird nur bei Bedarf
synchronisiert, und Visualizer Free haelt nur ein 1-Monats-Fenster vor — was
dort herausgefallen ist, existiert **nur** noch in der Produktionsinstanz und
laesst sich lokal nicht mehr nachziehen. Genau das ist der Archivzweck aus
SPEC ss1. Zahlen aus dieser Datei sind daher nie eine Aussage ueber den echten
Bestand; dafuer `status()` gegen die Produktionsinstanz fragen.

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
| `WRITE_ENABLED` | `false` | Schaltet `update_shot` frei (SPEC ss18). Aus heisst: Tool existiert nicht |
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

Nach dem Verbinden lohnt ein Blick auf `status()`: `version` und `build_ref`
sagen, welche Fassung wirklich laeuft — nicht welche gebaut wurde.

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
