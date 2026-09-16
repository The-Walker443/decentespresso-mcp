"""Batch master data on the read paths (SPEC §5.3, §9).

The bug these protect against: batches were synced and writable, and no read
path gave them out. The roast date of the batch in the hopper sat in the
database, correct, and was invisible to every client - so bean age, which is
one of the two or three variables a person actually turns while dialling in,
could not be asked about at all.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from helpers import decaid_detail, store_shot

from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_mapping import batch_row_from_decaid, bean_row_from_decaid
from decentespresso_mcp.guards import bean_age_days
from decentespresso_mcp.server import _stock, build_mcp

BEAN_ID = "bean-1"
SYNCED = "2026-09-16T06:00:00Z"


@pytest.fixture
def db(tmp_path: pathlib.Path) -> Iterator[Database]:
    database = Database(tmp_path / "batches.db")
    database.migrate()
    yield database
    database.close()


def stock(db: Database, batch: dict, *, started: str, bean: dict | None = None) -> str:
    """One shot pulled from ``batch``, with the bean behind it."""
    db.upsert_beans([bean_row_from_decaid(bean or {
        "id": BEAN_ID, "name": "Testsorte", "roaster": "Tchibo",
    }, SYNCED)])
    db.upsert_bean_batches([batch_row_from_decaid(batch, SYNCED)])
    detail = decaid_detail("de1app-1785525360", timestamp=started, enjoyment=None)
    detail["workflow"]["context"]["beanBatchId"] = batch["id"]
    store_shot(db, detail)
    db.link_shots_to_beans()
    return str(detail["id"])


async def call(mcp, name: str, args: dict | None = None):
    async with Client(mcp) as client:
        return (await client.call_tool(name, args or {})).data


# ------------------------------------------------------ The age is the shot's


async def test_get_shot_gives_the_batch_and_its_age(
    config: Config, db: Database
) -> None:
    shot_id = stock(db, {
        "id": "batch-roasted", "beanId": BEAN_ID,
        "roastDate": "2026-08-15T00:00:00.000Z",
    }, started="2026-08-29T08:00:00")

    result = await call(build_mcp(config, db), "get_shot", {"id": shot_id})
    batch = result["bean_batch"]

    assert batch["roast_date"] == "2026-08-15"
    assert batch["days_off_roast"] == 14
    assert batch["frozen"] is False
    assert "age_unknown_reason" not in batch


async def test_the_age_is_measured_against_the_shot_not_the_clock(
    config: Config, db: Database
) -> None:
    """Otherwise every historical analysis drifts as the calendar moves.

    An age taken against `now()` would make the same shot answer "how old was
    the bean" differently every week, and a comparison across a month would
    silently compare two different questions. This test pins a shot far in the
    past: the answer must be the same today and in a year.
    """
    shot_id = stock(db, {
        "id": "batch-old", "beanId": BEAN_ID,
        "roastDate": "2026-01-01T00:00:00.000Z",
    }, started="2026-01-11T08:00:00")

    result = await call(build_mcp(config, db), "get_shot", {"id": shot_id})
    assert result["bean_batch"]["days_off_roast"] == 10

    days_since = (datetime.now(UTC).date() - datetime(2026, 1, 11).date()).days
    assert days_since > 30, "the fixture must be old enough for this to mean anything"


# --------------------------------------------------------- No date, no number


async def test_a_batch_without_a_roast_date_says_so_instead_of_guessing(
    config: Config, db: Database
) -> None:
    """No fallback to the purchase date, the open date or zero.

    Those are different facts. A plausible number in place of a missing one is
    worse than the gap, because nothing downstream can tell them apart.
    """
    shot_id = stock(db, {
        "id": "batch-undated", "beanId": BEAN_ID,
        "buyDate": "2026-08-01T00:00:00.000Z",
        "openDate": "2026-08-02T00:00:00.000Z",
    }, started="2026-08-29T08:00:00")

    batch = (await call(build_mcp(config, db), "get_shot",
                        {"id": shot_id}))["bean_batch"]

    assert batch["roast_date"] is None
    assert batch["days_off_roast"] is None
    assert "no roast date" in batch["age_unknown_reason"]
    assert batch["buy_date"] == "2026-08-01"
    assert batch["open_date"] == "2026-08-02", "read, but never used as an age"


def test_the_guard_sits_out_a_missing_roast_date() -> None:
    """It must never compute an age it cannot substantiate."""
    age, certain = bean_age_days({"roast_date": None, "buy_date": "2026-01-01"},
                                 datetime(2026, 9, 16, tzinfo=UTC))
    assert age is None
    assert certain is False


async def test_the_audit_says_how_many_shots_it_sat_out(
    config: Config, db: Database
) -> None:
    """A silent skip looks exactly like a clean result, and is not one."""
    stock(db, {"id": "batch-undated", "beanId": BEAN_ID},
          started=(datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"))

    result = await call(build_mcp(config, db), "audit_archive")
    assert result["not_checked"]["shots_whose_batch_has_no_roast_date"] == 1
    assert "rather than computing an age" in result["not_checked"]["note"]


# -------------------------------------------------------------- The freezer


async def test_a_thawed_batch_without_a_thaw_date_is_an_upper_bound(
    config: Config, db: Database
) -> None:
    """Frozen once, thawed, no date: the time in the freezer cannot be removed.

    The age is then the largest it could be, and must be passed on as such
    rather than as a number.
    """
    shot_id = stock(db, {
        "id": "batch-thawed", "beanId": BEAN_ID,
        "roastDate": "2026-07-01T00:00:00.000Z",
        "freezeDate": "2026-07-10T00:00:00.000Z",
        "frozen": False,
    }, started="2026-08-29T08:00:00")

    batch = (await call(build_mcp(config, db), "get_shot",
                        {"id": shot_id}))["bean_batch"]

    assert batch["days_off_roast"] == 59
    assert batch["age_is_upper_bound"] is True
    assert "at most" in batch["age_upper_bound_reason"]


async def test_a_thaw_date_makes_the_age_exact(config: Config, db: Database) -> None:
    """Decaid does keep `unfreezeDate` - an earlier verification said otherwise.

    With it the freezer time is subtracted exactly: roasted 1 July, frozen on
    the 10th, out again on the 20th, pulled on 29 August. 59 days elapsed minus
    10 in the freezer is 49, and no upper-bound caveat.
    """
    shot_id = stock(db, {
        "id": "batch-thaw-known", "beanId": BEAN_ID,
        "roastDate": "2026-07-01T00:00:00.000Z",
        "freezeDate": "2026-07-10T00:00:00.000Z",
        "unfreezeDate": "2026-07-20T00:00:00.000",
        "frozen": False,
    }, started="2026-08-29T08:00:00")

    batch = (await call(build_mcp(config, db), "get_shot",
                        {"id": shot_id}))["bean_batch"]

    assert batch["days_off_roast"] == 49
    assert batch["thawed_on" if "thawed_on" in batch else "unfreeze_date"] == "2026-07-20"
    assert "age_is_upper_bound" not in batch


async def test_a_batch_still_frozen_stops_ageing(config: Config, db: Database) -> None:
    shot_id = stock(db, {
        "id": "batch-frozen", "beanId": BEAN_ID,
        "roastDate": "2026-07-01T00:00:00.000Z",
        "freezeDate": "2026-07-10T00:00:00.000Z",
        "frozen": True,
    }, started="2026-08-29T08:00:00")

    batch = (await call(build_mcp(config, db), "get_shot",
                        {"id": shot_id}))["bean_batch"]
    assert batch["days_off_roast"] == 9
    assert batch["frozen"] is True


# ----------------------------------------------------------- The lookup path


async def test_list_batches_gives_the_identifiers_update_batch_needs(
    config: Config, db: Database
) -> None:
    """Without this, a batch id could only be guessed out of a shot."""
    stock(db, {"id": "batch-roasted", "beanId": BEAN_ID,
               "roastDate": "2026-08-15T00:00:00.000Z"},
          started="2026-08-29T08:00:00")

    result = await call(build_mcp(config, db), "list_batches")
    batch = result["batches"][0]

    assert batch["id"] == "batch-roasted"
    assert batch["bean_name"] == "Testsorte"
    assert batch["roast_date"] == "2026-08-15"
    assert batch["shot_count"] == 1


async def test_get_batch_shows_the_state_before_it_is_overwritten(
    config: Config, db: Database
) -> None:
    stock(db, {"id": "batch-full", "beanId": BEAN_ID,
               "roastDate": "2026-08-15T00:00:00.000Z",
               "openDate": "2026-08-20T00:00:00.000",
               "freezeDate": "2026-08-25T00:00:00.000Z",
               "weight": 250.0, "weightRemaining": 180.0},
          started="2026-08-29T08:00:00")

    batch = await call(build_mcp(config, db), "get_batch", {"id": "batch-full"})

    assert batch["roast_date"] == "2026-08-15"
    assert batch["open_date"] == "2026-08-20"
    assert batch["frozen_since"] == "2026-08-25"
    assert batch["stock"]["bag_weight_g"] == 250.0
    assert batch["stock"]["recorded_remaining_g"] == 180.0


async def test_list_beans_carries_the_batches(config: Config, db: Database) -> None:
    stock(db, {"id": "batch-roasted", "beanId": BEAN_ID,
               "roastDate": "2026-08-15T00:00:00.000Z"},
          started="2026-08-29T08:00:00")

    bean = (await call(build_mcp(config, db), "list_beans"))["beans"][0]
    assert bean["batches"][0]["id"] == "batch-roasted"
    assert bean["batches"][0]["roast_date"] == "2026-08-15"


# ------------------------------------------------------------------- Origin


async def test_origin_travels_from_decaid_to_the_response(
    config: Config, db: Database
) -> None:
    stock(db, {"id": "batch-roasted", "beanId": BEAN_ID}, started="2026-08-29T08:00:00",
          bean={"id": BEAN_ID, "name": "House Blend", "roaster": "Seniman",
                "country": "Bali", "region": "Kintamani",
                "variety": ["Typica"], "altitude": [1100, 1300]})

    bean = (await call(build_mcp(config, db), "list_beans"))["beans"][0]
    assert bean["origin"] == {"country": "Bali", "region": "Kintamani",
                              "variety": ["Typica"], "altitude": [1100, 1300]}


def test_an_unset_origin_field_is_absent_not_a_placeholder() -> None:
    """Decaid's UI greys in "washed, natural, honey…" where nothing is set.

    The API omits the key instead, checked across every bean on the live
    instance. Importing the placeholder would turn a UI hint into a recorded
    fact about somebody's coffee.
    """
    row = bean_row_from_decaid({"id": BEAN_ID, "name": "House Blend"}, SYNCED)
    for field in ("country", "region", "producer", "variety", "altitude",
                  "processing", "species"):
        assert row[field] is None, field


async def test_a_bean_without_origin_has_no_origin_block(
    config: Config, db: Database
) -> None:
    stock(db, {"id": "batch-roasted", "beanId": BEAN_ID}, started="2026-08-29T08:00:00")
    bean = (await call(build_mcp(config, db), "list_beans"))["beans"][0]
    assert "origin" not in bean


# ------------------------------------------------- What is left of the bag


async def test_the_stock_is_derived_and_decaids_figure_is_kept_beside_it(
    config: Config, db: Database
) -> None:
    """Decaid initialises `weightRemaining` and never counts it down.

    Measured on the real archive: a 500 g bag reads 500 g remaining after
    twenty-seven shots. So the useful figure is bag weight minus what the shots
    consumed - but Decaid's own number stays visible next to it, because
    dropping it would make an estimate look like a reading.
    """
    stock_batch = {"id": "batch-stock", "beanId": BEAN_ID,
                   "roastDate": "2026-08-15T00:00:00.000Z",
                   "weight": 500.0, "weightRemaining": 500.0}
    stock(db, stock_batch, started="2026-08-29T08:00:00")

    batch = await call(build_mcp(config, db), "get_batch", {"id": "batch-stock"})
    reported = batch["stock"]

    assert reported["bag_weight_g"] == 500.0
    assert reported["recorded_remaining_g"] == 500.0
    assert reported["used_by_shots_g"] > 0
    assert reported["estimated_remaining_g"] < 500.0


def test_the_stock_note_fires_on_the_real_numbers() -> None:
    """The case from the archive: a 500 g bag reading 500 g left after 27 shots.

    One shot's worth of difference is noise and stays quiet; 486 g of it is not.
    """
    quiet = _stock({"weight_g": 500.0, "weight_remaining_g": 500.0,
                    "coffee_used_g": 18.0, "mean_dose_g": 18.0})
    assert "note" not in quiet

    loud = _stock({"weight_g": 500.0, "weight_remaining_g": 500.0,
                   "coffee_used_g": 486.0, "mean_dose_g": 18.0})
    assert loud["estimated_remaining_g"] == 14.0
    assert loud["shots_left_estimate"] == 0
    assert "does not count down" in loud["note"]


def test_no_weight_means_no_stock_block() -> None:
    """Most batches carry none, and an empty block would suggest an empty bag."""
    assert _stock({"coffee_used_g": 200.0, "mean_dose_g": 18.0}) is None


async def test_provenance_reaches_the_response(config: Config, db: Database) -> None:
    """Seven fields that had no column until a batch was filled in properly."""
    stock(db, {"id": "batch-rich", "beanId": BEAN_ID,
               "roastDate": "2026-08-15T00:00:00.000Z",
               "roastLevel": "Medium", "harvestDate": "2026",
               "qualityScore": 77.0, "price": 3.0, "currency": "EUR",
               "notes": "test"},
          started="2026-08-29T08:00:00")

    batch = await call(build_mcp(config, db), "get_batch", {"id": "batch-rich"})

    assert batch["roast_level"] == "Medium"
    assert batch["harvest"] == "2026", "a season, kept as text"
    assert batch["quality_score"] == 77.0
    assert batch["price"] == {"amount": 3.0, "currency": "EUR"}
    assert batch["notes"] == "test"


async def test_a_price_without_a_currency_is_not_reported_as_euros(
    config: Config, db: Database
) -> None:
    stock(db, {"id": "batch-price", "beanId": BEAN_ID, "price": 3.0},
          started="2026-08-29T08:00:00")
    batch = await call(build_mcp(config, db), "get_batch", {"id": "batch-price"})
    assert batch["price"] == {"amount": 3.0}
