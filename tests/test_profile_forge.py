"""Deriving per-coffee profiles (SPEC §11.5) - the pure part.

The fixtures are five real records from the live instance (2026-09-26, Decaid
0.8.6): the operator's visible D-Flow, an earlier hidden edit of it, and three
bundled defaults chosen for their edge cases.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from decentespresso_mcp.profile_forge import (
    TITLE_SEPARATOR,
    apply_overrides,
    changes_brewing,
    check_title,
    default_title,
    is_default,
    main_step,
    resolve_profile,
    running_profile,
    same_brew,
)
from decentespresso_mcp.writes import ValidationError

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"
RECORDS = json.loads((FIXTURES / "profiles_sample.json").read_text(encoding="utf-8"))
WORKFLOW = json.loads((FIXTURES / "workflow_unsaved_dflow.json").read_text(encoding="utf-8"))


def record(title: str, visibility: str = "visible") -> dict:
    return next(r for r in RECORDS if r["profile"]["title"] == title
                and r["visibility"] == visibility)


def problems(profile: dict, overrides: dict) -> list[str]:
    with pytest.raises(ValidationError) as excinfo:
        apply_overrides(profile, overrides)
    return excinfo.value.problems


DFLOW = record("D-Flow")["profile"]
BLOOMING = record("Blooming Espresso")["profile"]
ADAPTIVE = record("Adaptive v3")["profile"]


# --------------------------------------------------------------- Temperature


def test_the_curve_keeps_its_shape_when_the_temperature_moves() -> None:
    """The live D-Flow fills 0.5 C above its pour, and that half degree stays.

    A first version set every step to one value and refused any profile whose
    steps differed. On the real instance that refused the operator's own
    D-Flow - the profile the milestone's acceptance was built around. D-Flow's
    editor has separate fill and pour temperatures; the offset is a design
    decision, so it is shifted rather than flattened.
    """
    profile, changes = apply_overrides(DFLOW, {"temperature_c": 91.5})
    temps = [(s["name"], s["temperature"]) for s in profile["steps"]]
    assert temps == [("Filling", 92.0), ("Infusing", 91.5), ("Pouring", 91.5)]
    assert changes["temperature_c"] == {"before": 88.0, "after": 91.5,
                                        "curve_shifted_by": 3.5}


def test_a_uniform_profile_just_takes_the_value() -> None:
    uniform = record("D-Flow", "hidden")["profile"]
    profile, changes = apply_overrides(uniform, {"temperature_c": 90})
    assert {s["temperature"] for s in profile["steps"]} == {90.0}
    assert "curve_shifted_by" not in changes["temperature_c"]


def test_a_shift_that_pushes_a_step_past_the_limit_is_refused() -> None:
    """Every step written is a step this server writes, so every one is bounded.

    Adaptive v3 fills 5 C above its extraction; at 92 C extraction the fill
    would land at 97 - over the limit, although 92 itself is not.
    """
    found = problems(ADAPTIVE, {"temperature_c": 92})
    assert "outside 80 to 96" in found[0]
    assert "97" in found[0]


def test_a_curve_without_an_anchor_is_left_alone() -> None:
    """Blooming Espresso ends on a 1 s reset step: no temperature is 'the' one."""
    found = problems(BLOOMING, {"temperature_c": 92})
    assert "change it on the tablet" in found[0]


@pytest.mark.parametrize("value", [79.9, 96.1, 150, -5])
def test_temperatures_outside_the_hard_limits_are_refused(value) -> None:
    assert "outside 80 to 96" in problems(DFLOW, {"temperature_c": value})[0]


@pytest.mark.parametrize("value", [80, 96, "91,5"])
def test_the_limits_themselves_and_decimal_commas_pass(value) -> None:
    """On a uniform profile - on the live D-Flow 96 would put the fill at 96.5."""
    apply_overrides(record("D-Flow", "hidden")["profile"], {"temperature_c": value})


def test_the_fill_lead_counts_against_the_upper_limit() -> None:
    assert "Filling 96.5" in problems(DFLOW, {"temperature_c": 96})[0]


# --------------------------------------------------- Weight and main setpoint


@pytest.mark.parametrize(("value", "ok"), [(10, True), (100, True), (9.9, False),
                                           (100.1, False)])
def test_the_weight_limits(value, ok) -> None:
    if ok:
        profile, _ = apply_overrides(DFLOW, {"target_weight_g": value})
        assert profile["target_weight"] == float(value)
    else:
        assert "outside 10 to 100" in problems(DFLOW, {"target_weight_g": value})[0]


def test_the_main_setpoint_is_the_pour_when_it_is_also_the_longest_step() -> None:
    profile, changes = apply_overrides(DFLOW, {"main_setpoint": 2.0})
    assert profile["steps"][-1]["flow"] == 2.0
    assert changes["main_setpoint"]["step"] == "Pouring"
    assert changes["main_setpoint"]["unit"] == "ml/s"


def test_a_flow_setpoint_is_bounded_at_eight() -> None:
    assert "outside 0.1 to 8 ml/s" in problems(DFLOW, {"main_setpoint": 8.5})[0]


def test_no_main_step_means_no_main_setpoint() -> None:
    """Picking a setpoint for Blooming Espresso would be a guess dressed as a setting."""
    assert main_step(BLOOMING["steps"]) is None
    assert "cannot be said without guessing" in problems(BLOOMING, {"main_setpoint": 2})[0]


def test_an_unverified_field_is_refused_with_the_verified_ones() -> None:
    found = problems(DFLOW, {"preinfusion_s": 10})
    assert "not changeable here" in found[0]
    assert "temperature_c" in found[0]


def test_every_problem_comes_back_at_once() -> None:
    assert len(problems(DFLOW, {"temperature_c": 200, "target_weight_g": 1,
                                "nonsense": 1})) == 3


def test_the_source_is_never_modified() -> None:
    before = json.dumps(DFLOW, sort_keys=True)
    apply_overrides(DFLOW, {"temperature_c": 91.5, "target_weight_g": 40})
    assert json.dumps(DFLOW, sort_keys=True) == before


# ------------------------------------------------------ Identity by content


def test_a_rename_alone_does_not_change_the_brew() -> None:
    """Measured (T39): Decaid answered a title-only copy with 201 and the
    original's id, silently dropping the new title, parent and metadata."""
    _, changes = apply_overrides(DFLOW, {}, title="Something else")
    assert not changes_brewing(changes)


