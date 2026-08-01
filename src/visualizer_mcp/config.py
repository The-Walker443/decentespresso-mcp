"""Env-Parsing und Startup-Validierung (SPEC ss11.3).

Grundsatz: Alles, was fehlen oder Unsinn sein kann, faellt beim Start auf - nicht
erst beim ersten Sync um 3 Uhr nachts. ``Config.from_env`` sammelt daher *alle*
Fehler und wirft sie gebuendelt.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from . import __version__

#: MCP_PATH_SECRET ersetzt die Authentifizierung (SPEC ss10.1) und muss daher
#: nicht ratbar sein. ``openssl rand -hex 24`` liefert 48 Zeichen.
MIN_SECRET_LEN = 32
_SECRET_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

#: Platzhalter aus .env.example - wer die uebernimmt, hat die Datei nicht gelesen.
_PLACEHOLDERS = {"change-me", "you@example.com", "<openssl rand -hex 24>"}


class ConfigError(ValueError):
    """Ungueltige oder fehlende Konfiguration. Enthaelt alle Einzelfehler."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"Ungueltige Konfiguration:\n  - {joined}")


@dataclass(frozen=True, repr=False)
class Config:
    visualizer_email: str
    visualizer_password: str
    mcp_path_secret: str
    sync_interval_min: int
    db_path: str
    log_level: str
    public_base_url: str | None
    display_tz: str
    host: str
    port: int

    @property
    def mcp_path(self) -> str:
        """Pfad, unter dem der MCP-Endpoint haengt - enthaelt das Secret."""
        return f"/{self.mcp_path_secret}/mcp"

    @property
    def connector_url(self) -> str | None:
        """Volle URL fuer den Claude-Connector. Enthaelt das Secret - nicht loggen."""
        if not self.public_base_url:
            return None
        return f"{self.public_base_url.rstrip('/')}{self.mcp_path}"

    @property
    def user_agent(self) -> str:
        """SPEC ss4: hoefliches Pollen mit identifizierbarem UA inkl. Kontaktadresse."""
        return f"visualizer-mcp/{__version__} (privat, {self.visualizer_email})"

    @property
    def basic_auth_token(self) -> str:
        """Der base64-Teil des ``Authorization: Basic``-Headers.

        Eine Redaction, die nur das Klartextpasswort kennt, laesst genau die
        Form durch, in der das Passwort tatsaechlich ueber die Leitung geht.
        """
        raw = f"{self.visualizer_email}:{self.visualizer_password}".encode()
        return base64.b64encode(raw).decode()

    def startup_warnings(self) -> list[str]:
        """Nicht-fatale Befunde, die beim Start ins Log gehoeren.

        Getrennt von ``ConfigError``: das hier verhindert keinen Start, sollte
        aber auffallen.
        """
        from .logging_setup import MIN_REDACT_LEN

        warnings: list[str] = []
        if len(self.visualizer_password) < MIN_REDACT_LEN:
            # Der Log-Filter laesst zu kurze Werte durch, weil er sonst jede
            # zufaellige Uebereinstimmung im Text zerschiessen wuerde. Ein so
            # kurzes Passwort kann also in einer Logzeile stehenbleiben.
            warnings.append(
                f"VISUALIZER_PASSWORD ist kuerzer als {MIN_REDACT_LEN} Zeichen und "
                "wird deshalb NICHT aus Logs entfernt. Bitte ein laengeres setzen."
            )
        return warnings

    def secret_values(self) -> tuple[str, ...]:
        """Werte, die der Log-Filter (``logging_setup``) nie durchlassen darf.

        Die E-Mail steht bewusst nicht drin: sie gehoert laut SPEC ss4 in den
        User-Agent und waere sonst in genau der Zeile unkenntlich, die man beim
        Debuggen eines 401 braucht. Kritisch ist das Passwort, nicht die Kennung.
        """
        return (self.visualizer_password, self.mcp_path_secret, self.basic_auth_token)

    def __repr__(self) -> str:
        return (
            "Config("
            f"visualizer_email={self.visualizer_email!r}, "
            "visualizer_password='***', "
            f"mcp_path_secret='***({len(self.mcp_path_secret)} chars)', "
            f"sync_interval_min={self.sync_interval_min}, "
            f"db_path={self.db_path!r}, "
            f"log_level={self.log_level!r}, "
            f"public_base_url={self.public_base_url!r}, "
            f"display_tz={self.display_tz!r}, "
            f"host={self.host!r}, port={self.port})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        src = os.environ if env is None else env
        problems: list[str] = []

        email = _req_str(src, "VISUALIZER_EMAIL", problems)
        password = _req_str(src, "VISUALIZER_PASSWORD", problems)
        secret = _req_str(src, "MCP_PATH_SECRET", problems)

        if secret is not None:
            if len(secret) < MIN_SECRET_LEN:
                problems.append(
                    f"MCP_PATH_SECRET ist {len(secret)} Zeichen lang, "
                    f"mindestens {MIN_SECRET_LEN} noetig (openssl rand -hex 24)"
                )
            if not _SECRET_CHARSET.match(secret):
                problems.append(
                    "MCP_PATH_SECRET darf nur [A-Za-z0-9_-] enthalten "
                    "(es wird Teil der URL)"
                )

        # 0 schaltet die Hintergrundschleife ab (manueller Sync bleibt moeglich).
        interval = _int_in_range(src, "SYNC_INTERVAL_MIN", 15, 0, 1440, problems)
        db_path = src.get("DB_PATH", "/data/shots.db").strip() or "/data/shots.db"
        if not _is_absolute_path(db_path):
            # Relative Pfade zeigen im Container ins Nirgendwo.
            problems.append(f"DB_PATH muss absolut sein, ist aber {db_path!r}")

        log_level = src.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in _LOG_LEVELS:
            problems.append(
                f"LOG_LEVEL={log_level!r} unbekannt, erlaubt: {sorted(_LOG_LEVELS)}"
            )

        base_url = (src.get("PUBLIC_BASE_URL") or "").strip() or None
        if base_url and not base_url.startswith(("http://", "https://")):
            problems.append("PUBLIC_BASE_URL muss mit http:// oder https:// beginnen")

        tz = (src.get("TZ") or "Europe/Berlin").strip() or "Europe/Berlin"
        host = (src.get("HOST") or "0.0.0.0").strip() or "0.0.0.0"  # noqa: S104
        port = _int_in_range(src, "PORT", 8000, 1, 65535, problems)

        if problems:
            raise ConfigError(problems)

        return cls(
            visualizer_email=email,  # type: ignore[arg-type]
            visualizer_password=password,  # type: ignore[arg-type]
            mcp_path_secret=secret,  # type: ignore[arg-type]
            sync_interval_min=interval,
            db_path=db_path,
            log_level=log_level,
            public_base_url=base_url,
            display_tz=tz,
            host=host,
            port=port,
        )


def _is_absolute_path(path: str) -> bool:
    """Absolut im Container (POSIX) wie in der lokalen Entwicklung (Windows).

    Die Pruefung ist bewusst plattformunabhaengig: der Container laeuft unter
    Linux, entwickelt wird aber auch unter Windows - ``os.path.isabs`` wuerde je
    nach Host unterschiedlich urteilen.
    """
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _req_str(src: Mapping[str, str], key: str, problems: list[str]) -> str | None:
    value = (src.get(key) or "").strip()
    if not value:
        problems.append(f"{key} fehlt oder ist leer")
        return None
    if value in _PLACEHOLDERS:
        problems.append(f"{key} steht noch auf dem Platzhalter aus .env.example")
        return None
    return value


def _int_in_range(
    src: Mapping[str, str],
    key: str,
    default: int,
    low: int,
    high: int,
    problems: list[str],
) -> int:
    raw = (src.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{key}={raw!r} ist keine ganze Zahl")
        return default
    if not low <= value <= high:
        problems.append(f"{key}={value} liegt ausserhalb von {low}..{high}")
        return default
    return value
