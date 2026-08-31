"""Antwortoekonomie nach SPEC ss17.

Haelt fest, was M6 erreicht hat: die Groesse der Tool-Definitionen, die
Antwortgroessen der typischen Aufrufe und dass jeder Aufruf messbar ist. Die
Schranken sind bewusst eng - sie sollen anschlagen, wenn etwas zurueckwaechst,
nicht erst wenn es aus dem Ruder laeuft.
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
from visualizer_mcp.metrics import warm_metrics_cache
from visualizer_mcp.server import build_mcp
from visualizer_mcp.tcl_profile import parse_profile, semantic_hash, version_hash
from visualizer_mcp.visualizer_client import series_rows_from_detail, shot_row_from_detail

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
REFERENCE = "6eb25d36-ff0c-48d0-8f88-0b05f9c7418a"
RECENT = "36ed21cd-8fc0-489e-8ea4-959d952e04c0"

# --- Schranken (SPEC ss17.4) -------------------------------------------------
#
# Jede Schranke liegt knapp ueber dem gemessenen Wert - sie soll anschlagen,
# wenn etwas zurueckwaechst, nicht erst bei einer Verdopplung. Gemessen am
# Archiv aus den Fixtures.

#: Alle neun Tool-Definitionen zusammen, wie sie in jeder Anfrage mitgehen.
#: Vor M6: 10399 B. Jetzt: 6876 B. Davon sind rund 3600 B JSON-Schema der
#: Parameter - das ist der Boden, tiefer geht es nur ueber weniger Parameter.
MAX_TOOL_DEFINITIONS = 7_200

#: get_shot("latest") ohne Punktarrays.
#: Vor M6: 5021 B (Arrays waren Default). Jetzt: 2092 B.
MAX_GET_SHOT_LEAN = 2_300

#: Mit WRITE_ENABLED kommt update_shot dazu (1568 B; der Docstring traegt die
#: Verhaltensregeln fuer das Modell und die Feldliste, SPEC ss18.4). Gemessen:
#: 8446 B. Der Aufschlag faellt nur an, wenn Schreiben eingeschaltet ist.
MAX_TOOL_DEFINITIONS_WITH_WRITE = 8_800

#: compare_shots mit zwei Shots inklusive Profilen. Jetzt: 4234 B.
#: Vor M6 brauchte derselbe Informationsstand drei Aufrufe: compare_shots
#: (1680 B) plus zweimal get_profile (je 1013 B) = 3706 B in drei Runden.
MAX_COMPARE_TWO = 4_500


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def size_of(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _add(db: Database, fixture: str, tcl_name: str | None) -> None:
    payload = load(fixture)
    db.upsert_shot(
        shot_row_from_detail(payload, "2026-08-01T10:00:00Z"),
        series_rows_from_detail(payload),
    )
    if tcl_name:
        raw = (FIXTURES / tcl_name).read_text(encoding="utf-8")
        parsed = parse_profile(raw)
        profile_id, _ = db.upsert_profile(
            name=parsed["title"] or "(ohne Titel)", version_hash=version_hash(raw),
            semantic_hash=semantic_hash(parsed), raw_tcl=raw,
            parsed_json=json.dumps(parsed, ensure_ascii=False),
            profile_notes=parsed.get("notes"), seen_at="2026-08-01T10:00:00Z",
        )
        db.link_shot_profile(payload["id"], profile_id)


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "economy.db")
    database.migrate()
    _add(database, "shot_reference.json", "profile_reference.tcl")
    _add(database, "shot_recent.json", "profile_recent.tcl")
    warm_metrics_cache(database)
    database.set_state("last_sync_at", "2026-08-01T10:00:00Z")
    database.set_state("backfill_completed_at", "2026-08-01T10:00:00Z")
    yield database
    database.close()


@pytest.fixture
def mcp(config: Config, db: Database):
    return build_mcp(config, db)


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


# ------------------------------------------------- (3) Tool-Definitionen


async def test_tool_definitions_stay_small(mcp) -> None:
    """Die Definitionen gehen bei *jeder* Anfrage mit - doppelte Semantik kostet.

    Die vollstaendige Begriffserklaerung steht in den Server-Anweisungen; die
    Docstrings verweisen nur darauf.
    """
    async with Client(mcp) as client:
        tools = await client.list_tools()

    definitions = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    total = size_of(definitions)
    assert total <= MAX_TOOL_DEFINITIONS, (
        f"Tool-Definitionen sind auf {total} B gewachsen, erlaubt sind "
        f"{MAX_TOOL_DEFINITIONS}. Gehoert der neue Text in die INSTRUCTIONS?"
    )


async def test_no_tool_repeats_the_glossary(mcp) -> None:
    """Die ausfuehrlichen Warntexte stehen genau einmal - in den Anweisungen."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Begriffe, die frueher in mehreren Docstrings ausbuchstabiert waren.
    for phrase in ("60 % des Druckmaximums", "Puckaufbau", "kein Nullwert"):
        carriers = [t.name for t in tools if phrase in (t.description or "")]
        assert not carriers, f"{phrase!r} steht wieder in {carriers}"


