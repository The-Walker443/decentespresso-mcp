"""Response economy per SPEC §9.1.

Records the size of the tool definitions, the response sizes of the typical
calls, and that every call is measurable. The bounds are
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
from fastmcp.exceptions import ToolError
from helpers import RECENT_ID, REFERENCE_ID, corpus, store_shot_with_profile

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_profile import profile_version
from decentespresso_mcp.metrics import warm_metrics_cache
from decentespresso_mcp.server import build_mcp

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
REFERENCE = REFERENCE_ID
RECENT = RECENT_ID

# --- Bounds (SPEC §9.1) ------------------------------------------------------
#
# Every bound sits just above the measured value - it should fire when
# something grows back, not only on a doubling. Measured against the archive
# built from the fixtures.

#: All read-only tool definitions together, as they travel with every
#: request. Measured over time: 10399 B across 9 tools, then 6876 B after the
#: economy pass, now 9241 B across 11 tools.
#:
#: The telling figure is the one **per tool**, not the sum - more capability
#: necessarily costs more, verbosity does not. Per tool: 1155 B, then 764 B,
#: now 840 B. The growth sits in the JSON schema of the new
#: parameters, not in the descriptions.
MAX_TOOL_DEFINITIONS = 9_800
MAX_BYTES_PER_TOOL = 900

#: get_shot("latest") without point arrays.
#: Was 5021 B while the arrays were the default, then 2092 B. The puck
#: diagnostics add ~200 B at summary level: a band, a risk and a
#: temperature verdict, which is what triage needs.
MAX_GET_SHOT_LEAN = 2_400

#: With WRITE_ENABLED four write tools join in (update_shot, update_bean,
#: update_batch, set_workflow). Measured: 12123 B across 15 tools - 808 B per
#: tool and therefore leaner than the 845 B a single write tool once cost,
#: because the behavioural rules live centrally in INSTRUCTIONS rather than in
#: every docstring. The surcharge only applies when writing is on.
MAX_TOOL_DEFINITIONS_WITH_WRITE = 12_800

#: compare_shots with two shots including profiles. Now: 4234 B.
#: The same information once took three calls: compare_shots (1680 B) plus
#: get_profile twice (1013 B each) = 3706 B across three round trips.
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


#: SPEC §9.1 - the response budget these levels have to fit inside.
BUDGET_BYTES = 15_000


def shot_ids(db: Database) -> list[str]:
    return [r["id"] for r in db._conn.execute(
        "SELECT id FROM shots ORDER BY started_at DESC")]


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


# --------------------------------------------------- (3) Tool definitions


async def test_tool_definitions_stay_small(mcp) -> None:
    """The definitions travel with *every* request - duplicated semantics cost.

    The full glossary lives in the server instructions; the docstrings only
    point at it.
    """
    async with Client(mcp) as client:
        tools = await client.list_tools()

    definitions = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    total = size_of(definitions)
    assert total <= MAX_TOOL_DEFINITIONS, (
        f"the tool definitions have grown to {total} B, allowed is "
        f"{MAX_TOOL_DEFINITIONS}. Does the new text belong in INSTRUCTIONS?"
    )


async def test_no_tool_repeats_the_glossary(mcp) -> None:
    """The detailed explanations appear exactly once - in the instructions."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    # Terms that used to be spelled out across several docstrings.
    for phrase in ("60 % of its maximum", "building the puck", "not 0"):
        carriers = [t.name for t in tools if phrase in (t.description or "")]
        assert not carriers, f"{phrase!r} is back in {carriers}"


# ----------------------------------------------------- (1)/(2) Response sizes


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


async def test_the_shape_costs_a_fraction_of_the_arrays(mcp) -> None:
    """The heart of it: the same question, less context.

    Measured on the curve itself rather than on the whole response - the
    diagnostics that ride along at summary level would otherwise dilute the
    comparison and hide the thing being claimed.
    """
    lean = await call(mcp, "get_shot", {"id": REFERENCE})
    with_arrays = await call(mcp, "get_shot", {"id": REFERENCE, "include_curve": True})

    shape = size_of(lean["curve_shape"])
    arrays = size_of(with_arrays["curve"])
    assert arrays > shape * 3, (
        f"the arrays are {arrays} B against {shape} B of shape - the point of "
        "sending the shape by default is that the difference is large"
    )


