"""Env-Parsing und Startup-Validierung (SPEC ss11.3).

Grundsatz: Alles, was fehlen oder Unsinn sein kann, faellt beim Start auf - nicht
erst beim ersten Sync um 3 Uhr nachts. ``Config.from_env`` sammelt daher *alle*
Fehler und wirft sie gebuendelt.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

from . import __version__
from .guards import (
    ALL_RULES,
    BEAN_AGE_WARN_DAYS,
    DOSE_TOLERANCE_G,
    RATING_GRACE_HOURS,
)

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
    #: SPEC ss18.4. Ist er aus, existiert das Schreibtool gar nicht - es lehnt
    #: nicht ab, es steht nicht in der Tool-Liste.
    write_enabled: bool
    #: SPEC ss20: Decaid im LAN ist ab M8 die Quelle. Nur private Adressen.
    decaid_url: str
    #: Benachrichtigung der Waechter (SPEC ss20.7); leer = keine.
    ntfy_url: str | None
    ntfy_topic: str | None
    ntfy_token: str | None
    #: Eingeschaltete Waechterregeln (SPEC ss20.7). Jede einzeln
    #: abschaltbar - eine Regel, die zu oft anschlaegt, wuerde sonst im
    #: Ganzen ignoriert und naehme die anderen mit.
    guard_rules: tuple[str, ...]
    #: Schwellen der Regeln.
    bean_age_warn_days: int
    rating_grace_hours: int
    dose_tolerance_g: float

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
            f"host={self.host!r}, port={self.port}, "
            f"write_enabled={self.write_enabled}, "
            f"decaid_url={self.decaid_url!r}, "
            f"ntfy={'an' if self.ntfy_url else 'aus'})"
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
        write_enabled = _bool(src, "WRITE_ENABLED", default=False, problems=problems)

        decaid_url = (src.get("DECAID_URL") or "").strip()
        if not decaid_url:
            problems.append(
                "DECAID_URL fehlt - ab M8 ist Decaid im LAN die Quelle "
                "(z. B. http://10.100.100.171:8080)"
            )
        else:
            problems.extend(_lan_url_problems(decaid_url))

        ntfy_url = (src.get("NTFY_URL") or "").strip() or None
        ntfy_topic = (src.get("NTFY_TOPIC") or "").strip() or None
        ntfy_token = (src.get("NTFY_TOKEN") or "").strip() or None
        if ntfy_url and not ntfy_topic:
            problems.append("NTFY_URL ist gesetzt, aber NTFY_TOPIC fehlt")

        guard_rules = _rules(src, problems)
        bean_age_warn_days = _int_in_range(src, "BEAN_AGE_WARN_DAYS",
                                          BEAN_AGE_WARN_DAYS, 1, 3650, problems)
        rating_grace_hours = _int_in_range(src, "RATING_GRACE_HOURS",
                                          RATING_GRACE_HOURS, 1, 24 * 90, problems)
        dose_tolerance_g = _float_in_range(src, "DOSE_TOLERANCE_G",
                                          DOSE_TOLERANCE_G, 0.1, 10.0, problems)

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
            write_enabled=write_enabled,
            decaid_url=decaid_url,
            ntfy_url=ntfy_url,
            ntfy_topic=ntfy_topic,
            ntfy_token=ntfy_token,
            guard_rules=guard_rules,
            bean_age_warn_days=bean_age_warn_days,
            rating_grace_hours=rating_grace_hours,
            dose_tolerance_g=dose_tolerance_g,
        )


def _rules(src, problems: list[str]) -> tuple[str, ...]:
    """``GUARD_RULES`` als Liste von Regelnamen; leer heisst: keine Waechter.

    Ohne Angabe laufen alle. Ein Tippfehler wird abgewiesen statt still
    ignoriert - sonst glaubte man, eine Regel laufe, die es nicht gibt.
    """
    # Leer heisst nicht "keine" - wer .env.example kopiert, hat die Zeile leer
    # stehen und will damit die Vorgabe, nicht die Abschaltung. Zum Abschalten
    # gibt es das ausdrueckliche "none".
    raw = (src.get("GUARD_RULES") or "").strip()
    if not raw:
        return ALL_RULES
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    if names == ("none",):
        return ()
    unknown = [n for n in names if n not in ALL_RULES]
    if unknown:
        problems.append(
            "GUARD_RULES kennt " + ", ".join(unknown) + " nicht - erlaubt sind "
            + ", ".join(ALL_RULES) + " oder none"
        )
    return names


def _float_in_range(src, name: str, default: float, low: float, high: float,
                    problems: list[str]) -> float:
    raw = (src.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        problems.append(f"{name} ist keine Zahl: {raw!r}")
        return default
    if not low <= value <= high:
        problems.append(f"{name}={value} liegt ausserhalb von {low} bis {high}")
    return value


def _lan_url_problems(url: str) -> list[str]:
    """Prueft, dass DECAID_URL auf eine private Adresse zeigt (SPEC ss20.6).

    Nur IP-Literale werden akzeptiert, keine Hostnamen. Ein Name laesst sich
    spaeter umbiegen, ohne dass die Konfiguration sich aendert - und dann liefe
    der Bezugsdatenverkehr womoeglich ins offene Netz. Die Adresse des Tablets
    steht ohnehin fest.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return [f"DECAID_URL muss mit http:// oder https:// beginnen, ist aber {url!r}"]
    if not parsed.hostname:
        return [f"DECAID_URL enthaelt keinen Host: {url!r}"]

    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return [
            f"DECAID_URL={parsed.hostname!r} ist ein Hostname. Erwartet wird eine "
            "feste private IP (z. B. 10.100.100.171) - ein Name kann spaeter "
            "woandershin zeigen, und der Decaid-Verkehr darf das LAN nicht "
            "verlassen."
        ]
    if not (address.is_private or address.is_loopback or address.is_link_local):
        return [
            f"DECAID_URL zeigt auf die oeffentliche Adresse {address}. Decaid wird "
            "ausschliesslich im LAN angesprochen (SPEC ss20.6)."
        ]
    return []


def _bool(
    src: Mapping[str, str], key: str, *, default: bool, problems: list[str]
) -> bool:
    """Nur eindeutige Schreibweisen - bei einem Schreibschalter wird nicht geraten."""
    raw = (src.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    problems.append(
        f"{key}={src.get(key)!r} ist kein Wahrheitswert "
        "(erlaubt: true/false, 1/0, yes/no, on/off)"
    )
    return default


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