# --------------------------------------------------- (1)/(2) Antwortgroessen


async def test_get_shot_lean_is_small(mcp) -> None:
    payload = await call(mcp, "get_shot", {"id": "latest"})
    assert "curve" not in payload
    assert payload["curve_shape"]["segments"]

    actual = size_of(payload)
    assert actual <= MAX_GET_SHOT_LEAN, (
        f"get_shot('latest') ist {actual} B, erlaubt sind {MAX_GET_SHOT_LEAN}"
    )


async def test_compare_two_with_profiles_is_small(mcp) -> None:
    payload = await call(mcp, "compare_shots", {"ids": [REFERENCE, RECENT]})
    # Vollstaendig in einem Aufruf: Metriken, Form und Profile.
    assert all("profile" in shot for shot in payload["shots"])
    assert all("curve_shape" in shot for shot in payload["shots"])

    actual = size_of(payload)
    assert actual <= MAX_COMPARE_TWO, (
        f"compare_shots(2, mit Profilen) ist {actual} B, erlaubt sind {MAX_COMPARE_TWO}"
    )


async def test_lean_answers_beat_the_old_defaults(mcp) -> None:
    """Der Kern von M6: dieselbe Frage, weniger Kontext."""
    lean = await call(mcp, "get_shot", {"id": REFERENCE})
    with_arrays = await call(mcp, "get_shot", {"id": REFERENCE, "include_curve": True})
    assert size_of(lean) * 2 < size_of(with_arrays)


# --------------------------------------------------------- (2) Profilhinweis


async def test_same_profile_needs_no_notice(db: Database, config: Config) -> None:
    # Beide Shots auf dieselbe Version haengen.
    profile_id = db.get_shot_row(REFERENCE)["profile_id"]
    db.link_shot_profile(RECENT, profile_id)

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    assert "profile_notice" not in payload


async def test_cosmetic_difference_is_named_as_such(db: Database, config: Config) -> None:
    base = (FIXTURES / "profile_default_a.tcl").read_text(encoding="utf-8")
    cosmetic = (FIXTURES / "profile_default_a_cosmetic.tcl").read_text(encoding="utf-8")
    for shot_id, raw in ((REFERENCE, base), (RECENT, cosmetic)):
        parsed = parse_profile(raw)
        pid, _ = db.upsert_profile(
            name=parsed["title"], version_hash=version_hash(raw),
            semantic_hash=semantic_hash(parsed), raw_tcl=raw,
            parsed_json=json.dumps(parsed), profile_notes=None,
            seen_at="2026-08-01T10:00:00Z",
        )
        db.link_shot_profile(shot_id, pid)

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    notice = payload["profile_notice"]
    assert "kosmetisch" in notice
    assert "Achtung" not in notice