def test_same_brew_ignores_labels_and_number_spelling() -> None:
    renamed = dict(DFLOW, title="x", notes="y", author="z")
    assert same_brew(DFLOW, renamed)
    respelled = json.loads(json.dumps(DFLOW).replace("88.0", "88"))
    assert same_brew(DFLOW, respelled)


def test_the_live_workflow_ran_a_profile_that_is_no_record() -> None:
    """The case the unsaved-profile guard exists for.

    Measured on 2026-09-26: the workflow's D-Flow (pour 1.5 ml/s, limiter
    9 bar, fill weight 4 g) matched none of 85 stored profiles. It was tuned on
    the tablet and lived only in the workflow.
    """
    assert running_profile(WORKFLOW["profile"], RECORDS) is None


def test_running_profile_finds_a_record_by_content() -> None:
    target = record("D-Flow")
    assert running_profile(dict(target["profile"], title="renamed"), RECORDS) == target


# --------------------------------------------------------- Finding profiles


def test_a_title_means_the_visible_one() -> None:
    """Every tablet edit leaves the old version behind, hidden, same title.

    The live instance holds four "D-Flow"s. Someone naming one means the one
    they can see; asking them to pick an id would be pedantry.
    """
    assert resolve_profile(RECORDS, "d-flow")["visibility"] == "visible"


def test_an_id_finds_a_hidden_record_too() -> None:
    hidden = record("D-Flow", "hidden")
    assert resolve_profile(RECORDS, hidden["id"]) == hidden


def test_an_unknown_reference_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        resolve_profile(RECORDS, "No Such Profile")
    assert "on_tablet" in excinfo.value.problems[0]


def test_a_deleted_profile_is_never_chosen() -> None:
    deleted = [dict(RECORDS[0], visibility="deleted")]
    with pytest.raises(ValidationError):
        resolve_profile(deleted, RECORDS[0]["id"])


def test_defaults_are_recognised() -> None:
    assert is_default(record("D-Flow / default"))
    assert not is_default(record("D-Flow"))


# ------------------------------------------------------------------ Titles


def test_the_naming_convention() -> None:
    assert default_title("Tugu Kawisari", "Arabica Honey Process") == (
        f"Tugu Kawisari{TITLE_SEPARATOR}Arabica Honey Process")
    assert TITLE_SEPARATOR == " – ", "an en dash, as a person would write it"
    assert default_title("", "x") is None


def test_a_taken_title_is_refused_with_a_free_one_suggested() -> None:
    """Decaid itself allows duplicate titles; two identical entries on the
    tablet are two entries nobody can tell apart."""
    with pytest.raises(ValidationError) as excinfo:
        check_title("tugu - honey", ["Tugu - Honey", "Tugu - Honey (2)"])
    assert "'tugu - honey (3)'" in excinfo.value.problems[0]


def test_a_title_is_normalised_and_bounded() -> None:
    """Decaid took 300 characters without complaint (verified); the limit is
    readability on the tablet, not an API rule."""
    assert check_title("  Tugu   Honey ", []) == "Tugu Honey"
    with pytest.raises(ValidationError):
        check_title("x" * 61, [])
    with pytest.raises(ValidationError):
        check_title("   ", [])
