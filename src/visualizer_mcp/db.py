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

#: Spalten von shot_series in Einfuegereihenfolge.
SERIES_COLUMNS = (
    "shot_id", "elapsed", "pressure", "flow_in", "flow_out",
    "weight", "temp_mix", "temp_basket", "state_change",
)

_SHOT_COLUMNS = (
    "id", "started_at", "bean_brand", "bean_type", "bean_notes", "profile_name",
    "grinder_model", "grinder_setting", "dose_g", "yield_g", "duration_s", "ratio",
    "drink_tds", "drink_ey", "enjoyment", "notes", "private_notes", "raw_json",
    "updated_at", "synced_at",
)

# profile_id bleibt beim Upsert unangetastet - die Verknuepfung setzt M2.
_SHOT_UPDATE_SET = ", ".join(
    f"{c}=excluded.{c}" for c in _SHOT_COLUMNS if c != "id"
)

_UPSERT_SHOT = (
    f"INSERT INTO shots ({', '.join(_SHOT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _SHOT_COLUMNS)}) "
    f"ON CONFLICT(id) DO UPDATE SET {_SHOT_UPDATE_SET}"
)


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

    # --------------------------------------------------------------------- Shots

    def known_shot_versions(self) -> dict[str, int]:
        """``{shot_id: updated_at}`` - Grundlage fuer Dedupe und Update-Erkennung."""
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
            if series:
                self._conn.executemany(
                    f"INSERT INTO shot_series ({', '.join(SERIES_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in SERIES_COLUMNS)})",
                    [tuple(row[c] for c in SERIES_COLUMNS) for row in series],
                )
            self._conn.commit()
            return is_new

    def count_shots(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM shots").fetchone()["n"]

    def count_series_points(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM shot_series"
            ).fetchone()["n"]

    def max_updated_at(self) -> int | None:
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
        raw_tcl: str,
        parsed_json: str,
        profile_notes: str | None,
        seen_at: str,
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
                "(name, version_hash, raw_tcl, parsed_json, profile_notes, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, version_hash, raw_tcl, parsed_json, profile_notes, seen_at, seen_at),
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
                    "SELECT p.id, p.name, p.version_hash, p.first_seen, p.last_seen, "
                    "       COUNT(s.id) AS shot_count "
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
