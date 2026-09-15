from __future__ import annotations

import pytest

from decentespresso_mcp.config import Config, ConfigError
from decentespresso_mcp.guards import ALL_RULES

from .conftest import TEST_SECRET


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

@pytest.mark.parametrize(
    "key", ["MCP_PATH_SECRET", "DECAID_URL"]
)
def test_missing_required_value_fails(valid_env: dict[str, str], key: str) -> None:
    del valid_env[key]
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert any(key in p for p in excinfo.value.problems)


def test_empty_string_counts_as_missing(valid_env: dict[str, str]) -> None:
    valid_env["MCP_PATH_SECRET"] = "   "
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


def test_placeholder_from_env_example_is_rejected(valid_env: dict[str, str]) -> None:
    valid_env["MCP_PATH_SECRET"] = "<openssl rand -hex 24>"
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert sum("placeholder" in p for p in excinfo.value.problems) == 1


def test_short_secret_is_rejected(valid_env: dict[str, str]) -> None:
    valid_env["MCP_PATH_SECRET"] = "a" * 31
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert any("31 characters" in p for p in excinfo.value.problems)


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
    # The container runs on Linux, development also happens on Windows.
    valid_env["DB_PATH"] = raw
    assert Config.from_env(valid_env).db_path == raw


def test_base_url_needs_scheme(valid_env: dict[str, str]) -> None:
    valid_env["PUBLIC_BASE_URL"] = "coffee-mcp.example.com"
    with pytest.raises(ConfigError):
        Config.from_env(valid_env)


def test_all_problems_are_reported_at_once(valid_env: dict[str, str]) -> None:
    # One start should show every problem, not one per restart.
    valid_env["MCP_PATH_SECRET"] = "short"
    valid_env["LOG_LEVEL"] = "CHATTY"
    valid_env["DECAID_URL"] = "https://decaid.example.com"
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert len(excinfo.value.problems) >= 3

def test_a_proper_secret_warns_about_nothing(config: Config) -> None:
    assert config.startup_warnings() == []


def test_a_barely_long_enough_secret_is_worth_a_word(valid_env) -> None:
    """It passes validation, and it still stands in for authentication."""
    valid_env["MCP_PATH_SECRET"] = "a" * 33
    warnings = Config.from_env(valid_env).startup_warnings()
    assert len(warnings) == 1
    assert "MCP_PATH_SECRET" in warnings[0]

# --------------------------------------------------- DECAID_URL (SPEC ss20.6)


def test_decaid_url_is_required(valid_env: dict[str, str]) -> None:
    del valid_env["DECAID_URL"]
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(valid_env)
    assert any("DECAID_URL is missing" in p for p in excinfo.value.problems)


@pytest.mark.parametrize("url", [
    "http://10.100.100.171:8080",
    "http://192.168.1.50:8080",
    "http://172.16.0.9:8080",
    "http://127.0.0.1:8080",
    "http://169.254.1.1:8080",
])
def test_private_addresses_are_accepted(valid_env: dict[str, str], url: str) -> None:
    assert Config.from_env({**valid_env, "DECAID_URL": url}).decaid_url == url


def test_public_address_is_refused(valid_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({**valid_env, "DECAID_URL": "http://8.8.8.8:8080"})
    assert "public address" in excinfo.value.problems[0]


def test_hostname_is_refused(valid_env: dict[str, str]) -> None:
    # A name can be repointed later without the configuration changing - the
    # shot traffic might then run out onto the open internet.
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({**valid_env, "DECAID_URL": "http://tablet.local:8080"})
    assert "hostname" in excinfo.value.problems[0]


def test_wrong_scheme_is_refused(valid_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Config.from_env({**valid_env, "DECAID_URL": "ftp://10.0.0.1"})


def test_ntfy_url_without_topic_is_refused(valid_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({**valid_env, "NTFY_URL": "http://ntfy.example.com"})
    assert "NTFY_TOPIC" in excinfo.value.problems[0]


def test_ntfy_is_optional(config: Config) -> None:
    assert config.ntfy_url is None and config.ntfy_topic is None


# ------------------------------------------------------ Waechterregeln


def test_guard_rules_default_to_all(valid_env: dict[str, str]) -> None:
    assert Config.from_env(valid_env).guard_rules == ALL_RULES


def test_an_empty_setting_is_not_an_off_switch(valid_env: dict[str, str]) -> None:
    """Whoever copies .env.example leaves the line blank.

    Reading that as "no guards" would have switched them off silently -
    for switching off there is the explicit "none".
    """
    assert Config.from_env({**valid_env, "GUARD_RULES": ""}).guard_rules == ALL_RULES
    assert Config.from_env({**valid_env, "GUARD_RULES": "  "}).guard_rules == ALL_RULES


def test_none_switches_the_guards_off(valid_env: dict[str, str]) -> None:
    assert Config.from_env({**valid_env, "GUARD_RULES": "none"}).guard_rules == ()


def test_a_selection_is_kept(valid_env: dict[str, str]) -> None:
    config = Config.from_env({**valid_env, "GUARD_RULES": "bean_age, dose_outlier"})
    assert config.guard_rules == ("bean_age", "dose_outlier")


def test_a_typo_in_a_rule_name_is_refused(valid_env: dict[str, str]) -> None:
    """Otherwise one would believe a rule was running that does not exist."""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({**valid_env, "GUARD_RULES": "bean_age,bohnenalter"})
    assert "bohnenalter" in str(excinfo.value)
    assert "bean_age" in str(excinfo.value), "the message names the valid names"


def test_thresholds_have_sensible_defaults(valid_env: dict[str, str]) -> None:
    config = Config.from_env(valid_env)
    assert config.bean_age_warn_days == 42
    assert config.rating_grace_hours == 36
    assert config.dose_tolerance_g == 1.0


def test_a_german_decimal_comma_is_accepted(valid_env: dict[str, str]) -> None:
    assert Config.from_env({**valid_env, "DOSE_TOLERANCE_G": "0,5"}).dose_tolerance_g == 0.5


@pytest.mark.parametrize(("name", "value"), [
    ("BEAN_AGE_WARN_DAYS", "0"),
    ("RATING_GRACE_HOURS", "0"),
    ("DOSE_TOLERANCE_G", "99"),
    ("DOSE_TOLERANCE_G", "none at all"),
])
def test_an_impossible_threshold_is_refused(valid_env: dict[str, str], name, value) -> None:
    with pytest.raises(ConfigError):
        Config.from_env({**valid_env, name: value})
