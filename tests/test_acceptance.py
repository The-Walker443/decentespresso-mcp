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
from fastmcp import Client

from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.logging_setup import REDACTED, setup_logging
from visualizer_mcp.metrics import warm_metrics_cache
from visualizer_mcp.server import build_mcp
from visualizer_mcp.sync import run_sync
from visualizer_mcp.tcl_profile import parse_profile, semantic_hash, version_hash
from visualizer_mcp.visualizer_client import series_rows_from_detail, shot_row_from_detail

from .conftest import TEST_PASSWORD, TEST_SECRET
from .test_sync import ADVANCED_TCL, FakeClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
REFERENCE = "6eb25d36-ff0c-48d0-8f88-0b05f9c7418a"
BUDGET_BYTES = 15_000


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
    details = [load("shot_reference.json"), load("shot_recent.json")]
    client = FakeClient(details)

    result = await run_sync(client, db, full=True)

    assert result.errors == [], "Kriterium 1: Backfill lief nicht fehlerfrei"
    assert result.new_shots == len(details)
    assert db.count_shots() == len(details)
    assert db.count_series_points() == sum(len(d["timeframe"]) for d in details)
    assert db.count_profiles() >= 1, "Kriterium 1: keine Profilversion angelegt"
    assert db.shot_ids_without_profile() == []


# --- Kriterium 3: get_shot("latest") liefert alles in einer Antwort <= 15 kB --


async def test_criterion_3_latest_is_complete_and_within_budget(
    config: Config, db: Database
) -> None:
    payload = load("shot_reference.json")
    db.upsert_shot(shot_row_from_detail(payload, "2026-08-01T10:00:00Z"),
                   series_rows_from_detail(payload))
    parsed = parse_profile(ADVANCED_TCL)
    profile_id, _ = db.upsert_profile(
        name=parsed["title"], version_hash=version_hash(ADVANCED_TCL),
        semantic_hash=semantic_hash(parsed), raw_tcl=ADVANCED_TCL,
        parsed_json=json.dumps(parsed), profile_notes=None,
        seen_at="2026-08-01T10:00:00Z",
    )
    db.link_shot_profile(payload["id"], profile_id)
    warm_metrics_cache(db)

    async with Client(build_mcp(config, db)) as client:
        result = (await client.call_tool("get_shot", {"id": "latest"})).data

    # Vier Bestandteile in einer Antwort.
    assert result["shot"]["id"] == REFERENCE
    assert result["metrics"]["pi_end"] is not None
    assert result["curve"]["t"]
    assert result["profile"]["title"] == "D-Flow / default"

    size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    assert size <= BUDGET_BYTES, f"Kriterium 3: Antwort ist {size} B, erlaubt sind 15000"


# --- Kriterium 4: Profilaenderung erzeugt neue Version, alter Shot bleibt -----


async def test_criterion_4_profile_change_keeps_history(db: Database) -> None:
    old_shot = load("shot_reference.json")
    new_shot = load("shot_recent.json")

    await run_sync(FakeClient([old_shot]), db, full=True)
    old_profile_id = db.get_shot_row(old_shot["id"])["profile_id"]
    assert old_profile_id is not None

    # Profil an der Maschine geaendert, danach ein weiterer Bezug.
    changed = ADVANCED_TCL.replace("espresso_temperature 88", "espresso_temperature 92")
    await run_sync(
        FakeClient([old_shot, new_shot],
                   profiles={old_shot["id"]: ADVANCED_TCL, new_shot["id"]: changed}),
        db, full=True,
    )

    assert db.count_profiles() == 2, "Kriterium 4: keine neue Profilversion"
    assert db.get_shot_row(old_shot["id"])["profile_id"] == old_profile_id, (
        "Kriterium 4: alter Shot wurde auf die neue Version umgehaengt"
    )
    assert db.get_shot_row(new_shot["id"])["profile_id"] != old_profile_id


# --- Kriterium 6: kein Secret in Logs oder Tool-Ausgaben ----------------------


async def test_criterion_6_no_secret_in_tool_output(config: Config, db: Database) -> None:
    payload = load("shot_reference.json")
    db.upsert_shot(shot_row_from_detail(payload, "2026-08-01T10:00:00Z"),
                   series_rows_from_detail(payload))
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
