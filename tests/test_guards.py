"""Guards over the archive (SPEC §20.7).

Pure functions, therefore pure tests: rows in, findings out, no network and no
database. The clock is handed in - otherwise the result would depend on the
day the test runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from decentespresso_mcp.guards import (
    ALL_RULES,
    RULE_BEAN_AGE,
    RULE_DOSE_OUTLIER,
    RULE_GRIND,
    RULE_MISSING_RATING,
    bean_age_days,
    dose_outliers,
    grind_not_adjusted,
    missing_rating,
    run_rules,
    stale_beans,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def shot(sid: str, *, at: str, batch: str | None = "b1", grind: str = "3.30",
         dose: float | None = 18.0, target: float | None = 18.0,
         yielded: float | None = 45.0, target_yield: float | None = 45.0,
         enjoyment: float | None = 80.0) -> dict:
    return {
        "id": sid, "started_at": at, "bean_batch_id": batch,
        "grinder_setting": grind, "dose_g": dose, "target_dose_g": target,
        "yield_g": yielded, "target_yield_g": target_yield,
        "enjoyment": enjoyment,
    }


def batch(bid: str = "b1", *, roast: str | None = "2026-09-01",
          freeze: str | None = None, frozen: bool = False,
          thaw: str | None = None) -> dict:
    return {"id": bid, "roast_date": roast, "freeze_date": freeze,
            "frozen": 1 if frozen else 0, "unfreeze_date": thaw}


# -------------------------------------------- Rule 1: grind setting


def test_a_batch_change_without_a_grind_change_is_flagged() -> None:
    findings = grind_not_adjusted([
        shot("a", at="2026-09-10T08:00:00Z", batch="b1", grind="3.30"),
        shot("b", at="2026-09-11T08:00:00Z", batch="b2", grind="3.30"),
    ])
    assert [f.shot_id for f in findings] == ["b"]
    assert findings[0].rule == RULE_GRIND
    assert "3.30" in findings[0].message


def test_a_batch_change_with_a_grind_change_is_fine() -> None:
    assert grind_not_adjusted([
        shot("a", at="2026-09-10T08:00:00Z", batch="b1", grind="3.30"),
        shot("b", at="2026-09-11T08:00:00Z", batch="b2", grind="3.10"),
    ]) == []


def test_only_the_shot_at_the_change_is_flagged() -> None:
    """Otherwise every batch would keep reporting until someone touches the grinder."""
    findings = grind_not_adjusted([
        shot("a", at="2026-09-10T08:00:00Z", batch="b1"),
        shot("b", at="2026-09-11T08:00:00Z", batch="b2"),
        shot("c", at="2026-09-12T08:00:00Z", batch="b2"),
    ])
    assert [f.shot_id for f in findings] == ["b"]


def test_an_unknown_batch_does_not_count_as_a_change() -> None:
    """Shots imported from the de1app often have no batch."""
    assert grind_not_adjusted([
        shot("a", at="2026-09-10T08:00:00Z", batch="b1"),
        shot("b", at="2026-09-11T08:00:00Z", batch=None),
        shot("c", at="2026-09-12T08:00:00Z", batch="b1"),
    ]) == []


def test_a_single_shot_cannot_be_a_change() -> None:
    assert grind_not_adjusted([shot("a", at="2026-09-10T08:00:00Z")]) == []


# ----------------------------------------------- Rule 2: bean age


def test_plain_age_without_freezing() -> None:
    age, certain = bean_age_days(batch(roast="2026-08-01"), NOW)
    assert (age, certain) == (44, True)


def test_freezing_stops_the_clock() -> None:
    """Frozen ten days after roasting - and there it stays."""
    age, certain = bean_age_days(
        batch(roast="2026-08-01", freeze="2026-08-11", frozen=True), NOW
    )
    assert (age, certain) == (10, True)


def test_a_known_thaw_date_is_subtracted() -> None:
    age, certain = bean_age_days(
        batch(roast="2026-08-01", freeze="2026-08-11", frozen=False,
              thaw="2026-09-01"), NOW
    )
    # 44 Tage insgesamt, 21 davon eingefroren.
    assert (age, certain) == (23, True)


def test_an_unknown_thaw_date_yields_an_upper_bound() -> None:
    """Decaid keeps no thaw date (checked against the API on 2026-09-14).

    A number that cannot be substantiated is not reported as certain.
    """
    age, certain = bean_age_days(
        batch(roast="2026-08-01", freeze="2026-08-11", frozen=False), NOW
    )
    assert age == 44
    assert certain is False


def test_age_is_measured_at_the_shot_not_today() -> None:
    """Otherwise old shots would age retroactively into the warning."""
    age, _ = bean_age_days(
        batch(roast="2026-08-01"), NOW,
        started=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert age == 4


def test_no_roast_date_means_no_statement() -> None:
    assert bean_age_days(batch(roast=None), NOW) == (None, False)


def test_a_roast_date_after_the_shot_is_not_negative_age() -> None:
    assert bean_age_days(batch(roast="2026-10-01"), NOW) == (None, False)


def test_stale_beans_reports_the_shot() -> None:
    findings = stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z")],
        {"b1": batch(roast="2026-07-01")},
        at=NOW, warn_days=42,
    )
    assert len(findings) == 1
    assert findings[0].rule == RULE_BEAN_AGE
    assert findings[0].detail["certain"] is True
    assert "75 days" in findings[0].message


def test_an_uncertain_age_says_so_in_the_message() -> None:
    findings = stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z")],
        {"b1": batch(roast="2026-07-01", freeze="2026-07-10", frozen=False)},
        at=NOW, warn_days=42,
    )
    assert "at least" in findings[0].message
    assert findings[0].detail["certain"] is False


def test_a_fresh_bean_is_not_flagged() -> None:
    assert stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z")],
        {"b1": batch(roast="2026-09-01")}, at=NOW, warn_days=42,
    ) == []


def test_a_shot_without_a_known_batch_is_skipped() -> None:
    assert stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z", batch="does-not-exist")],
        {"b1": batch()}, at=NOW,
    ) == []


# ----------------------------------------- Rule 3: missing rating


def test_an_unrated_shot_inside_the_window_is_flagged() -> None:
    findings = missing_rating(
        [shot("a", at="2026-09-10T08:00:00Z", enjoyment=None)],
        at=NOW, grace_hours=36,
    )
    assert len(findings) == 1
    assert findings[0].rule == RULE_MISSING_RATING
    assert "100 h" in findings[0].message


def test_a_long_past_shot_is_left_alone() -> None:
    """145 of the 169 shots in the archive are unrated.

    Without an upper bound the rule reported 83 percent of the archive - and
    what fires that often gets ignored wholesale. Whoever has not rated a shot
    from the week before last will not do so now.
    """
    assert missing_rating(
        [shot("alt", at="2026-07-01T08:00:00Z", enjoyment=None)],
        at=NOW, grace_hours=36, window_hours=7 * 24,
    ) == []


def test_a_recent_unrated_shot_is_left_alone() -> None:
    """One drinks it first - right afterwards no rating is normal."""
    assert missing_rating(
        [shot("a", at="2026-09-14T08:00:00Z", enjoyment=None)],
        at=NOW, grace_hours=36,
    ) == []


def test_a_rated_shot_is_never_flagged() -> None:
    assert missing_rating(
        [shot("a", at="2026-08-01T08:00:00Z", enjoyment=60.0)], at=NOW
    ) == []


def test_a_zero_rating_counts_as_rated() -> None:
    """Depends on the normalisation: in the archive 0 is an input, never a
    default (SPEC §20.4). Otherwise the rule would report 75 shots of the
    import era."""
    assert missing_rating(
        [shot("a", at="2026-08-01T08:00:00Z", enjoyment=0.0)], at=NOW
    ) == []


# ------------------------------------------------- Rule 4: weights


def test_a_yield_far_from_the_target_is_flagged() -> None:
    """On this machine the dose *is* the target - the yield is what gets measured.

    Measured against the archive: in all 165 cases the dose exactly equals the
    target. Comparing the two compared a number with itself.
    """
    findings = dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", yielded=29.5, target_yield=45.0)],
        tolerance_g=1.0,
    )
    assert len(findings) == 1
    assert findings[0].rule == RULE_DOSE_OUTLIER
    assert findings[0].detail["delta_g"] == -15.5
    assert findings[0].detail["basis"] == "target"


def test_an_impossible_dose_is_a_scale_fault() -> None:
    """0 g means the scale was not connected. That occurs in the archive."""
    findings = dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", dose=0.0)]
    )
    assert len(findings) == 1
    assert "impossible" in findings[0].message
    assert "scale" in findings[0].message


def test_a_yield_inside_the_tolerance_is_fine() -> None:
    """Das Bezugsgewicht schwankt von Natur aus - im Mittel 8,9 g."""
    assert dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", yielded=47.0, target_yield=45.0)],
        tolerance_g=1.0,
    ) == []


def test_without_a_target_the_batch_median_is_the_yardstick() -> None:
    shots = [
        shot(f"s{i}", at=f"2026-09-0{i+1}T08:00:00Z", yielded=45.0,
             target_yield=None)
        for i in range(5)
    ] + [shot("weit", at="2026-09-10T08:00:00Z", yielded=20.0, target_yield=None)]
    findings = dose_outliers(shots, tolerance_g=1.0)
    assert [f.shot_id for f in findings] == ["weit"]
    assert findings[0].detail["basis"] == "batch median"


def test_too_few_shots_make_no_yardstick() -> None:
    """Otherwise a single mishap would set the yardstick itself."""
    assert dose_outliers([
        shot("a", at="2026-09-10T08:00:00Z", yielded=45.0, target_yield=None),
        shot("b", at="2026-09-11T08:00:00Z", yielded=20.0, target_yield=None),
    ]) == []


def test_a_shot_without_weights_is_skipped() -> None:
    assert dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", dose=None, yielded=None)]
    ) == []


# ------------------------------------------------- All together


def test_run_rules_applies_every_enabled_rule() -> None:
    shots = [
        shot("alt", at="2026-09-10T08:00:00Z", batch="b1", enjoyment=None),
        shot("neu", at="2026-09-11T08:00:00Z", batch="b2", yielded=20.0),
    ]
    findings = run_rules(shots, {"b1": batch("b1", roast="2026-05-01"),
                                 "b2": batch("b2", roast="2026-05-01")},
                         at=NOW)
    rules = {f.rule for f in findings}
    assert RULE_GRIND in rules
    assert RULE_BEAN_AGE in rules
    assert RULE_MISSING_RATING in rules
    assert RULE_DOSE_OUTLIER in rules


def test_a_switched_off_rule_stays_quiet() -> None:
    shots = [shot("a", at="2026-09-10T08:00:00Z", enjoyment=None)]
    findings = run_rules(shots, {}, at=NOW,
                         enabled=[r for r in ALL_RULES if r != RULE_MISSING_RATING])
    assert all(f.rule != RULE_MISSING_RATING for f in findings)


def test_no_rules_means_no_findings() -> None:
    shots = [shot("a", at="2026-09-10T08:00:00Z", enjoyment=None, yielded=20.0)]
    assert run_rules(shots, {}, at=NOW, enabled=[]) == []


def test_findings_come_newest_first() -> None:
    shots = [
        shot("alt", at="2026-09-09T08:00:00Z", enjoyment=None),
        shot("neu", at="2026-09-11T08:00:00Z", enjoyment=None),
    ]
    findings = run_rules(shots, {}, at=NOW, enabled=[RULE_MISSING_RATING])
    assert [f.shot_id for f in findings] == ["neu", "alt"]


def test_the_limit_caps_the_answer() -> None:
    shots = [shot(f"s{i}", at=f"2026-09-0{i+8}T08:00:00Z", enjoyment=None)
             for i in range(5)]
    assert len(run_rules(shots, {}, at=NOW, enabled=[RULE_MISSING_RATING],
                         limit=2)) == 2


@pytest.mark.parametrize("rule", ALL_RULES)
def test_no_finding_ever_carries_free_text(rule: str) -> None:
    """Findings leave the house over ntfy - notes stay here.

    The input deliberately carries a note and a bean name; neither may appear
    in any field of the finding.
    """
    secret = "streng-vertraulich"
    shots = [
        {**shot("a", at="2026-09-09T08:00:00Z", batch="b1", enjoyment=None),
         "notes": secret, "bean_name": secret},
        {**shot("b", at="2026-09-11T08:00:00Z", batch="b2", yielded=20.0,
                enjoyment=None),
         "notes": secret, "bean_name": secret},
    ]
    findings = run_rules(shots, {"b1": batch("b1", roast="2026-01-01"),
                                 "b2": batch("b2", roast="2026-01-01")},
                         at=NOW, enabled=[rule])
    assert findings, "otherwise the test checks nothing"
    for finding in findings:
        assert secret not in str(finding.as_dict())
