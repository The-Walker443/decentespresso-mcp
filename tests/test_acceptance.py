"""Abnahmekriterien aus SPEC ss13, jeweils mit ihrer Nummer.

Vier der sechs Kriterien laufen hier automatisch. Kriterium 2 (neuer Bezug
erscheint rechtzeitig) und 5 (Container uebersteht Neustart) brauchen die echte
Maschine bzw. Docker; fuer sie steht eine Checkliste im README unter
"Abnahme auf dem Host". Die Tests hier decken das ab, was ohne beides pruefbar
ist - und benennen im Fehlerfall, welches Kriterium gerissen wurde.
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

from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.logging_setup import REDACTED, setup_logging
from visualizer_mcp.metrics import warm_metrics_cache
from visualizer_mcp.server import build_mcp
from visualizer_mcp.sync import run_sync

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


# --- Kriterium 1: Backfill laedt alle Shots inkl. Profilversionen fehlerfrei --


async def test_criterion_1_backfill_is_complete_and_error_free(db: Database) -> None:
    details = corpus()[:2]
    client = FakeDecaid(details)

    result = await run_sync(client, db, full=True)

    assert result.errors == [], "Kriterium 1: Backfill lief nicht fehlerfrei"
    assert result.waiting_for_tablet is False
    assert result.new_shots == len(details)
    assert db.count_shots() == len(details)
    assert db.count_series_points() == len(details) * POINTS_PER_SHOT
    assert db.count_profiles() >= 1, "Kriterium 1: keine Profilversion angelegt"
    assert db.shot_ids_without_profile() == []


# --- Kriterium 3: get_shot("latest") liefert alles in einer Antwort <= 15 kB --


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

    # Vier Bestandteile in einer Antwort. Der Verlauf kommt seit SPEC ss17 als
    # curve_shape; die Punktarrays sind die Ausnahme.
    assert lean["shot"]["id"] == REFERENCE
    assert lean["metrics"]["pi_end"] is not None
    assert lean["curve_shape"]["segments"]
    assert lean["profile"]["title"] == "D-Flow"

    for label, payload in (("ohne Punktarrays", lean), ("mit Punktarrays", full)):
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert size <= BUDGET_BYTES, (
            f"Kriterium 3 ({label}): Antwort ist {size} B, erlaubt sind 15000"
        )
    assert full["curve"]["t"]


# --- Kriterium 4: Profilaenderung erzeugt neue Version, alter Shot bleibt -----


async def test_criterion_4_profile_change_keeps_history(db: Database) -> None:
    old_shot = corpus()[0]

    await run_sync(FakeDecaid([old_shot]), db, full=True)
    old_profile_id = db.get_shot_row(old_shot["id"])["profile_id"]
    assert old_profile_id is not None

    # Profil an der Maschine geaendert, danach ein weiterer Bezug.
    new_shot = decaid_detail("de1app-1785599123", timestamp="2026-08-02T05:32:50",
                             updated_at="2026-09-01T11:00:00Z")
    new_shot["workflow"]["profile"]["steps"][0]["temperature"] = 92.0
    await run_sync(FakeDecaid([old_shot, new_shot]), db, full=True)

    assert db.count_profiles() == 2, "Kriterium 4: keine neue Profilversion"
    assert db.get_shot_row(old_shot["id"])["profile_id"] == old_profile_id, (
        "Kriterium 4: alter Bezug wurde auf die neue Version umgehaengt"
    )
    assert db.get_shot_row(new_shot["id"])["profile_id"] != old_profile_id


# --- Kriterium 6: kein Secret in Logs oder Tool-Ausgaben ----------------------


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
        assert secret not in blob, "Kriterium 6: Secret in einer Tool-Antwort"


def test_criterion_6_no_secret_in_logs(capsys, config: Config) -> None:
    setup_logging(config.log_level, secrets=config.secret_values())
    log = logging.getLogger("visualizer_mcp.test")

    # Alle Formen, in denen ein Geheimnis realistisch in eine Logzeile geraet.
    log.info("connector %s", config.connector_url)
    log.info("password %s", TEST_PASSWORD)
    log.info("header Authorization: Basic %s", config.basic_auth_token)
    log.info("dict %s", {"authorization": f"Basic {config.basic_auth_token}"})
    try:
        raise RuntimeError(f"401 fuer {config.basic_auth_token}")
    except RuntimeError:
        log.exception("auth fehlgeschlagen")

    out = capsys.readouterr().out
    logging.getLogger().handlers.clear()

    for secret in (TEST_SECRET, TEST_PASSWORD, config.basic_auth_token):
        assert secret not in out, "Kriterium 6: Secret im Log"
    assert REDACTED in out


def test_basic_auth_token_is_the_wire_format(config: Config) -> None:
    """Der Klartext allein reicht nicht - so geht das Passwort tatsaechlich raus."""
    import base64

    decoded = base64.b64decode(config.basic_auth_token).decode()
    assert decoded == f"{config.visualizer_email}:{TEST_PASSWORD}"
    assert TEST_PASSWORD not in config.basic_auth_token, "sonst waere die Stufe unnoetig"


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
    # Faengt auch ab, was die Konfiguration gar nicht kennt.
    setup_logging(config.log_level, secrets=config.secret_values())
    logging.getLogger("visualizer_mcp.test").info("%s", line)
    out = capsys.readouterr().out
    logging.getLogger().handlers.clear()

    assert REDACTED in out
    for leaked in ("Zm9vOmJhcg==", "eyJhbGciOi", "username=admin"):
        assert leaked not in out
