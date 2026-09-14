"""SQLite-Zugriff: Migrationen, Upserts, Sync-Zustand (SPEC ss5).

Die Klasse ist synchron und thread-safe ueber eine Lock-geschuetzte Einzel-
verbindung. Der async Sync-Worker ruft sie via ``asyncio.to_thread`` auf - fuer
einen Single-User-Dienst ist das einfacher und robuster als eine async
SQLite-Bibliothek.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def default_migrations_dir() -> Path:
    """Findet ``migrations/`` in Repo-Layout wie im Container.

    Das Paket liegt im Image unter site-packages, die Migrationen aber neben dem
    WORKDIR (/app/migrations) - eine feste paketrelative Ableitung greift daher
    nur beim Editable-Install im Repo.
    """
    override = os.environ.get("MIGRATIONS_DIR")
    candidates = [
        *( [Path(override)] if override else [] ),
        Path(__file__).resolve().parents[2] / "migrations",  # Repo / editable
        Path.cwd() / "migrations",                            # Container-WORKDIR
        Path("/app/migrations"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "migrations/ nicht gefunden, gesucht in: "
        + ", ".join(str(c) for c in candidates)
    )

#: Spalten von shot_series in Einfuegereihenfolge. Deckungsgleich mit dem, was
#: decaid_mapping.series_rows_from_decaid liefert.
SERIES_COLUMNS = (
    "shot_id", "elapsed",
    "pressure", "flow_in", "flow_out", "weight", "temp_mix", "temp_basket", "volume",
    "target_pressure", "target_flow", "target_temp_mix", "target_temp_basket",
    "state", "substate", "profile_frame",
)

#: Deckungsgleich mit decaid_mapping.shot_row_from_decaid.
_SHOT_COLUMNS = (
    "id", "started_at", "time_source", "created_at", "updated_at",
    "duration_s", "stop_reason", "workflow_id", "profile_name",
    "bean_batch_id", "bean_id", "bean_name", "bean_roaster", "basket_name",
    "grinder_model", "grinder_setting",
    "target_dose_g", "target_yield_g", "dose_g", "yield_g", "ratio",
    "enjoyment", "notes", "raw_json", "synced_at",
)

_BEAN_COLUMNS = (
    "id", "name", "roaster", "species", "processing", "decaf", "archived",
    "notes", "created_at", "updated_at", "raw_json", "synced_at",
)

_BATCH_COLUMNS = (
    "id", "bean_id", "roast_date", "buy_date", "freeze_date", "unfreeze_date",
    "frozen", "archived", "created_at", "updated_at", "raw_json", "synced_at",
)


def _upsert_sql(table: str, columns: tuple[str, ...]) -> str:
    sets = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "id")
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)}) "
        f"ON CONFLICT(id) DO UPDATE SET {sets}"
    )

# profile_id bleibt beim Upsert unangetastet - die Verknuepfung setzt die
# Profilversionierung.
_UPSERT_SHOT = _upsert_sql("shots", _SHOT_COLUMNS)
_UPSERT_BEAN = _upsert_sql("beans", _BEAN_COLUMNS)
_UPSERT_BATCH = _upsert_sql("bean_batches", _BATCH_COLUMNS)


#: Migrationen der Visualizer-Aera. Liegt eine davon in einer Datei vor, ist es
#: nicht das Archiv, das dieser Server ab M8 fuehrt.
_VISUALIZER_ERA_MIGRATIONS = frozenset({
    "001_init.sql",
    "002_profile_semantic_hash.sql",
    "003_shot_metrics.sql",
})


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    def __init__(self, path: str | Path, migrations_dir: Path | None = None) -> None:
        self.path = str(path)
        self._migrations_dir = migrations_dir or default_migrations_dir()
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- Migrationen

    def migrate(self) -> list[str]:
        """Wendet alle noch nicht angewendeten ``migrations/*.sql`` an."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {
                row["name"]
                for row in self._conn.execute("SELECT name FROM schema_migrations")
            }
            self._refuse_visualizer_era(done)
            applied: list[str] = []
            for sql_file in sorted(self._migrations_dir.glob("*.sql")):
                if sql_file.name in done:
                    continue
                self._conn.executescript(sql_file.read_text(encoding="utf-8"))
                self._conn.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    (sql_file.name, utc_now_iso()),
                )
                self._conn.commit()
                applied.append(sql_file.name)
            if applied:
                log.info("migrations applied", extra={"fields": {"files": ",".join(applied)}})
            return applied

    def _refuse_visualizer_era(self, applied: set[str]) -> None:
        """Bricht ab, wenn die Datei noch aus der Visualizer-Aera stammt.

        Die neuen Migrationen bestehen aus ``CREATE TABLE IF NOT EXISTS`` - auf
        eine alte Datei angewendet wuerden sie stillschweigend nichts tun und
        das alte Schema stehen lassen, waehrend der Server so taete, als waere
        er auf dem neuen Stand. Der Historien-Reset verlangt eine neue Datei.
        """
        stale = sorted(applied & _VISUALIZER_ERA_MIGRATIONS)
        if stale:
            raise RuntimeError(
                f"{self.path} stammt aus der Visualizer-Aera (angewendet: "
                f"{', '.join(stale)}). Ab M8 ist Decaid die Quelle und das "
                "Schema ein anderes; eine neue Datei anlegen und die alte als "
                "shots-visualizer-era.db behalten."
            )

    # --------------------------------------------------------------------- Shots

    def known_shot_versions(self) -> dict[str, str | None]:
        """``{shot_id: updated_at}`` - Grundlage fuer Dedupe und Update-Erkennung.

        ``updated_at`` ist ISO8601 in UTC und damit lexikographisch
        vergleichbar; Decaid bietet keinen serverseitigen Zeitfilter, der
        Abgleich laeuft deshalb hier.
        """
        with self._lock:
            return {
                row["id"]: row["updated_at"]
                for row in self._conn.execute("SELECT id, updated_at FROM shots")
            }

    def upsert_shot(self, shot: dict[str, Any], series: Sequence[dict[str, Any]]) -> bool:
        """Schreibt Shot + Zeitreihe. Gibt True zurueck, wenn der Shot neu war."""
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM shots WHERE id = ?", (shot["id"],))
            is_new = cur.fetchone() is None
            self._conn.execute(_UPSERT_SHOT, shot)
            # Zeitreihe komplett ersetzen: sie ist unveraenderlich, aber ein
            # abgebrochener Vorlauf koennte Teilstaende hinterlassen haben.
            self._conn.execute("DELETE FROM shot_series WHERE shot_id = ?", (shot["id"],))
            # Metriken haengen an der Zeitreihe und an dose/yield - alle drei
            # koennen sich hier gerade geaendert haben.
            self._conn.execute("DELETE FROM shot_metrics WHERE shot_id = ?", (shot["id"],))
            if series:
                self._conn.executemany(
                    f"INSERT INTO shot_series ({', '.join(SERIES_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in SERIES_COLUMNS)})",
                    [tuple(row[c] for c in SERIES_COLUMNS) for row in series],
                )
            self._conn.commit()
            return is_new

    # ------------------------------------------------- Bohnen und Chargen

    def upsert_beans(self, beans: Sequence[dict[str, Any]]) -> int:
        with self._lock:
            self._conn.executemany(_UPSERT_BEAN, beans)
            self._conn.commit()
        return len(beans)

    def upsert_bean_batches(self, batches: Sequence[dict[str, Any]]) -> int:
        with self._lock:
            self._conn.executemany(_UPSERT_BATCH, batches)
            self._conn.commit()
        return len(batches)

    def link_shots_to_beans(self) -> int:
        """Traegt ``bean_id`` aus der Charge nach.

        Der Bezug nennt nur die Charge; welche Bohne dahintersteht, weiss allein
        bean_batches. Erst nachdem Bohnen und Chargen da sind, laesst sich das
        aufloesen - deshalb ein eigener Schritt am Ende des Abgleichs.
        """
        with self._lock:
            cur = self._conn.execute("""
                UPDATE shots SET bean_id = (
                    SELECT b.bean_id FROM bean_batches b WHERE b.id = shots.bean_batch_id
                )
                WHERE bean_batch_id IS NOT NULL
                  AND bean_id IS NOT (
                    SELECT b.bean_id FROM bean_batches b WHERE b.id = shots.bean_batch_id
                  )
            """)
            self._conn.commit()
            return cur.rowcount

    def count_beans(self) -> tuple[int, int]:
        with self._lock:
            beans = self._conn.execute("SELECT COUNT(*) AS n FROM beans").fetchone()["n"]
            batches = self._conn.execute(
                "SELECT COUNT(*) AS n FROM bean_batches"
            ).fetchone()["n"]
        return beans, batches

    def batch_row(self, batch_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM bean_batches WHERE id = ?", (batch_id,)
            ).fetchone()

    def count_shots(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM shots").fetchone()["n"]

    def count_series_points(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM shot_series"
            ).fetchone()["n"]

    def max_updated_at(self) -> str | None:
        """Juengstes ``updated_at`` im Archiv - der Cursor des Abgleichs."""
        with self._lock:
            row = self._conn.execute("SELECT MAX(updated_at) AS m FROM shots").fetchone()
            return row["m"]

    def shot_span(self) -> tuple[str | None, str | None]:
        """(aeltestes, neuestes) ``started_at`` - fuer status()."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(started_at) AS lo, MAX(started_at) AS hi FROM shots"
            ).fetchone()
            return row["lo"], row["hi"]

    # ------------------------------------------------------------- Abfragen

    def list_beans(self) -> list[dict[str, Any]]:
        """Bohnen mit Bezugszahl, Zeitraum und genutzten Muehleneinstellungen.

        Grundlage ist jetzt Decaids Bohnenliste, nicht mehr der Freitext an den
        Bezuegen: damit tauchen auch Bohnen auf, die zwar angelegt, aber noch
        nicht bezogen wurden, und ein Umbenennen fuehrt nicht zu zwei Eintraegen.

        Die Muehleneinstellungen kommen als eigene Abfrage statt via
        GROUP_CONCAT: sie sind Freitext und enthalten selbst Kommas ("4,2"),
        eine verkettete Liste liesse sich nicht mehr zuverlaessig zerlegen.
        """
        with self._lock:
            beans = list(self._conn.execute("""
                SELECT b.id AS bean_id, b.name AS bean_name, b.roaster,
                       b.species, b.processing, b.decaf, b.archived,
                       COUNT(s.id) AS shot_count,
                       MIN(s.started_at) AS first_shot,
                       MAX(s.started_at) AS last_shot,
                       (SELECT x.grinder_setting FROM shots x
                         WHERE x.bean_id = b.id AND x.grinder_setting IS NOT NULL
                         ORDER BY x.started_at DESC LIMIT 1) AS last_grinder_setting,
                       (SELECT x.grinder_model FROM shots x
                         WHERE x.bean_id = b.id AND x.grinder_model IS NOT NULL
                         ORDER BY x.started_at DESC LIMIT 1) AS last_grinder_model
                FROM beans b
                LEFT JOIN shots s ON s.bean_id = b.id
                GROUP BY b.id
                ORDER BY last_shot IS NULL, last_shot DESC
            """))
            settings: dict[str, list[str]] = {}
            for row in self._conn.execute(
                "SELECT DISTINCT bean_id, grinder_setting FROM shots "
                "WHERE grinder_setting IS NOT NULL AND bean_id IS NOT NULL "
                "ORDER BY grinder_setting"
            ):
                settings.setdefault(row["bean_id"], []).append(row["grinder_setting"])

        return [
            {**dict(bean), "grinder_settings": settings.get(bean["bean_id"], [])}
            for bean in beans
        ]

    def orphan_bean_names(self) -> list[dict[str, Any]]:
        """Bezuege, deren Charge keiner bekannten Bohne zugeordnet ist.

        Aus der de1app importierte Bezuege tragen den Bohnennamen nur als
        Freitext. Ohne diese Ansicht verschwaenden sie aus list_beans.
        """
        with self._lock:
            return [dict(row) for row in self._conn.execute("""
                SELECT bean_name, bean_roaster,
                       COUNT(*) AS shot_count,
                       MIN(started_at) AS first_shot,
                       MAX(started_at) AS last_shot
                FROM shots
                WHERE bean_id IS NULL AND bean_name IS NOT NULL
                GROUP BY bean_name, bean_roaster
                ORDER BY last_shot DESC
            """)]

    def query_shots(
        self,
        *,
        bean: str | None = None,
        roaster: str | None = None,
        profile: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        """Gefilterte Shots plus Gesamtzahl. Textfilter sind Teilstring, case-insensitiv.

        ``bean`` trifft den Bohnennamen *oder* den Roester, ``roaster`` nur
        den Roester.
        """
        where: list[str] = []
        params: list[Any] = []
        if bean:
            where.append("(LOWER(bean_name) LIKE ? OR LOWER(bean_roaster) LIKE ?)")
            params += [f"%{bean.lower()}%"] * 2
        if roaster:
            where.append("LOWER(bean_roaster) LIKE ?")
            params.append(f"%{roaster.lower()}%")
        if profile:
            where.append("LOWER(profile_name) LIKE ?")
            params.append(f"%{profile.lower()}%")
        if since:
            where.append("started_at >= ?")
            params.append(since)
        if until:
            where.append("started_at <= ?")
            params.append(until)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM shots {clause}", params
            ).fetchone()["n"]
            rows = list(self._conn.execute(
                f"SELECT * FROM shots {clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ))
        return rows, total

    def get_shot_row(self, shot_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()

    def latest_shot_id(self, bean: str | None = None) -> str | None:
        clause = ""
        params: list[Any] = []
        if bean:
            clause = "WHERE LOWER(bean_name) LIKE ? OR LOWER(bean_roaster) LIKE ?"
            params = [f"%{bean.lower()}%"] * 2
        with self._lock:
            row = self._conn.execute(
                f"SELECT id FROM shots {clause} ORDER BY started_at DESC LIMIT 1", params
            ).fetchone()
        return row["id"] if row else None

    def get_profile_row(self, profile_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()

    def find_profile(
        self, *, version_hash: str | None = None, name: str | None = None
    ) -> sqlite3.Row | None:
        """Version per Hash-Praefix, sonst die neueste Version eines Namens."""
        with self._lock:
            if version_hash:
                return self._conn.execute(
                    "SELECT * FROM profiles WHERE version_hash LIKE ? ORDER BY last_seen DESC",
                    (f"{version_hash}%",),
                ).fetchone()
            return self._conn.execute(
                "SELECT * FROM profiles WHERE LOWER(name) LIKE ? ORDER BY last_seen DESC",
                (f"%{(name or '').lower()}%",),
            ).fetchone()

    def series_for_shot(self, shot_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                f"SELECT {', '.join(c for c in SERIES_COLUMNS if c != 'shot_id')} "
                "FROM shot_series WHERE shot_id = ? ORDER BY elapsed",
                (shot_id,),
            ))

    def shot_basics(self, shot_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT id, dose_g, yield_g FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()

    # ------------------------------------------------------------------ Metriken

    def get_cached_metrics(self, shot_id: str, version: int) -> dict[str, Any] | None:
        """Cache-Treffer nur bei passender ``metrics_version``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT metrics_json FROM shot_metrics "
                "WHERE shot_id = ? AND metrics_version = ?",
                (shot_id, version),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["metrics_json"])
        except json.JSONDecodeError:
            return None

    def store_metrics(self, shot_id: str, version: int, metrics: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO shot_metrics (shot_id, metrics_version, metrics_json, computed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(shot_id) DO UPDATE SET "
                "metrics_version = excluded.metrics_version, "
                "metrics_json = excluded.metrics_json, computed_at = excluded.computed_at",
                (shot_id, version, json.dumps(metrics, ensure_ascii=False), utc_now_iso()),
            )
            self._conn.commit()

    def shot_ids_without_metrics(self, version: int) -> list[str]:
        """Shots ohne gueltigen Cache - inklusive derer mit veralteter Version."""
        with self._lock:
            return [
                row["id"]
                for row in self._conn.execute(
                    "SELECT s.id FROM shots s "
                    "LEFT JOIN shot_metrics m ON m.shot_id = s.id "
                    "WHERE m.shot_id IS NULL OR m.metrics_version != ? "
                    "ORDER BY s.started_at",
                    (version,),
                )
            ]

    def count_metrics(self, version: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM shot_metrics WHERE metrics_version = ?", (version,)
            ).fetchone()["n"]

    # ------------------------------------------------------------------ Profile

    def shot_ids_without_profile(self) -> list[str]:
        """Shots, denen noch eine Profilversion fehlt (SPEC ss6.4)."""
        with self._lock:
            return [
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM shots WHERE profile_id IS NULL ORDER BY started_at"
                )
            ]

    def upsert_profile(
        self,
        *,
        name: str,
        version_hash: str,
        raw_json: str,
        parsed_json: str,
        profile_notes: str | None,
        seen_at: str,
        semantic_hash: str | None = None,
        source: str = "decaid",
    ) -> tuple[int, bool]:
        """Legt die Profilversion an oder aktualisiert nur ``last_seen``.

        Rueckgabe ``(profile_id, is_new)``. Dedupe laeuft ueber ``version_hash``
        (SPEC ss5): dieselbe Version bekommt nie einen zweiten Datensatz, und
        alte Shots bleiben an genau der Version haengen, mit der sie liefen.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM profiles WHERE version_hash = ?", (version_hash,)
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE profiles SET last_seen = ? WHERE id = ?", (seen_at, row["id"])
                )
                self._conn.commit()
                return row["id"], False

            cursor = self._conn.execute(
                "INSERT INTO profiles "
                "(name, version_hash, semantic_hash, source, raw_json, parsed_json, "
                " profile_notes, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, version_hash, semantic_hash, source, raw_json, parsed_json,
                 profile_notes, seen_at, seen_at),
            )
            self._conn.commit()
            return int(cursor.lastrowid), True

    def link_shot_profile(self, shot_id: str, profile_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE shots SET profile_id = ? WHERE id = ?", (profile_id, shot_id)
            )
            self._conn.commit()

    def count_profiles(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]

    def profile_overview(self) -> list[dict[str, Any]]:
        """Je Profilversion: Name, Hash, Zeitraum, Anzahl verknuepfter Shots."""
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT p.id, p.name, p.version_hash, p.semantic_hash, "
                    "       p.first_seen, p.last_seen, COUNT(s.id) AS shot_count "
                    "FROM profiles p LEFT JOIN shots s ON s.profile_id = p.id "
                    "GROUP BY p.id ORDER BY p.name, p.first_seen"
                )
            ]

    # ---------------------------------------------------------------- sync_state

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_json_state(self, key: str, default: Any = None) -> Any:
        raw = self.get_state(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json_state(self, key: str, value: Any) -> None:
        self.set_state(key, json.dumps(value, ensure_ascii=False))

    def record_errors(self, errors: Iterable[str], keep: int = 20) -> None:
        """Haengt Fehler an ``sync_state['last_errors']`` an (SPEC ss6.5, max 20)."""
        new = list(errors)
        if not new:
            return
        stamped = [{"at": utc_now_iso(), "error": e} for e in new]
        existing = self.get_json_state("last_errors", [])
        if not isinstance(existing, list):
            existing = []
        self.set_json_state("last_errors", (existing + stamped)[-keep:])
