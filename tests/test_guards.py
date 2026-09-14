"""Waechter ueber dem Archiv (SPEC ss20.7).

Reine Funktionen, deshalb reine Tests: Zeilen rein, Befunde raus, kein Netz und
keine Datenbank. Die Uhr wird uebergeben - sonst haengt das Ergebnis am Tag, an
dem der Test laeuft.
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


# ------------------------------------------- Regel 1: Mahlgrad


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
    """Sonst meldete jede Charge so lange, bis jemand die Muehle anfasst."""
    findings = grind_not_adjusted([
        shot("a", at="2026-09-10T08:00:00Z", batch="b1"),
        shot("b", at="2026-09-11T08:00:00Z", batch="b2"),
        shot("c", at="2026-09-12T08:00:00Z", batch="b2"),
    ])
    assert [f.shot_id for f in findings] == ["b"]


def test_an_unknown_batch_does_not_count_as_a_change() -> None:
    """Aus der de1app importierte Bezuege haben oft keine Charge."""
    assert grind_not_adjusted([
        shot("a", at="2026-09-10T08:00:00Z", batch="b1"),
        shot("b", at="2026-09-11T08:00:00Z", batch=None),
        shot("c", at="2026-09-12T08:00:00Z", batch="b1"),
    ]) == []


def test_a_single_shot_cannot_be_a_change() -> None:
    assert grind_not_adjusted([shot("a", at="2026-09-10T08:00:00Z")]) == []


# ------------------------------------------- Regel 2: Bohnenalter


def test_plain_age_without_freezing() -> None:
    age, certain = bean_age_days(batch(roast="2026-08-01"), NOW)
    assert (age, certain) == (44, True)


def test_freezing_stops_the_clock() -> None:
    """Zehn Tage nach der Roestung eingefroren - dabei bleibt es."""
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
    """Decaid fuehrt kein Auftaudatum (am 2026-09-14 gegen die API geprueft).

    Eine Zahl, die man nicht belegen kann, wird nicht als sicher ausgegeben.
    """
    age, certain = bean_age_days(
        batch(roast="2026-08-01", freeze="2026-08-11", frozen=False), NOW
    )
    assert age == 44
    assert certain is False


def test_age_is_measured_at_the_shot_not_today() -> None:
    """Sonst altern alte Bezuege rueckwirkend in die Warnung hinein."""
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
    assert "75 Tage" in findings[0].message


def test_an_uncertain_age_says_so_in_the_message() -> None:
    findings = stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z")],
        {"b1": batch(roast="2026-07-01", freeze="2026-07-10", frozen=False)},
        at=NOW, warn_days=42,
    )
    assert "mindestens" in findings[0].message
    assert findings[0].detail["certain"] is False


def test_a_fresh_bean_is_not_flagged() -> None:
    assert stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z")],
        {"b1": batch(roast="2026-09-01")}, at=NOW, warn_days=42,
    ) == []


def test_a_shot_without_a_known_batch_is_skipped() -> None:
    assert stale_beans(
        [shot("a", at="2026-09-14T08:00:00Z", batch="gibt-es-nicht")],
        {"b1": batch()}, at=NOW,
    ) == []


# ------------------------------------------- Regel 3: fehlende Bewertung


def test_an_unrated_shot_inside_the_window_is_flagged() -> None:
    findings = missing_rating(
        [shot("a", at="2026-09-10T08:00:00Z", enjoyment=None)],
        at=NOW, grace_hours=36,
    )
    assert len(findings) == 1
    assert findings[0].rule == RULE_MISSING_RATING
    assert "100 h" in findings[0].message


def test_a_long_past_shot_is_left_alone() -> None:
    """Am Bestand sind 145 von 169 Bezuegen unbewertet.

    Ohne Obergrenze meldete die Regel 83 Prozent des Archivs - und was so
    oft anschlaegt, wird im Ganzen ignoriert. Wer einen Bezug von vorletzter
    Woche nicht bewertet hat, tut es nicht mehr.
    """
    assert missing_rating(
        [shot("alt", at="2026-07-01T08:00:00Z", enjoyment=None)],
        at=NOW, grace_hours=36, window_hours=7 * 24,
    ) == []


def test_a_recent_unrated_shot_is_left_alone() -> None:
    """Man trinkt ja erst - direkt danach ist keine Bewertung normal."""
    assert missing_rating(
        [shot("a", at="2026-09-14T08:00:00Z", enjoyment=None)],
        at=NOW, grace_hours=36,
    ) == []


def test_a_rated_shot_is_never_flagged() -> None:
    assert missing_rating(
        [shot("a", at="2026-08-01T08:00:00Z", enjoyment=60.0)], at=NOW
    ) == []


def test_a_zero_rating_counts_as_rated() -> None:
    """Haengt an der Normalisierung: im Archiv ist 0 eine Eingabe, nie ein
    Vorgabewert (SPEC ss20.4). Sonst meldete die Regel 75 Bezuege der
    Import-Aera."""
    assert missing_rating(
        [shot("a", at="2026-08-01T08:00:00Z", enjoyment=0.0)], at=NOW
    ) == []


# ------------------------------------------- Regel 4: Dosis


def test_a_yield_far_from_the_target_is_flagged() -> None:
    """Die Dosis ist in dieser Maschine der Sollwert - das Gewicht misst.

    Am Bestand gemessen: in allen 165 Faellen ist die Dosis exakt gleich dem
    Soll. Ein Vergleich beider verglich eine Zahl mit sich selbst.
    """
    findings = dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", yielded=29.5, target_yield=45.0)],
        tolerance_g=1.0,
    )
    assert len(findings) == 1
    assert findings[0].rule == RULE_DOSE_OUTLIER
    assert findings[0].detail["delta_g"] == -15.5
    assert findings[0].detail["basis"] == "Soll"


def test_an_impossible_dose_is_a_scale_fault() -> None:
    """0 g heisst: die Waage war nicht verbunden. Am Bestand kommt das vor."""
    findings = dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", dose=0.0)]
    )
    assert len(findings) == 1
    assert "unmoeglich" in findings[0].message
    assert "Waage" in findings[0].message


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
    assert findings[0].detail["basis"] == "Median der Charge"


def test_too_few_shots_make_no_yardstick() -> None:
    """Sonst bestimmte ein einzelner Fehlgriff selbst den Massstab."""
    assert dose_outliers([
        shot("a", at="2026-09-10T08:00:00Z", yielded=45.0, target_yield=None),
        shot("b", at="2026-09-11T08:00:00Z", yielded=20.0, target_yield=None),
    ]) == []


def test_a_shot_without_weights_is_skipped() -> None:
    assert dose_outliers(
        [shot("a", at="2026-09-14T08:00:00Z", dose=None, yielded=None)]
    ) == []


# ------------------------------------------- Zusammenspiel


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
    """Befunde gehen per ntfy aus dem Haus - Notizen bleiben hier.

    Die Eingabe traegt absichtlich eine Notiz und einen Bohnennamen; beides
    darf in keinem Feld des Befunds auftauchen.
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
    assert findings, "sonst prueft der Test nichts"
    for finding in findings:
        assert secret not in str(finding.as_dict())
