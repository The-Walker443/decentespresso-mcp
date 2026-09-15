"""Response economy per SPEC §17.

Records what M6 achieved: the size of the tool definitions, the response sizes
of the typical calls, and that every call is measurable. The bounds are
deliberately tight - they should fire when something grows back, not only when
it gets out of hand.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Iterator

import pytest
from fastmcp import Client
from helpers import RECENT_ID, REFERENCE_ID, corpus, store_shot_with_profile

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_profile import profile_version
from decentespresso_mcp.metrics import warm_metrics_cache
from decentespresso_mcp.server import build_mcp

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
REFERENCE = REFERENCE_ID
RECENT = RECENT_ID

# --- Schranken (SPEC ss17.4) -------------------------------------------------
#
# Every bound sits just above the measured value - it should fire when
# something grows back, not only on a doubling. Measured against the archive
# built from the fixtures.

#: All read-only tool definitions together, as they travel with every
#: request. Before M6: 10399 B across 9 tools. After M6: 6876 B. M8 adds
#: audit_archive and get_workflow: 9241 B across 11 tools (English texts came
#: out marginally leaner than the German ones - 82 B less).
#:
#: The telling figure is the one **per tool**, not the sum - more capability
#: necessarily costs more, verbosity does not. Per tool: 1155 B before M6,
#: 764 B after M6, 840 B now. The growth sits in the JSON schema of the new
#: parameters, not in the descriptions.
MAX_TOOL_DEFINITIONS = 9_800
MAX_BYTES_PER_TOOL = 900

#: get_shot("latest") without point arrays.
#: Before M6: 5021 B (arrays were the default). Now: 2092 B.
MAX_GET_SHOT_LEAN = 2_300

#: With WRITE_ENABLED four write tools join in (update_shot, update_bean,
#: update_batch, set_workflow). Measured: 12123 B across 15 tools - 808 B per
#: tool and therefore leaner than the 845 B M7 needed with a single write
#: tool. Since M8 the behavioural rules live centrally in INSTRUCTIONS rather
#: than in every docstring. The surcharge only applies when writing is on.
MAX_TOOL_DEFINITIONS_WITH_WRITE = 12_800

#: compare_shots with two shots including profiles. Now: 4234 B.
#: Before M6 the same information took three calls: compare_shots (1680 B)
#: plus get_profile twice (1013 B each) = 3706 B across three round trips.
MAX_COMPARE_TWO = 4_500


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _profile_of(detail: dict) -> dict:
    return detail["workflow"]["profile"]


def _relink(db: Database, per_shot: dict[str, dict]) -> None:
    """Attaches a profile version of its own to each shot."""
    for shot_id, profile in per_shot.items():
        pid, _ = db.upsert_profile(
            seen_at="2026-09-14T12:00:00Z", source="decaid",
            **profile_version(profile),
        )
        db.link_shot_profile(shot_id, pid)


def size_of(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))





@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "economy.db")
    database.migrate()
    for detail in corpus()[:2]:
        store_shot_with_profile(database, detail)
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
    """The definitions travel with *every* request - duplicated semantics cost.

    The full glossary lives in the server instructions; the
    Docstrings verweisen nur darauf.
    """
    async with Client(mcp) as client:
        tools = await client.list_tools()

    definitions = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    total = size_of(definitions)
    assert total <= MAX_TOOL_DEFINITIONS, (
        f"Tool-Definitionen sind auf {total} B gewachsen, erlaubt sind "
        f"{MAX_TOOL_DEFINITIONS}. Does the new text belong in INSTRUCTIONS?"
    )


async def test_no_tool_repeats_the_glossary(mcp) -> None:
    """The detailed explanations appear exactly once - in the instructions."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Terms that used to be spelled out across several docstrings.
    for phrase in ("60 % of its maximum", "building the puck", "not 0"):
        carriers = [t.name for t in tools if phrase in (t.description or "")]
        assert not carriers, f"{phrase!r} steht wieder in {carriers}"


# --------------------------------------------------- (1)/(2) Antwortgroessen


async def test_get_shot_lean_is_small(mcp) -> None:
    payload = await call(mcp, "get_shot", {"id": "latest"})
    assert "curve" not in payload
    assert payload["curve_shape"]["segments"]

    actual = size_of(payload)
    assert actual <= MAX_GET_SHOT_LEAN, (
        f"get_shot('latest') is {actual} B, allowed is {MAX_GET_SHOT_LEAN}"
    )


