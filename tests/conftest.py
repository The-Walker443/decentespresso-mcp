from __future__ import annotations

import pytest

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database

# 48 characters, as 'openssl rand -hex 24' produces.
TEST_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef"

# The one other secret this server holds: ntfy travels as Authorization: Bearer.
TEST_NTFY_TOKEN = "tk_test-token-long-enough"


@pytest.fixture
def valid_env() -> dict[str, str]:
    return {
        "MCP_PATH_SECRET": TEST_SECRET,
        "PUBLIC_BASE_URL": "https://coffee-mcp.example.com",
        # Mandatory, and private addresses only.
        "DECAID_URL": "http://10.100.100.171:8080",
    }


@pytest.fixture
def config(valid_env: dict[str, str]) -> Config:
    return Config.from_env(valid_env)


@pytest.fixture
def notifying_config(valid_env: dict[str, str]) -> Config:
    """A configuration that actually holds a second secret."""
    return Config.from_env({
        **valid_env,
        "NTFY_URL": "https://ntfy.example.org",
        "NTFY_TOPIC": "espresso",
        "NTFY_TOKEN": TEST_NTFY_TOKEN,
    })


@pytest.fixture
def archive(tmp_path):
    """A fresh archive with the migrations applied."""
    db = Database(tmp_path / "shots.db")
    db.migrate()
    yield db
    db.close()
