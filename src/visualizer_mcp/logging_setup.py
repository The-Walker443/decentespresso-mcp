"""Strukturiertes key=value-Logging auf stdout mit Secret-Redaction.

SPEC ss12 verlangt strukturierte Logs, SPEC ss10.3 dass niemals Credentials oder
vollstaendige Secret-URLs herausfallen. Die Redaction sitzt bewusst im *Formatter*
und nicht in einem ``logging.Filter``: so erwischt sie auch Tracebacks, ``extra``-
Felder und alles, was Fremdbibliotheken (httpx, uvicorn) formatieren - also genau
die Stellen, an denen ein Secret sonst durchrutscht.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "***REDACTED***"

#: Kuerzere Werte werden nicht ersetzt - sonst zerlegt ein Passwort wie "abc"
#: jede zweite Logzeile.
_MIN_REDACT_LEN = 8

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn haengt eine ANSI-eingefaerbte Zweitfassung der Meldung an.
    "color_message",
}


def redact(text: str, secrets: Iterable[str]) -> str:
    """Ersetzt jedes Secret im Text durch ``***REDACTED***``."""
    for secret in secrets:
        if secret and len(secret) >= _MIN_REDACT_LEN:
            text = text.replace(secret, REDACTED)
    return text


def _fmt_value(value: Any) -> str:
    text = "-" if value is None else str(value)
    if text == "" or any(c in text for c in ' ="\n\t'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text


class KeyValueFormatter(logging.Formatter):
    """``ts=... level=... logger=... msg="..." key=value ...``"""

    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secrets)
        self.converter = __import__("time").gmtime  # Logs immer UTC (SPEC ss6)

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"ts={self.formatTime(record)}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_fmt_value(record.getMessage())}",
        ]
        for key, value in _extra_fields(record).items():
            parts.append(f"{key}={_fmt_value(value)}")
        if record.exc_info:
            parts.append(f"exc={_fmt_value(self.formatException(record.exc_info))}")
        if record.stack_info:
            parts.append(f"stack={_fmt_value(record.stack_info)}")
        return redact(" ".join(parts), self._secrets)


def _extra_fields(record: logging.LogRecord) -> Mapping[str, Any]:
    """``extra={"fields": {...}}`` bevorzugt, sonst alle Nicht-Standard-Attribute."""
    fields = getattr(record, "fields", None)
    if isinstance(fields, Mapping):
        return fields
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Konfiguriert Root-Logging auf stdout. Mehrfachaufruf ist idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(KeyValueFormatter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn bringt eigene Handler mit und wuerde sonst unredigiert doppelt loggen.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        noisy = logging.getLogger(name)
        noisy.handlers.clear()
        noisy.propagate = True
    logging.getLogger("httpcore").setLevel(logging.WARNING)