async def test_compare_two_with_profiles_is_small(mcp) -> None:
    payload = await call(mcp, "compare_shots", {"ids": [REFERENCE, RECENT]})
    # Complete in one call: metrics, shape and profiles.
    assert all("profile" in shot for shot in payload["shots"])
    assert all("curve_shape" in shot for shot in payload["shots"])

    actual = size_of(payload)
    assert actual <= MAX_COMPARE_TWO, (
        f"compare_shots(2, with profiles) is {actual} B, allowed is {MAX_COMPARE_TWO}"
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
    """Two versions differing only in their notes."""
    base = _profile_of(corpus()[0])
    cosmetic = dict(base)
    cosmetic["notes"] = base.get("notes", "") + " (Tippfehler korrigiert)"
    _relink(db, {REFERENCE: base, RECENT: cosmetic})

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    notice = payload["profile_notice"]
    assert "cosmetic" in notice
    assert "Careful" not in notice


async def test_different_targets_are_flagged_loudly(db: Database, config: Config) -> None:
    """Different targets - the difference then comes from the profile."""
    base = _profile_of(corpus()[0])
    louder = dict(base)
    louder["target_weight"] = float(base.get("target_weight") or 36) + 12
    _relink(db, {REFERENCE: base, RECENT: louder})

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    notice = payload["profile_notice"]
    assert "Careful" in notice
    assert "from the profile" in notice


async def test_missing_profile_is_reported(db: Database, config: Config) -> None:
    db._conn.execute("UPDATE shots SET profile_id = NULL WHERE id = ?", (RECENT,))
    db._conn.commit()

    payload = await call(build_mcp(config, db), "compare_shots", {"ids": [REFERENCE, RECENT]})
    assert "incomplete" in payload["profile_notice"]


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
    caplog.set_level(logging.INFO, logger="decentespresso_mcp.telemetry")
    await call(mcp, "get_shot_metrics", {"id": REFERENCE})

    records = [r for r in caplog.records if r.getMessage() == "tool call"]
    assert len(records) == 1
    fields = records[0].fields
    assert fields["tool"] == "get_shot_metrics"
    assert fields["bytes"] > 0
    assert fields["dur_ms"] >= 0


async def test_failed_calls_are_logged_without_leaking(mcp, caplog) -> None:
    from fastmcp.exceptions import ToolError

    caplog.set_level(logging.INFO, logger="decentespresso_mcp.telemetry")
    with pytest.raises(ToolError):
        await call(mcp, "get_shot", {"id": "does-not-exist"})

    records = [r for r in caplog.records if r.getMessage() == "tool call failed"]
    assert len(records) == 1
    assert records[0].fields["tool"] == "get_shot"
    assert "error" in records[0].fields


async def test_log_never_carries_arguments(mcp, caplog) -> None:
    """Arguments are the likeliest route by which something reaches the log."""
    caplog.set_level(logging.INFO, logger="decentespresso_mcp.telemetry")
    await call(mcp, "list_shots", {"bean": "Tchibo", "limit": 3})

    blob = "\n".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert "Tchibo" not in blob
    assert "bean" not in blob
    for record in caplog.records:
        assert set(getattr(record, "fields", {})) <= {"tool", "dur_ms", "bytes", "error"}


async def test_write_tool_costs_what_it_is_worth(valid_env, db) -> None:
    """Write mode must not blow up the tool list (SPEC §17.3/§18.4)."""
    from decentespresso_mcp.decaid_client import DecaidClient
    from decentespresso_mcp.sync import SyncCoordinator

    writable = Config.from_env({**valid_env, "WRITE_ENABLED": "true"})
    coordinator = SyncCoordinator(
        DecaidClient("http://10.100.100.171:8080"), db
    )
    try:
        async with Client(build_mcp(writable, db, coordinator)) as client:
            tools = await client.list_tools()
    finally:
        await coordinator.aclose()

    assert "update_shot" in {t.name for t in tools}
    total = size_of([t.model_dump(mode="json", exclude_none=True) for t in tools])
    assert total <= MAX_TOOL_DEFINITIONS_WITH_WRITE, (
        f"tool definitions with write mode: {total} B, allowed "
        f"{MAX_TOOL_DEFINITIONS_WITH_WRITE}"
    )
