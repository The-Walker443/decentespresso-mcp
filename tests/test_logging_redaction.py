"""Deckt Abnahmekriterium 6 ab: kein Secret in Logs (SPEC ss13)."""

from __future__ import annotations

import logging

from decentespresso_mcp.config import Config
from decentespresso_mcp.logging_setup import REDACTED, KeyValueFormatter, setup_logging

from .conftest import TEST_PASSWORD, TEST_SECRET


def _record(msg: str, *args: object, **kwargs: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args or None, exc_info=None, **kwargs,  # type: ignore[arg-type]
    )


def test_message_is_key_value_shaped() -> None:
    out = KeyValueFormatter().format(_record("sync done"))
    assert out.startswith("ts=")
    assert " level=INFO " in out
    assert 'msg="sync done"' in out


def test_extra_fields_are_appended() -> None:
    record = _record("sync finished")
    record.fields = {"new": 2, "updated": 1, "dur_ms": 1200}  # type: ignore[attr-defined]
    out = KeyValueFormatter().format(record)
    assert "new=2 updated=1 dur_ms=1200" in out


def test_secret_in_message_is_redacted() -> None:
    fmt = KeyValueFormatter([TEST_PASSWORD, TEST_SECRET])
    out = fmt.format(_record("connecting to https://host/%s/mcp", TEST_SECRET))
    assert TEST_SECRET not in out
    assert REDACTED in out


def test_secret_in_traceback_is_redacted() -> None:
    fmt = KeyValueFormatter([TEST_PASSWORD, TEST_SECRET])
    try:
        raise RuntimeError(f"auth failed for {TEST_PASSWORD}")
    except RuntimeError:
        import sys

        record = _record("boom")
        record.exc_info = sys.exc_info()
    out = fmt.format(record)
    assert TEST_PASSWORD not in out
    assert REDACTED in out


def test_secret_in_extra_field_is_redacted() -> None:
    record = _record("request")
    record.fields = {"url": f"https://host/{TEST_SECRET}/mcp"}  # type: ignore[attr-defined]
    out = KeyValueFormatter([TEST_SECRET]).format(record)
    assert TEST_SECRET not in out


def test_short_values_are_not_redacted() -> None:
    # Sonst zerschiesst ein kurzes Passwort jede Logzeile, die zufaellig
    # dieselbe Zeichenfolge enthaelt.
    out = KeyValueFormatter(["abc"]).format(_record("abcdef"))
    assert "abcdef" in out


def test_setup_logging_redacts_on_stdout(capsys, config: Config) -> None:
    setup_logging(config.log_level, secrets=config.secret_values())
    logging.getLogger("decentespresso_mcp.test").info(
        "connector at %s with password %s", config.connector_url, TEST_PASSWORD
    )
    out = capsys.readouterr().out
    assert TEST_SECRET not in out
    assert TEST_PASSWORD not in out
    assert out.count(REDACTED) == 2

    logging.getLogger().handlers.clear()
