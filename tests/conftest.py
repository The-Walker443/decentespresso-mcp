from __future__ import annotations

import pytest

from visualizer_mcp.config import Config

# 48 Zeichen wie 'openssl rand -hex 24'.
TEST_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef"
TEST_PASSWORD = "hunter2-but-long-enough"


@pytest.fixture
def valid_env() -> dict[str, str]:
    return {
        "VISUALIZER_EMAIL": "shots@example.org",
        "VISUALIZER_PASSWORD": TEST_PASSWORD,
        "MCP_PATH_SECRET": TEST_SECRET,
        "PUBLIC_BASE_URL": "https://coffee-mcp.example.com",
        # SPEC ss20: ab M8 Pflicht, und nur private Adressen.
        "DECAID_URL": "http://10.100.100.171:8080",
    }


@pytest.fixture
def config(valid_env: dict[str, str]) -> Config:
    return Config.from_env(valid_env)
