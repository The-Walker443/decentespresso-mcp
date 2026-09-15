"""Statistics over a period (SPEC §9.4).

Pure functions over rows, so the tests are too: rows in, figures out, with the
clock handed in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import decaid_detail, store_shot

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.server import build_mcp
from decentespresso_mcp.stats import (
    batch_usage,
    compare,
    is_real_shot,
    parse_period,
    summarise,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def shot(**fields):
    base = {
        "id": fields.pop("id", "s1"),
        "started_at": "2026-09-14T08:00:00Z",
        "bean_name": "Testsorte",
        "bean_batch_id": "b1",
        "profile_name": "D-Flow",
        "grinder_setting": "3.30",
        "dose_g": 18.0,
        "yield_g": 36.0,
        "ratio": 2.0,
        "duration_s": 28.0,
        "enjoyment": None,
    }
    base.update(fields)
    return base


# ------------------------------------------------------------ The period


@pytest.mark.parametrize(("text", "days"), [
    ("30d", 30), ("7d", 7), ("4w", 28), ("1m", 30), ("1y", 365),
])
def test_shorthand_periods(text: str, days: int) -> None:
    since, until = parse_period(text, now=NOW)
    assert until == NOW
    assert (until - since).days == days


def test_hours_are_hours_not_days() -> None:
    since, _ = parse_period("12h", now=NOW)
    assert (NOW - since) == timedelta(hours=12)


def test_an_iso_date_means_since_then() -> None:
    since, until = parse_period("2026-09-01", now=NOW)
    assert since.date().isoformat() == "2026-09-01"
    assert until == NOW


def test_nonsense_is_refused_with_both_forms_named() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_period("last tuesday", now=NOW)
    assert "ISO date" in str(excinfo.value)
    assert "30d" in str(excinfo.value)


# ------------------------------------------------- What counts as a shot


@pytest.mark.parametrize("profile", [
    "Cleaning/Forward Flush x5", "cleaning", "Test/temperature calibration",
    "Backflush", "Rinse",
])
def test_maintenance_profiles_are_not_coffee(profile: str) -> None:
    assert not is_real_shot(shot(profile_name=profile))


def test_an_abort_under_a_normal_profile_is_not_coffee() -> None:
    """The reason there are two criteria rather than the one the brief names.

    Measured across 171 shots: 22 produced under 5 g, and only 12 of those
    carry a maintenance profile name. The other 10 ran under "Default" or
    "D-Flow" - filtering on the name alone would have counted them as coffee.
    """
    assert not is_real_shot(shot(profile_name="D-Flow", yield_g=0.0))
    assert not is_real_shot(shot(profile_name="Default", yield_g=2.4))


def test_a_missing_yield_still_counts() -> None:
    """The scale may have been off. Dropping those would shrink the archive
    quietly, which is worse than counting one shot too many."""
    assert is_real_shot(shot(yield_g=None))


def test_an_ordinary_shot_counts() -> None:
    assert is_real_shot(shot())


# ------------------------------------------------------------- Summarising


def test_the_headline_figures() -> None:
    shots = [shot(id=f"s{i}", enjoyment=60.0 if i < 2 else None) for i in range(4)]
    result = summarise(shots, since=NOW - timedelta(days=2), until=NOW)

    assert result["shots"] == 4
    assert result["excluded"] == 0
    assert result["per_day"] == 2.0
    assert result["enjoyment"] == {"rated": 2, "unrated": 2, "mean": 60.0}
    assert result["averages"]["dose_g"] == 18.0
    assert result["averages"]["ratio"] == 2.0


def test_maintenance_is_counted_out_and_said_so() -> None:
    shots = [shot(id="a"), shot(id="b", profile_name="Cleaning/Forward Flush x5")]
    result = summarise(shots, since=NOW - timedelta(days=1), until=NOW)
    assert result["shots"] == 1
    assert result["excluded"] == 1


def test_the_busiest_day_counts_everything() -> None:
    """A day of flushes was still a day at the machine.

    Hiding maintenance there would make the number confusing rather than clean,
    so it is reported alongside.
    """
    shots = (
        [shot(id=f"r{i}", started_at="2026-09-10T08:00:00Z") for i in range(3)]
        + [shot(id=f"f{i}", started_at="2026-09-10T09:00:00Z",
                profile_name="Cleaning/Forward Flush x5") for i in range(5)]
        + [shot(id="other", started_at="2026-09-11T08:00:00Z")]
    )
    busiest = summarise(shots, since=NOW - timedelta(days=7), until=NOW)["busiest_day"]
    assert busiest == {"date": "2026-09-10", "total": 8, "real_shots": 3,
                       "maintenance": 5}


def test_top_lists_carry_their_rating_coverage() -> None:
    """A mean over one rating out of twenty is not the same claim as over ten."""
    shots = ([shot(id=f"a{i}", bean_name="Alpha", enjoyment=80.0) for i in range(2)]
             + [shot(id=f"b{i}", bean_name="Alpha") for i in range(8)]
             + [shot(id="c", bean_name="Beta", enjoyment=40.0)])
    beans = summarise(shots, since=NOW - timedelta(days=7), until=NOW)["beans"]

    alpha = next(b for b in beans if b["name"] == "Alpha")
    assert alpha["shots"] == 10
    assert alpha["rated"] == 2
    assert alpha["mean_enjoyment"] == 80.0


# ------------------------------------------------------ Grind over time


def test_two_spellings_of_one_setting_are_not_a_change() -> None:
    """Free text typed on a tablet.

    The archive holds "2.7", "2.70" and "2,8" for the same grinder; comparing
    the strings counted 13 changes on a bean that was moved 11 times.
    """
    shots = [
        shot(id="a", started_at="2026-09-10T08:00:00Z", grinder_setting="2.7"),
        shot(id="b", started_at="2026-09-11T08:00:00Z", grinder_setting="2.70"),
        shot(id="c", started_at="2026-09-12T08:00:00Z", grinder_setting="2,7"),
        shot(id="d", started_at="2026-09-13T08:00:00Z", grinder_setting="2.8"),
    ]
    grind = summarise(shots, since=NOW - timedelta(days=7), until=NOW)["grind_by_bean"]
    assert grind[0]["changes"] == 1
    assert grind[0]["settings"] == ["2.7", "2.8"], "reported as they were typed"


# ----------------------------------------------------------- Comparison


def test_deltas_against_the_previous_period() -> None:
    current = summarise([shot(id="a", enjoyment=80.0), shot(id="b")],
                        since=NOW - timedelta(days=1), until=NOW)
    previous = summarise([shot(id="c", enjoyment=60.0)],
                         since=NOW - timedelta(days=2), until=NOW - timedelta(days=1))
    result = compare(current, previous)

    assert result["deltas"]["shots"] == 1
    assert result["deltas"]["enjoyment_mean"] == 20.0
    assert result["previous"]["shots"] == 1


def test_a_delta_needs_both_sides() -> None:
    current = summarise([shot(enjoyment=80.0)], since=NOW - timedelta(days=1), until=NOW)
    previous = summarise([], since=NOW - timedelta(days=2), until=NOW - timedelta(days=1))
    assert compare(current, previous)["deltas"]["enjoyment_mean"] is None


# ----------------------------------------------------------- Batch usage


def test_batch_usage_reports_what_was_used() -> None:
    shots = [shot(id=f"s{i}", bean_batch_id="b1") for i in range(3)]
    batches = {"b1": {"id": "b1", "roast_date": "2026-09-01", "frozen": 0}}
    usage = batch_usage(shots, batches, at=NOW)

    assert usage[0]["shots"] == 3
    assert usage[0]["coffee_used_g"] == 54.0
    assert usage[0]["mean_dose_g"] == 18.0
    assert usage[0]["days_since_roast"] == 14


def test_batch_usage_reports_no_remainder() -> None:
    """Decaid keeps no weight on a batch.

    Verified against both the list and the single-item endpoint: a batch
    carries id, beanId, roastDate, buyDate, freezeDate, frozen, archived and
    the two timestamps. Without a starting weight there is no honest remainder,
    and inventing one would be worse than leaving it out.
    """
    usage = batch_usage([shot()], {"b1": {"id": "b1"}}, at=NOW)
    assert "shots_remaining" not in usage[0]
    assert "weight_remaining_g" not in usage[0]


def test_maintenance_does_not_consume_a_batch() -> None:
    shots = [shot(id="a"), shot(id="b", profile_name="Cleaning/Forward Flush x5")]
    usage = batch_usage(shots, {}, at=NOW)
    assert usage[0]["shots"] == 1


# ---------------------------------------------------------------- The tool


async def test_stats_answers_over_the_archive(config: Config, archive: Database) -> None:
    recent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    store_shot(archive, decaid_detail("de1app-1785525360", timestamp=recent,
                                      enjoyment=80.0))
    async with Client(build_mcp(config, archive)) as client:
        result = (await client.call_tool("stats", {"period": "30d"})).data

    assert result["period"] == "30d"
    assert result["shots"] == 1
    assert "comparison" in result


async def test_stats_can_skip_the_comparison(config: Config, archive: Database) -> None:
    async with Client(build_mcp(config, archive)) as client:
        result = (await client.call_tool(
            "stats", {"period": "7d", "compare_previous": False})).data
    assert "comparison" not in result


async def test_stats_refuses_a_period_it_cannot_read(
    config: Config, archive: Database
) -> None:
    async with Client(build_mcp(config, archive)) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("stats", {"period": "whenever"})
    assert "invalid_argument" in str(excinfo.value)
