"""Acceptance criteria from SPEC §13, each with its number.

Four of the six criteria run automatically here. Criterion 2 (a new shot
appears in time) and 5 (the container survives a restart) need the real
machine or Docker; a checklist for those sits in the README under
"Acceptance on the host". The tests here cover what is checkable without
either - and on failure they name which criterion was broken.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Iterator

import pytest
from conftest import TEST_PASSWORD, TEST_SECRET
from fastmcp import Client
from helpers import REFERENCE_ID, corpus, decaid_detail, store_shot_with_profile
from test_sync import FakeDecaid

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.logging_setup import REDACTED, setup_logging
from decentespresso_mcp.metrics import warm_metrics_cache
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.sync import run_sync

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
REFERENCE = REFERENCE_ID
BUDGET_BYTES = 15_000
POINTS_PER_SHOT = 184


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "acceptance.db")
    database.migrate()
    yield database
    database.close()


# --- Criterion 1: backfill loads every shot incl. profile versions cleanly ---


async def test_criterion_1_backfill_is_complete_and_error_free(db: Database) -> None:
    details = corpus()[:2]
    client = FakeDecaid(details)

    result = await run_sync(client, db, full=True)

    assert result.errors == [], "criterion 1: the backfill did not run cleanly"
    assert result.waiting_for_tablet is False
    assert result.new_shots == len(details)
    assert db.count_shots() == len(details)
    assert db.count_series_points() == len(details) * POINTS_PER_SHOT
    assert db.count_profiles() >= 1, "criterion 1: no profile version created"
    assert db.shot_ids_without_profile() == []


# --- Criterion 3: get_shot("latest") returns everything in one <= 15 kB ------


async def test_criterion_3_latest_is_complete_and_within_budget(
    config: Config, db: Database
) -> None:
    store_shot_with_profile(db, corpus()[0])
    warm_metrics_cache(db)

    async with Client(build_mcp(config, db)) as client:
        lean = (await client.call_tool("get_shot", {"id": "latest"})).data
        full = (await client.call_tool(
            "get_shot", {"id": "latest", "include_curve": True}
        )).data

    # Four parts in one response. Since SPEC §17 the curve comes as
    # curve_shape; the point arrays are the exception.
    assert lean["shot"]["id"] == REFERENCE
    assert lean["metrics"]["pi_end"] is not None
    assert lean["curve_shape"]["segments"]
    assert lean["profile"]["title"] == "D-Flow"

    for label, payload in (("without point arrays", lean), ("with point arrays", full)):
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert size <= BUDGET_BYTES, (
            f"criterion 3 ({label}): the response is {size} B, 15000 allowed"
        )
    assert full["curve"]["t"]


# --- Criterion 4: a profile change creates a new version, the old shot stays -


async def test_criterion_4_profile_change_keeps_history(db: Database) -> None:
    old_shot = corpus()[0]

    await run_sync(FakeDecaid([old_shot]), db, full=True)
    old_profile_id = db.get_shot_row(old_shot["id"])["profile_id"]
    assert old_profile_id is not None

    # Profile changed at the machine, then another shot.
    new_shot = decaid_detail("de1app-1785599123", timestamp="2026-08-02T05:32:50",
                             updated_at="2026-09-01T11:00:00Z")
    new_shot["workflow"]["profile"]["steps"][0]["temperature"] = 92.0
    await run_sync(FakeDecaid([old_shot, new_shot]), db, full=True)

    assert db.count_profiles() == 2, "criterion 4: no new profile version"
    assert db.get_shot_row(old_shot["id"])["profile_id"] == old_profile_id, (
        "criterion 4: the old shot was moved onto the new version"
    )
    assert db.get_shot_row(new_shot["id"])["profile_id"] != old_profile_id


# --- Criterion 6: no secret in logs or tool output ---------------------------


async def test_criterion_6_no_secret_in_tool_output(config: Config, db: Database) -> None:
    store_shot_with_profile(db, corpus()[0])
    warm_metrics_cache(db)

    collected: list[str] = []
    async with Client(build_mcp(config, db)) as client:
        collected.append(json.dumps(
            [t.model_dump(mode="json") for t in await client.list_tools()]
        ))
        for name, args in [
            ("status", {}), ("list_beans", {}), ("list_shots", {}),
            ("get_shot", {"id": REFERENCE}), ("get_shot_metrics", {"id": REFERENCE}),
            ("list_profiles", {}),
        ]:
            collected.append(json.dumps((await client.call_tool(name, args)).data,
                                        default=str))

    blob = "\n".join(collected)
    for secret in (TEST_SECRET, TEST_PASSWORD, config.basic_auth_token):
        assert secret not in blob, "criterion 6: a secret in a tool response"


def test_criterion_6_no_secret_in_logs(capsys, config: Config) -> None:
    setup_logging(config.log_level, secrets=config.secret_values())
    log = logging.getLogger("decentespresso_mcp.test")

    # Every shape in which a secret realistically ends up in a log line.
    log.info("connector %s", config.connector_url)
    log.info("password %s", TEST_PASSWORD)
    log.info("header Authorization: Basic %s", config.basic_auth_token)
    log.info("dict %s", {"authorization": f"Basic {config.basic_auth_token}"})
    try:
        raise RuntimeError(f"401 for {config.basic_auth_token}")
    except RuntimeError:
        log.exception("auth failed")

    out = capsys.readouterr().out
    logging.getLogger().handlers.clear()

    for secret in (TEST_SECRET, TEST_PASSWORD, config.basic_auth_token):
        assert secret not in out, "criterion 6: a secret in the log"
    assert REDACTED in out


def test_basic_auth_token_is_the_wire_format(config: Config) -> None:
    """The plaintext alone is not enough - this is how the password really goes out."""
    import base64

    decoded = base64.b64decode(config.basic_auth_token).decode()
    assert decoded == f"{config.visualizer_email}:{TEST_PASSWORD}"
    assert TEST_PASSWORD not in config.basic_auth_token, "otherwise the step would be pointless"


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Basic Zm9vOmJhcg==",
        "authorization='Bearer eyJhbGciOi'",
        'headers={"authorization": "Basic Zm9vOmJhcg=="}',
        "AUTHORIZATION: Digest username=admin",
    ],
)
def test_unknown_auth_headers_are_masked_too(capsys, config: Config, line: str) -> None:
    # Also catches what the configuration knows nothing about.
    setup_logging(config.log_level, secrets=config.secret_values())
    logging.getLogger("decentespresso_mcp.test").info("%s", line)
    out = capsys.readouterr().out
    logging.getLogger().handlers.clear()

    assert REDACTED in out
    for leaked in ("Zm9vOmJhcg==", "eyJhbGciOi", "username=admin"):
        assert leaked not in out
