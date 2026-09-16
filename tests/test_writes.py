"""Whitelist and validation of write access (SPEC §11.2)."""

from __future__ import annotations

import pytest

from decentespresso_mcp.writes import (
    ALLOWED_FIELDS,
    BATCH,
    BEAN,
    BLOCKED_FIELDS,
    MAX_NOTE_CHARS,
    RULESETS,
    SHOT,
    WORKFLOW,
    ValidationError,
    validate_fields,
)


def problems_of(fields: dict, ruleset=SHOT) -> list[str]:
    with pytest.raises(ValidationError) as excinfo:
        validate_fields(fields, ruleset)
    return excinfo.value.problems


# ------------------------------------------------------------------ Whitelist


def test_the_whitelist_is_exactly_the_verified_set() -> None:
    """Only what was written against Decaid and read back is listed here.

    A change to any of these lists is a deliberate decision following a
    verification - never an oversight.
    """
    assert set(ALLOWED_FIELDS) == {
        "espressoNotes", "enjoyment", "actualDoseWeight", "actualYield",
    }
    assert set(BEAN.allowed) == {
        "name", "roaster", "species", "processing", "notes", "decaf",
        # Origin, written against the live API and read back on 2026-09-16
        # (bean 1fb10258, probe values restored with an explicit null, which
        # clears a field).
        "country", "region", "producer", "variety", "altitude",
    }
    assert set(BATCH.allowed) == {
        "roastDate", "buyDate", "openDate", "bestBeforeDate",
        "freezeDate", "unfreezeDate", "frozen",
    }
    assert set(WORKFLOW.allowed) == {
        "grinderSetting", "grinderModel", "targetDoseWeight", "targetYield",
        "beanBatchId",
    }


@pytest.mark.parametrize("ruleset", list(RULESETS.values()), ids=lambda r: r.name)
def test_no_field_is_allowed_and_blocked_at_once(ruleset) -> None:
    assert not (set(ruleset.allowed) & set(ruleset.blocked))


@pytest.mark.parametrize("ruleset", list(RULESETS.values()), ids=lambda r: r.name)
def test_every_ruleset_protects_its_identity(ruleset) -> None:
    assert "id" in ruleset.blocked or "id" not in ruleset.allowed


def test_unknown_field_is_rejected_with_the_allowed_list() -> None:
    problems = problems_of({"enjoyment": 50, "kaffeesorte": "x"})
    assert len(problems) == 1
    assert "kaffeesorte" in problems[0]
    # The message must help, not merely refuse.
    assert "espressoNotes" in problems[0]


@pytest.mark.parametrize("field", sorted(BLOCKED_FIELDS))
def test_blocked_fields_name_the_reason(field: str) -> None:
    problems = problems_of({field: "x"})
    assert "not writable" in problems[0]
    assert problems[0] != f"{field}: unknown field"


def test_telemetry_and_identity_are_blocked() -> None:
    for field in ("id", "timestamp", "measurements", "workflow", "stopReason"):
        assert field in BLOCKED_FIELDS


def test_the_timestamp_block_is_the_only_protection() -> None:
    """Decaid really does accept a changed shot timestamp.

    Measured on 2026-09-14: a PUT carrying ``timestamp`` came back 200 and the
    value stood afterwards. Unlike ``id`` and ``createdAt`` the API does not
    catch this - this entry is the safeguard.
    """
    assert "timestamp" in BLOCKED_FIELDS
    problems = problems_of({"timestamp": "2020-01-01T00:00:00"})
    assert "not writable" in problems[0]


def test_a_profile_change_is_refused_with_its_reason() -> None:
    """In v1 a profile change stays reserved for the machine."""
    problems = problems_of({"profile": {}}, WORKFLOW)
    assert "at the machine" in problems[0]


def test_the_thaw_date_is_writable_after_all() -> None:
    """It exists, and an earlier verification said otherwise.

    The claim "Decaid keeps no thaw date" came from a response where the field
    was simply unset - absent from JSON is not absent from the API. Written
    against the live instance and read back on 2026-09-16 (batch 0a640616,
    restored with null): `unfreezeDate` is taken and stands. The consequence is
    not cosmetic - with a thaw date the bean age is exact instead of an upper
    bound.
    """
    assert validate_fields({"unfreezeDate": "2026-09-01"}, BATCH) == {
        "unfreezeDate": "2026-09-01"
    }
    assert "unfreezeDate" not in BATCH.blocked


def test_empty_fields_is_an_error() -> None:
    assert "is empty" in problems_of({})[0]


def test_all_problems_are_collected() -> None:
    problems = problems_of({"unknown_field": 1, "id": "x", "enjoyment": 999})
    assert len(problems) == 3


# ----------------------------------------------------------------- Bewertung


@pytest.mark.parametrize("value", [0, 50, 100, "75", 80.0])
def test_enjoyment_accepts_whole_numbers_in_range(value) -> None:
    assert validate_fields({"enjoyment": value})["enjoyment"] == int(float(value))