# ------------------------------------------------------ (2) Profile notice


async def test_same_profile_needs_no_notice(db: Database, config: Config) -> None:
    # Hang both shots on the same version.
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
    """Write mode must not blow up the tool list (SPEC §9.1/§11)."""
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


# ------------------------------------------------------- Detail levels


#: Measured on the fixture archive. Each step roughly doubles, which is the
#: point: the cost of looking closer should be visible in the number.
MAX_BY_DETAIL = {
    "get_shot": {"summary": 2_400, "per_phase": 4_100, "detailed": 6_200},
    "get_shot_metrics": {"summary": 900, "per_phase": 2_600, "detailed": 2_600},
    "compare_shots": {"summary": 4_200, "per_phase": 5_700, "detailed": 9_200},
}


@pytest.mark.parametrize("detail", ["summary", "per_phase", "detailed"])
async def test_each_detail_level_stays_within_its_bound(
    config: Config, db: Database, detail: str
) -> None:
    """A level that quietly grows costs every conversation that uses it."""
    ids = shot_ids(db)
    async with Client(build_mcp(config, db)) as client:
        for tool, args in (
            ("get_shot", {"id": ids[0], "detail": detail}),
            ("get_shot_metrics", {"id": ids[0], "detail": detail}),
            ("compare_shots", {"ids": ids[:2], "detail": detail}),
        ):
            payload = (await client.call_tool(tool, args)).data
            actual = size_of(payload)
            allowed = MAX_BY_DETAIL[tool][detail]
            assert actual <= allowed, (
                f"{tool}(detail={detail}) is {actual} B, allowed is {allowed}"
            )


async def test_the_worst_case_comparison_stays_in_budget(
    config: Config, db: Database
) -> None:
    """Four shots, deepest detail, curves attached.

    This is the combination that blew the budget at 19 kB before the phase
    tables were dropped from comparisons.
    """
    ids = shot_ids(db)
    four = (ids * 2)[:4]
    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("compare_shots", {
            "ids": four, "detail": "detailed", "include_curves": True,
        })).data
    assert size_of(payload) <= BUDGET_BYTES, (
        f"compare_shots(4, detailed, curves) is {size_of(payload)} B"
    )


async def test_summary_carries_the_verdicts_not_the_workings(
    config: Config, db: Database
) -> None:
    """Triage needs a band and a risk, not five raw indicator values."""
    ids = shot_ids(db)
    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool(
            "get_shot", {"id": ids[0], "detail": "summary"})).data

    metrics = payload["metrics"]
    assert set(metrics["puck_resistance"]) == {"median", "band", "trend"}
    assert "indicators" not in metrics["channeling"]
    assert "profile_compliance" not in metrics


async def test_per_phase_carries_the_workings(config: Config, db: Database) -> None:
    ids = shot_ids(db)
    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool(
            "get_shot", {"id": ids[0], "detail": "per_phase"})).data

    metrics = payload["metrics"]
    assert metrics["channeling"]["indicators"], "the raw values belong here"
    assert metrics["profile_compliance"]["phases"], "so do the phases"


async def test_the_indicator_glossary_lives_in_the_instructions() -> None:
    """The same sentence five times per shot, four times over in a comparison.

    It is identical every time, so it belongs where it is in context once.
    """
    from decentespresso_mcp.metrics import CHANNELING_INDICATORS
    from decentespresso_mcp.server import INSTRUCTIONS

    for name in CHANNELING_INDICATORS:
        assert name in INSTRUCTIONS, f"{name} is not explained anywhere"


async def test_an_unknown_detail_level_is_refused(config: Config, db: Database) -> None:
    ids = shot_ids(db)
    async with Client(build_mcp(config, db)) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_shot", {"id": ids[0], "detail": "everything"})
    assert "invalid_argument" in str(excinfo.value)
    assert "per_phase" in str(excinfo.value), "the message names the levels"
