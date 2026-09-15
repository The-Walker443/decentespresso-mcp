"""No secret in the logs.

Two secrets exist on this server: the path secret that stands in for
authentication, and the ntfy token that travels as ``Authorization: Bearer``.
"""

from __future__ import annotations

import logging

from decentespresso_mcp.config import Config
from decentespresso_mcp.logging_setup import REDACTED, KeyValueFormatter, setup_logging

from .conftest import TEST_NTFY_TOKEN, TEST_SECRET


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
    fmt = KeyValueFormatter([TEST_NTFY_TOKEN, TEST_SECRET])
    out = fmt.format(_record("connecting to https://host/%s/mcp", TEST_SECRET))
    assert TEST_SECRET not in out
    assert REDACTED in out


def test_secret_in_traceback_is_redacted() -> None:
    fmt = KeyValueFormatter([TEST_NTFY_TOKEN, TEST_SECRET])
    try:
        raise RuntimeError(f"ntfy refused {TEST_NTFY_TOKEN}")
    except RuntimeError:
        import sys

        record = _record("boom")
        record.exc_info = sys.exc_info()
    out = fmt.format(record)
    assert TEST_NTFY_TOKEN not in out
    assert REDACTED in out


def test_secret_in_extra_field_is_redacted() -> None:
    record = _record("request")
    record.fields = {"url": f"https://host/{TEST_SECRET}/mcp"}  # type: ignore[attr-defined]
    out = KeyValueFormatter([TEST_SECRET]).format(record)
    assert TEST_SECRET not in out


def test_short_values_are_not_redacted() -> None:
    # Otherwise a short secret shreds every log line that happens to contain
    # the same character sequence.
    out = KeyValueFormatter(["abc"]).format(_record("abcdef"))
    assert "abcdef" in out


def test_setup_logging_redacts_on_stdout(capsys, notifying_config: Config) -> None:
    setup_logging(notifying_config.log_level,
                  secrets=notifying_config.secret_values())
    logging.getLogger("decentespresso_mcp.test").info(
        "connector at %s, ntfy token %s",
        notifying_config.connector_url, TEST_NTFY_TOKEN,
    )
    out = capsys.readouterr().out
    assert TEST_SECRET not in out
    assert TEST_NTFY_TOKEN not in out
    assert out.count(REDACTED) == 2

    logging.getLogger().handlers.clear()


# ------------------------------------------------------- Third-party noise


def test_the_docket_worker_banner_is_muted(capsys, config: Config) -> None:
    """docket is fastmcp's task queue and announces itself at startup.

    Its worker starts unconditionally - fastmcp offers no setting to switch it
    off - and prints its three built-in demo tasks (trace, fail, sleep). None of
    that is ours, and four lines of someone else's banner in front of our own
    make a log harder to read.
    """
    setup_logging(config.log_level, secrets=config.secret_values())
    worker = logging.getLogger("docket.worker")
    worker.info("Starting worker 'host#1' with the following tasks:")
    worker.info("* trace(message: str, ...)")

    assert capsys.readouterr().out == ""
    logging.getLogger().handlers.clear()


def test_docket_warnings_still_get_through(capsys, config: Config) -> None:
    """Muting is not switching off: a real fault in the queue must show."""
    setup_logging(config.log_level, secrets=config.secret_values())
    logging.getLogger("docket.worker").warning("Failed to renew leases")

    assert "Failed to renew leases" in capsys.readouterr().out
    logging.getLogger().handlers.clear()


def test_debug_brings_the_noise_back(capsys, config: Config) -> None:
    """A logger keeps the level it was last given.

    Setting it only on the muting branch would leave a second call at DEBUG
    still muted, and the idempotence ``setup_logging`` promises would be false.
    """
    setup_logging("INFO", secrets=config.secret_values())
    setup_logging("DEBUG", secrets=config.secret_values())
    logging.getLogger("docket.worker").info("Starting worker 'host#1'")
    logging.getLogger("httpx").info("HTTP Request: GET /api/v1/shots")

    out = capsys.readouterr().out
    assert "Starting worker" in out
    assert "HTTP Request" in out
    logging.getLogger().handlers.clear()


def test_the_ntfy_token_is_on_the_list(notifying_config: Config) -> None:
    """It was not, until the Visualizer credentials left.

    With three other entries in the tuple nobody noticed that the one secret
    this server actually sends outward was missing from it.
    """
    assert TEST_NTFY_TOKEN in notifying_config.secret_values()


def test_without_ntfy_only_the_path_secret_remains(config: Config) -> None:
    assert config.secret_values() == (TEST_SECRET,)