def test_zero_is_a_deliberate_rating_here() -> None:
    """Unlike on read: what the user explicitly sets counts.

    The zero rule in ``decaid_mapping`` concerns the import-era default, not an
    input.
    """
    assert validate_fields({"enjoyment": 0})["enjoyment"] == 0


@pytest.mark.parametrize("value", [-1, 101, 999, 50.5, "viel", True])
def test_enjoyment_rejects_everything_else(value) -> None:
    # The API stores 999 and -5 without complaint - this is the only guard.
    problems = problems_of({"enjoyment": value})
    assert problems[0].startswith("enjoyment:")


# --------------------------------------------------------------------- Texte


def test_notes_at_the_limit_pass() -> None:
    text = "x" * MAX_NOTE_CHARS
    assert validate_fields({"espressoNotes": text})["espressoNotes"] == text


def test_notes_beyond_the_limit_fail() -> None:
    problems = problems_of({"espressoNotes": "x" * (MAX_NOTE_CHARS + 1)})
    assert str(MAX_NOTE_CHARS) in problems[0]


def test_text_fields_reject_numbers() -> None:
    assert "text is expected" in problems_of({"espressoNotes": 42})[0]


# ---------------------------------------------------------------- Loeschen


@pytest.mark.parametrize("field", sorted(ALLOWED_FIELDS))
def test_none_clears_any_allowed_field(field: str) -> None:
    assert validate_fields({field: None}) == {field: None}


# ------------------------------------------------------------ Bohne


def test_bean_text_fields_pass() -> None:
    assert validate_fields({"roaster": "Bogatz", "processing": "washed"}, BEAN) == {
        "roaster": "Bogatz", "processing": "washed",
    }


@pytest.mark.parametrize("value", [True, False, "true", "false"])
def test_decaf_accepts_booleans(value) -> None:
    assert isinstance(validate_fields({"decaf": value}, BEAN)["decaf"], bool)


def test_decaf_rejects_anything_else() -> None:
    assert "true or false" in problems_of({"decaf": "maybe"}, BEAN)[0]


# ------------------------------------------------------------ Charge


def test_roast_date_accepts_iso() -> None:
    assert validate_fields({"roastDate": "2026-06-01"}, BATCH) == {
        "roastDate": "2026-06-01"
    }


def test_decaid_date_with_time_is_truncated() -> None:
    """Decaid returns dates with a time attached - that part does not come along."""
    assert validate_fields({"roastDate": "2026-09-02T00:00:00.000Z"}, BATCH) == {
        "roastDate": "2026-09-02"
    }


def test_roast_date_in_the_future_is_rejected() -> None:
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    assert "in the future" in problems_of({"roastDate": future}, BATCH)[0]


def test_an_absurdly_old_roast_date_is_a_typo() -> None:
    assert "typo" in problems_of({"roastDate": "1999-01-01"}, BATCH)[0]


def test_roast_date_error_mentions_the_de1_format() -> None:
    # The DE1 app writes DD.MM.YYYY - the hint saves guessing.
    assert "DD.MM.YYYY" in problems_of({"roastDate": "01.06.2026"}, BATCH)[0]


def test_the_bean_of_a_batch_is_not_repointed_from_here() -> None:
    assert "set in Decaid" in problems_of({"beanId": "x"}, BATCH)[0]


# ------------------------------------------------------------ Workflow


def test_grinder_setting_keeps_its_text_form() -> None:
    # "3.30" is not a number, it is a position on a scale.
    assert validate_fields({"grinderSetting": "3.30"}, WORKFLOW) == {
        "grinderSetting": "3.30"
    }


@pytest.mark.parametrize(("value", "expected"), [(18, 18.0), ("18,5", 18.5), (20.0, 20.0)])
def test_target_dose_accepts_numbers_and_commas(value, expected) -> None:
    assert validate_fields({"targetDoseWeight": value}, WORKFLOW)[
        "targetDoseWeight"
    ] == expected


@pytest.mark.parametrize("value", [4.9, 30.1, "viel", True])
def test_target_dose_outside_the_range_is_rejected(value) -> None:
    assert problems_of({"targetDoseWeight": value}, WORKFLOW)[0].startswith(
        "targetDoseWeight:"
    )


def test_batch_reference_must_look_like_an_id() -> None:
    good = "0a640616-b680-4a10-8062-c1f9fd892cfe"
    assert validate_fields({"beanBatchId": good}, WORKFLOW) == {"beanBatchId": good}
    assert "identifier" in problems_of({"beanBatchId": "the one from yesterday"}, WORKFLOW)[0]


def test_the_error_names_the_endpoint() -> None:
    """Otherwise the model guesses which tool the field belongs to."""
    assert "on the workflow" in problems_of({"enjoyment": 50}, WORKFLOW)[0]
    assert "on the shot" in problems_of({"grinderSetting": "3.3"}, SHOT)[0]
