"""Structured key=value logging on stdout with secret redaction.

SPEC §12 calls for structured logs, SPEC §10.3 for credentials and complete
secret URLs never falling out. The redaction deliberately sits in the
*formatter* rather than in a ``logging.Filter``: that way it also catches
tracebacks, ``extra`` fields and everything third-party libraries (httpx,
uvicorn) format - which is exactly where a secret would otherwise slip
through.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "***REDACTED***"

#: Shorter values are not replaced - otherwise a password like "abc" would
#: shred every other log line. ``Config.startup_warnings`` points it out when
#: the configured password falls below this.
MIN_REDACT_LEN = 8

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn appends an ANSI-coloured second copy of the message.
    "color_message",
}


#: Catches Authorization headers regardless of which secret they carry -
#: including one the configuration knows nothing about (third-party library,
#: a forward, a future OAuth token).
#: The redaction runs on already formatted text in which quotes are escaped
#: (\"authorization\": \"Basic ...\"), so the expression has to read the
#: backslash along with it.
_AUTH_HEADER = re.compile(
    r"(authorization(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?)"
    r"(?:(basic|bearer|digest)\s+)?"
    r"([^\s\"',}\]\\]{4,})",
    re.IGNORECASE,
)


def _mask_auth(match: re.Match[str]) -> str:
    scheme = f"{match.group(2)} " if match.group(2) else ""
    return f"{match.group(1)}{scheme}{REDACTED}"


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replaces known secrets and every Authorization header.

    Two stages, because the first alone is not enough: the password travels
    base64-encoded, not in the clear. The configuration therefore also hands
    over the encoded token (``Config.basic_auth_token``), and the header
    expression additionally catches what nobody here knows about.
    """
    for secret in secrets:
        if secret and len(secret) >= MIN_REDACT_LEN:
            text = text.replace(secret, REDACTED)
    return _AUTH_HEADER.sub(_mask_auth, text)


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
        self.converter = __import__("time").gmtime  # logs are always UTC (SPEC §6)

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
    """``extra={"fields": {...}}`` preferred, otherwise every non-standard attribute."""
    fields = getattr(record, "fields", None)
    if isinstance(fields, Mapping):
        return fields
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Configures root logging on stdout. Calling it repeatedly is idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(KeyValueFormatter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn brings its own handlers and would otherwise log twice, unredacted.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore",
                 "docket"):
        noisy = logging.getLogger(name)
        noisy.handlers.clear()
        noisy.propagate = True
    # httpx logs every request individually; the sync worker summarises on its
    # own. At DEBUG the individual requests stay visible.
    #
    # docket is fastmcp's background task queue (a hard dependency since 2.14).
    # Its worker starts unconditionally - there is no setting to switch it off -
    # and announces itself with its three built-in demo tasks:
    #
    #   Starting worker 'host#1234' with the following tasks:
    #   * trace(...)  * fail(...)  * sleep(...)
    #
    # None of that is ours: no tool here declares a task_config, so the worker
    # never has anything to do. Four lines of someone else's startup banner in
    # front of our own is worse than useless when reading a log, so it is muted
    # to WARNING. Its warnings stay - they would report a real fault in the
    # queue - and at DEBUG everything comes back.
    #
    # Both levels are set explicitly rather than only the muting one: a logger
    # keeps whatever level it was last given, so setting it on one branch alone
    # would leave a second call at DEBUG still muted - and the promise of
    # idempotence above would be false.
    detail = logging.NOTSET if root.level <= logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(detail)
    logging.getLogger("docket").setLevel(detail)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