async def test_different_targets_are_flagged_loudly(db: Database, config: Config) -> None:
    base = (FIXTURES / "profile_default_a.tcl").read_text(encoding="utf-8")
    louder = (FIXTURES / "profile_default_a_pressure_only.tcl").read_text(encoding="utf-8")
    for shot_id, raw in ((REFERENCE, base), (RECENT, louder)):
        parsed = parse_profile(raw)
        pid, _ = db.upsert_profile(
            name=parsed["title"], version_hash=version_hash(raw),
            semantic_hash=semantic_hash(parsed), raw_tcl=raw,
            parsed_json=json.dumps(parsed), profile_notes=None,
            seen_at="2026-08-01T10:00:00Z",
        )
        db.link_shot_profile(shot_id, pid)

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    notice = payload["profile_notice"]
    assert "Achtung" in notice
    assert "vom Profil kommen" in notice


async def test_missing_profile_is_reported(db: Database, config: Config) -> None:
    db._conn.execute("UPDATE shots SET profile_id = NULL WHERE id = ?", (RECENT,))
    db._conn.commit()

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    assert "unvollstaendig" in payload["profile_notice"]


async def test_profiles_can_be_switched_off(mcp) -> None:
    with_profiles = await call(mcp, "compare_shots", {"ids": [REFERENCE, RECENT]})
    without = await call(
        mcp, "compare_shots", {"ids": [REFERENCE, RECENT], "include_profile": False}
    )
    assert all("profile" not in s for s in without["shots"])
    assert "profile_notice" not in without
    assert size_of(without) < size_of(with_profiles)


# ------------------------------------------------------------ (4) Messbarkeit


async def test_every_call_is_logged_with_size_and_duration(mcp, caplog) -> None:
    caplog.set_level(logging.INFO, logger="visualizer_mcp.telemetry")
    await call(mcp, "get_shot_metrics", {"id": REFERENCE})

    records = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert len(records) == 1
    fields = records[0].fields
    assert fields["tool"] == "get_shot_metrics"
    assert fields["bytes"] > 0
    assert fields["dur_ms"] >= 0


async def test_failed_calls_are_logged_without_leaking(mcp, caplog) -> None:
    from fastmcp.exceptions import ToolError

    caplog.set_level(logging.INFO, logger="visualizer_mcp.telemetry")
    with pytest.raises(ToolError):
        await call(mcp, "get_shot", {"id": "gibt-es-nicht"})

    records = [r for r in caplog.records if r.getMessage() == "tool call failed"]
    assert len(records) == 1
    assert records[0].fields["tool"] == "get_shot"
    assert "error" in records[0].fields


async def test_log_never_carries_arguments(mcp, caplog) -> None:
    """Argumente sind der wahrscheinlichste Weg, auf dem etwas ins Log geraet."""
    caplog.set_level(logging.INFO, logger="visualizer_mcp.telemetry")
    await call(mcp, "list_shots", {"bean": "Tchibo", "limit": 3})

    blob = "\n".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert "Tchibo" not in blob
    assert "bean" not in blob
    for record in caplog.records:
        assert set(getattr(record, "fields", {})) <= {"tool", "dur_ms", "bytes", "error"}


async def test_write_tool_costs_what_it_is_worth(valid_env, db) -> None:
    """Der Schreibmodus darf die Tool-Liste nicht sprengen (SPEC ss17.3/ss18.4)."""
    from visualizer_mcp.sync import SyncCoordinator
    from visualizer_mcp.visualizer_client import VisualizerClient

    writable = Config.from_env({**valid_env, "WRITE_ENABLED": "true"})
    coordinator = SyncCoordinator(
        VisualizerClient("a@b.org", "lang-genug-hier"), db
    )
    try:
        async with Client(build_mcp(writable, db, coordinator)) as client:
            tools = await client.list_tools()
    finally:
        await coordinator.aclose()

    assert "update_shot" in {t.name for t in tools}
    total = size_of([t.model_dump(mode="json", exclude_none=True) for t in tools])
    assert total <= MAX_TOOL_DEFINITIONS_WITH_WRITE, (
        f"Tool-Definitionen mit Schreibmodus: {total} B, erlaubt "
        f"{MAX_TOOL_DEFINITIONS_WITH_WRITE}"
    )
