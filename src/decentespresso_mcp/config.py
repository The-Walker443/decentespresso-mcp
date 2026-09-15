"""Env parsing and startup validation (SPEC §14).

Principle: anything that can be missing or nonsense shows up at startup - not
at the first sync at three in the morning. ``Config.from_env`` therefore
collects *every* problem and raises them together.
"""

from __future__ import annotations

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

#: MCP_PATH_SECRET stands in for authentication (SPEC §12) and must
#: therefore not be guessable. ``openssl rand -hex 24`` yields 48 characters.
MIN_SECRET_LEN = 32
_SECRET_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

#: Placeholders from .env.example - anyone leaving them has not read the file.
_PLACEHOLDERS = {"change-me", "you@example.com", "<openssl rand -hex 24>"}



class ConfigError(ValueError):
    """Invalid or missing configuration. Carries every single problem."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"Invalid configuration:\n  - {joined}")


@dataclass(frozen=True, repr=False)
class Config:
    mcp_path_secret: str
    sync_interval_min: int
    db_path: str
    log_level: str
    public_base_url: str | None
    display_tz: str
    host: str
    port: int
    #: SPEC §11. When off, the write tools do not exist at all - they do not
    #: refuse, they are absent from the tool list.
    write_enabled: bool
    #: SPEC §4: Decaid on the local network is the source. Private addresses
    #: only.
    decaid_url: str
    #: Guard notifications (SPEC §10); empty means none.
    ntfy_url: str | None
    ntfy_topic: str | None
    ntfy_token: str | None
    #: Enabled guard rules (SPEC §10). Each can be switched off on its own -
    #: a rule that fires too often would otherwise be ignored wholesale and
    #: take the others with it.
    guard_rules: tuple[str, ...]
    #: Thresholds of the rules.
    bean_age_warn_days: int
    rating_grace_hours: int
    dose_tolerance_g: float

    @property
    def mcp_path(self) -> str:
        """Path the MCP endpoint hangs under - contains the secret."""
        return f"/{self.mcp_path_secret}/mcp"

    @property
    def connector_url(self) -> str | None:
        """Full URL for the Claude connector. Contains the secret - do not log."""
        if not self.public_base_url:
            return None
        return f"{self.public_base_url.rstrip('/')}{self.mcp_path}"

    @property
    def user_agent(self) -> str:
        """Identifies this server to Decaid. No contact address: the tablet is
        on the same network and belongs to the same person."""
        return f"decentespresso-mcp/{__version__} (private, LAN)"

    def startup_warnings(self) -> list[str]:
        """Non-fatal findings that belong in the log at startup.

        Kept apart from ``ConfigError``: these do not prevent a start but should
        still be noticed.
        """
        warnings: list[str] = []
        if len(self.mcp_path_secret) < MIN_SECRET_LEN + 8:
            # Long enough to pass validation, short enough to be worth a word:
            # this path secret stands in for authentication entirely.
            warnings.append(
                f"MCP_PATH_SECRET is {len(self.mcp_path_secret)} characters. It "
                "replaces authentication outright - openssl rand -hex 24 gives 48."
            )
        return warnings

    def secret_values(self) -> tuple[str, ...]:
        """Values the log filter (``logging_setup``) must never let through.

        Decaid needs no credentials - the tablet is on the same network - so
        what is left is the path secret, which stands in for authentication,
        and the ntfy token, which travels as ``Authorization: Bearer``.

        The ntfy token was missing here until the Visualizer credentials were
        removed; with three other entries in the tuple, nobody noticed that it
        was not one of them.
        """
        return tuple(v for v in (self.mcp_path_secret, self.ntfy_token) if v)

    def __repr__(self) -> str:
        return (
            "Config("
            f"mcp_path_secret='***({len(self.mcp_path_secret)} chars)', "
            f"sync_interval_min={self.sync_interval_min}, "
            f"db_path={self.db_path!r}, "
            f"log_level={self.log_level!r}, "
            f"public_base_url={self.public_base_url!r}, "
            f"display_tz={self.display_tz!r}, "
            f"host={self.host!r}, port={self.port}, "
            f"write_enabled={self.write_enabled}, "
            f"decaid_url={self.decaid_url!r}, "
            f"ntfy={'on' if self.ntfy_url else 'off'})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        src = os.environ if env is None else env
        problems: list[str] = []

        secret = _req_str(src, "MCP_PATH_SECRET", problems)

        if secret is not None:
            if len(secret) < MIN_SECRET_LEN:
                problems.append(
                    f"MCP_PATH_SECRET is {len(secret)} characters long, "
                    f"mindestens {MIN_SECRET_LEN} noetig (openssl rand -hex 24)"
                )
            if not _SECRET_CHARSET.match(secret):
                problems.append(
                    "MCP_PATH_SECRET may only contain [A-Za-z0-9_-] "
                    "(it becomes part of the URL)"
                )

        # 0 switches the background loop off (a manual sync stays possible).
        interval = _int_in_range(src, "SYNC_INTERVAL_MIN", 15, 0, 1440, problems)
        db_path = src.get("DB_PATH", "/data/shots.db").strip() or "/data/shots.db"
        if not _is_absolute_path(db_path):
            # Relative paths point nowhere inside the container.
            problems.append(f"DB_PATH must be absolute, but is {db_path!r}")

        log_level = src.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in _LOG_LEVELS:
            problems.append(
                f"LOG_LEVEL={log_level!r} is unknown, allowed: {sorted(_LOG_LEVELS)}"
            )

        base_url = (src.get("PUBLIC_BASE_URL") or "").strip() or None
        if base_url and not base_url.startswith(("http://", "https://")):
            problems.append("PUBLIC_BASE_URL must start with http:// or https://")

        tz = (src.get("TZ") or "Europe/Berlin").strip() or "Europe/Berlin"
        host = (src.get("HOST") or "0.0.0.0").strip() or "0.0.0.0"  # noqa: S104
        port = _int_in_range(src, "PORT", 8000, 1, 65535, problems)
        write_enabled = _bool(src, "WRITE_ENABLED", default=False, problems=problems)

        decaid_url = (src.get("DECAID_URL") or "").strip()
        if not decaid_url:
            problems.append(
                "DECAID_URL is missing - Decaid on the local network is the "
                "source "
                "(z. B. http://10.100.100.171:8080)"
            )
        else:
            problems.extend(_lan_url_problems(decaid_url))

        ntfy_url = (src.get("NTFY_URL") or "").strip() or None
        ntfy_topic = (src.get("NTFY_TOPIC") or "").strip() or None
        ntfy_token = (src.get("NTFY_TOKEN") or "").strip() or None
        if ntfy_url and not ntfy_topic:
            problems.append("NTFY_URL is set but NTFY_TOPIC is missing")

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
    """``GUARD_RULES`` as a list of rule names; "none" means no guards.

    Without a setting, all of them run. A typo is refused rather than silently
    ignored - otherwise one would believe a rule was running that does not
    exist.
    """
    # Empty does not mean "none" - whoever copies .env.example leaves the line
    # blank and means the default by it, not a shutdown. For switching off
    # there is the explicit "none".
    raw = (src.get("GUARD_RULES") or "").strip()
    if not raw:
        return ALL_RULES
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    if names == ("none",):
        return ()
    unknown = [n for n in names if n not in ALL_RULES]
    if unknown:
        problems.append(
            "GUARD_RULES does not know " + ", ".join(unknown) + " - allowed are "
            + ", ".join(ALL_RULES) + " or none"
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
        problems.append(f"{name} is not a number: {raw!r}")
        return default
    if not low <= value <= high:
        problems.append(f"{name}={value} is outside the range {low} to {high}")
    return value


def _lan_url_problems(url: str) -> list[str]:
    """Checks that DECAID_URL points at a private address (SPEC §12).

    Only IP literals are accepted, no hostnames. A name can be repointed later
    without the configuration changing - and then the shot traffic might run
    out onto the open internet. The tablet's address is fixed anyway.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return [f"DECAID_URL must start with http:// or https://, but is {url!r}"]
    if not parsed.hostname:
        return [f"DECAID_URL carries no host: {url!r}"]

    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return [
            f"DECAID_URL={parsed.hostname!r} is a hostname. A fixed private IP "
            "is expected (10.100.100.171, for instance) - a name can be "
            "repointed later, and the Decaid traffic must not leave the local "
            "verlassen."
        ]
    if not (address.is_private or address.is_loopback or address.is_link_local):
        return [
            f"DECAID_URL points at the public address {address}. Decaid is "
            "addressed on the local network only (SPEC §12)."
        ]
    return []


def _bool(
    src: Mapping[str, str], key: str, *, default: bool, problems: list[str]
) -> bool:
    """Unambiguous spellings only - a write switch is no place for guessing."""
    raw = (src.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    problems.append(
        f"{key}={src.get(key)!r} is not a boolean "
        "(erlaubt: true/false, 1/0, yes/no, on/off)"
    )
    return default


def _is_absolute_path(path: str) -> bool:
    """Absolute in the container (POSIX) as well as in local development (Windows).

    The check is deliberately platform independent: the container runs on Linux
    but development also happens on Windows - ``os.path.isabs`` would judge
    differently depending on the host.
    """
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _req_str(src: Mapping[str, str], key: str, problems: list[str]) -> str | None:
    value = (src.get(key) or "").strip()
    if not value:
        problems.append(f"{key} is missing or empty")
        return None
    if value in _PLACEHOLDERS:
        problems.append(f"{key} still holds the placeholder from .env.example")
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
        problems.append(f"{key}={raw!r} is not a whole number")
        return default
    if not low <= value <= high:
        problems.append(f"{key}={value} is outside the range {low}..{high}")
        return default
    return value
