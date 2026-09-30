"""Freezer history from Decaid's fields or Beanie's events (SPEC T57).

Beanie (a54a625) keeps [{type: "frozen"|"thawed", at: ISO}] under
extras.storageEvents and sets only `frozen`; Decaid replaces `extras` whole on
a PUT (measured 2026-09-28 on a throwaway batch, deleted afterwards).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from decentespresso_mcp.freezing import events_of, frozen_periods, mirrored_events
from decentespresso_mcp.guards import bean_age_days
from decentespresso_mcp.sync import _with_storage_events

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def events(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"type": kind, "at": at} for kind, at in pairs]


def batch(**fields) -> dict:
    return {"roast_date": "2026-08-01", "freeze_date": None, "unfreeze_date": None,
            "frozen": 0, "storage_events": None, **fields}


def test_events_are_read_as_beanie_normalises_them() -> None:
    """Valid entries only, in time order - whatever order they were written in."""
    raw = events(("thawed", "2026-09-10T08:00:00.000Z"), ("frozen", "2026-09-01T08:00:00.000Z"))
    raw.append({"type": "melted", "at": "2026-09-02T08:00:00.000Z"})
    raw.append({"type": "frozen", "at": "not a date"})
    assert [e["type"] for e in events_of(raw)] == ["frozen", "thawed"]


def test_two_stays_in_the_freezer_are_both_subtracted() -> None:
    """The brief's case: roast age minus every frozen period, not the last."""
    history = events(("frozen", "2026-08-05T08:00:00.000Z"), ("thawed", "2026-08-15T08:00:00.000Z"),
                     ("frozen", "2026-08-20T08:00:00.000Z"), ("thawed", "2026-09-10T08:00:00.000Z"))
    b = batch(storage_events=history)
    periods, source, certain = frozen_periods(b)
    assert source == "storageEvents" and certain
    assert periods == [(date(2026, 8, 5), date(2026, 8, 15)),
                       (date(2026, 8, 20), date(2026, 9, 10))]
    age, _ = bean_age_days(b, NOW)
    assert age == 58 - 10 - 21


def test_a_bean_in_the_freezer_stops_ageing() -> None:
    b = batch(storage_events=events(("frozen", "2026-08-11T08:00:00.000Z")), frozen=1)
    assert bean_age_days(b, NOW) == (10, True)


def test_a_second_freeze_without_a_thaw_keeps_the_first_start() -> None:
    """Beanie's frozenIntervals: other clients can write such sequences."""
    b = batch(storage_events=events(("frozen", "2026-08-11T08:00:00.000Z"),
                                    ("frozen", "2026-08-20T08:00:00.000Z")), frozen=1)
    assert frozen_periods(b)[0] == [(date(2026, 8, 11), None)]


def test_a_shot_pulled_between_two_freezes_counts_only_the_first() -> None:
    history = events(("frozen", "2026-08-05T08:00:00.000Z"), ("thawed", "2026-08-15T08:00:00.000Z"),
                     ("frozen", "2026-08-20T08:00:00.000Z"))
    b = batch(storage_events=history, frozen=1)
    age, _ = bean_age_days(b, NOW, started=datetime(2026, 8, 18, 8, tzinfo=UTC))
    assert age == 17 - 10


def test_decaids_own_fields_win_over_the_events() -> None:
    b = batch(freeze_date="2026-08-10", unfreeze_date="2026-08-20",
              storage_events=events(("frozen", "2026-08-01T08:00:00.000Z")))
    periods, source, _ = frozen_periods(b)
    assert source == "fields" and periods == [(date(2026, 8, 10), date(2026, 8, 20))]


def test_thawed_without_a_date_stays_an_upper_bound() -> None:
    """The old rule holds for the fields: an age that cannot be substantiated."""
    assert bean_age_days(batch(freeze_date="2026-08-10"), NOW) == (58, False)


def test_a_freeze_is_written_as_beanie_would() -> None:
    """appendBatchStorageEvent: a new type appends, at the day given."""
    out = mirrored_events([], {"frozen": True, "freezeDate": "2026-09-20"}, NOW)
    assert out == events(("frozen", "2026-09-20T10:00:00.000Z"))


def test_correcting_a_date_replaces_the_latest_event_and_keeps_its_time() -> None:
    current = events(("frozen", "2026-09-20T07:30:00.000Z"))
    out = mirrored_events(current, {"freezeDate": "2026-09-19"}, NOW)
    assert out == events(("frozen", "2026-09-19T07:30:00.000Z"))


def test_a_thaw_is_appended_and_other_writes_leave_the_events_alone() -> None:
    current = events(("frozen", "2026-09-20T07:30:00.000Z"))
    out = mirrored_events(current, {"frozen": False, "unfreezeDate": "2026-09-27"}, NOW)
    assert [e["type"] for e in out] == ["frozen", "thawed"]
    assert mirrored_events(current, {"notes": "x"}, NOW) is None


def test_the_rest_of_extras_goes_along() -> None:
    """Decaid replaces extras whole on a PUT (T57) - dropping a key someone
    else keeps there would be a silent loss."""
    raw = {"extras": {"somethingElse": 1}}
    out = _with_storage_events(raw, {"frozen": True, "freezeDate": "2026-09-20"})
    assert out["extras"]["somethingElse"] == 1
    assert out["extras"]["storageEvents"][0]["type"] == "frozen"
    assert _with_storage_events(raw, {"notes": "x"}) == {"notes": "x"}
