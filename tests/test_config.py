from __future__ import annotations

import pytest

from visualizer_mcp.config import Config, ConfigError

from .conftest import TEST_PASSWORD, TEST_SECRET


def test_defaults_are_applied(config: Config) -> None:
    assert config.sync_interval_min == 15
    assert config.db_path == "/data/shots.db"
    assert config.log_level == "INFO"
    assert config.display_tz == "Europe/Berlin"
    assert config.port == 8000


def test_mcp_path_and_connector_url(config: Config) -> None:
    assert config.mcp_path == f"/{TEST_SECRET}/mcp"
    assert config.connector_url == f"https://coffee-mcp.example.com/{TEST_SECRET}/mcp"


def test_connector_url_is_none_without_base_url(valid_env: dict[str, str]) -> None:
    del valid_env["PUBLIC_BASE_URL"]
    assert Config.from_env(valid_env).connector_url is None


def test_user_agent_identifies_server_and_contact(config: Config) -> None:
    # SPEC ss4: hoeflich pollen heisst identifizierbar sein.
    assert config.user_agent.startswith("visualizer-mcp/")
    assert "shots@example.org" in config.user_agent


@pytest.mark.parametrize(
    "key", ["VISUALIZER_EMAIL", "VISUALIZER_PASSWORD", "MCP_PATH_SECRET"]
)
def test_missing_required_value_fails(valid_env: dict[str, str], key: str) -> None:
    del valid_env[key]
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert any(key in p for p in excinfo.value.problems)


def test_empty_string_counts_as_missing(valid_env: dict[str, str]) -> None:
    valid_env["VISUALIZER_PASSWORD"] = "   "
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


def test_placeholder_from_env_example_is_rejected(valid_env: dict[str, str]) -> None:
    valid_env["VISUALIZER_PASSWORD"] = "change-me"
    valid_env["MCP_PATH_SECRET"] = "<openssl rand -hex 24>"
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert sum("Platzhalter" in p for p in excinfo.value.problems) == 2


def test_short_secret_is_rejected(valid_env: dict[str, str]) -> None:
    valid_env["MCP_PATH_SECRET"] = "a" * 31
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert any("31 Zeichen" in p for p in excinfo.value.problems)


def test_secret_must_be_url_safe(valid_env: dict[str, str]) -> None:
    valid_env["MCP_PATH_SECRET"] = "a/b?" + "x" * 30
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert any("A-Za-z0-9_-" in p for p in excinfo.value.problems)


@pytest.mark.parametrize("raw", ["-1", "1441", "nope", "15.5"])
def test_bad_sync_interval_is_rejected(valid_env: dict[str, str], raw: str) -> None:
    valid_env["SYNC_INTERVAL_MIN"] = raw
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


def test_sync_interval_zero_disables_the_worker(valid_env: dict[str, str]) -> None:
    valid_env["SYNC_INTERVAL_MIN"] = "0"
    assert Config.from_env(valid_env).sync_interval_min == 0


def test_bad_log_level_is_rejected(valid_env: dict[str, str]) -> None:
    valid_env["LOG_LEVEL"] = "CHATTY"
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


def test_log_level_is_case_insensitive(valid_env: dict[str, str]) -> None:
    valid_env["LOG_LEVEL"] = "debug"
    assert Config.from_env(valid_env).log_level == "DEBUG"


@pytest.mark.parametrize("raw", ["shots.db", "./data/shots.db", "data/shots.db"])
def test_relative_db_path_is_rejected(valid_env: dict[str, str], raw: str) -> None:
    valid_env["DB_PATH"] = raw
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


@pytest.mark.parametrize("raw", ["/data/shots.db", "D:/dev/data/shots.db",
                                 r"C:\dev\data\shots.db"])
def test_absolute_db_paths_are_accepted(valid_env: dict[str, str], raw: str) -> None:
    # Container laeuft unter Linux, entwickelt wird auch unter Windows.
    valid_env["DB_PATH"] = raw
    assert Config.from_env(valid_env).db_path == raw


def test_base_url_needs_scheme(valid_env: dict[str, str]) -> None:
    valid_env["PUBLIC_BASE_URL"] = "coffee-mcp.example.com"
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


def test_all_problems_are_reported_at_once(valid_env: dict[str, str]) -> None:
    # Ein Start soll alle Fehler zeigen, nicht einen pro Neustart.
    del valid_env["VISUALIZER_EMAIL"]
    valid_env["MCP_PATH_SECRET"] = "short"
    valid_env["LOG_LEVEL"] = "CHATTY"
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert len(excinfo.value.problems) >= 3


def test_repr_hides_password_and_secret(config: Config) -> None:
    text = repr(config)
    assert TEST_PASSWORD not in text
    assert TEST_SECRET not in text
    assert "***" in text
    # Die Mailadresse bleibt sichtbar - sie steht ohnehin im User-Agent.
    assert "shots@example.org" in text


def test_secret_values_cover_password_and_path_secret(config: Config) -> None:
    assert set(config.secret_values()) == {TEST_PASSWORD, TEST_SECRET}
