"""Whitelist und Validierung schreibender Zugriffe (SPEC ss18.2, ss20.5)."""

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
    """Aufgenommen ist nur, was gegen Decaid geschrieben und gelesen wurde.

    Aendert sich eine dieser Listen, ist das eine bewusste Entscheidung nach
    einer Verifikation - kein Versehen.
    """
    assert set(ALLOWED_FIELDS) == {
        "espressoNotes", "enjoyment", "actualDoseWeight", "actualYield",
    }
    assert set(BEAN.allowed) == {
        "name", "roaster", "species", "processing", "notes", "decaf",
    }
    assert set(BATCH.allowed) == {
        "roastDate", "buyDate", "freezeDate", "frozen",
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
    # Die Meldung muss weiterhelfen, nicht nur abweisen.
    assert "espressoNotes" in problems[0]


@pytest.mark.parametrize("field", sorted(BLOCKED_FIELDS))
def test_blocked_fields_name_the_reason(field: str) -> None:
    problems = problems_of({field: "x"})
    assert "nicht aenderbar" in problems[0]
    assert problems[0] != f"{field}: unbekanntes Feld"


def test_telemetry_and_identity_are_blocked() -> None:
    for field in ("id", "timestamp", "measurements", "workflow", "stopReason"):
        assert field in BLOCKED_FIELDS


def test_the_timestamp_block_is_the_only_protection() -> None:
    """Decaid nimmt einen geaenderten Bezugszeitstempel tatsaechlich an.

    Am 2026-09-14 gemessen: PUT mit ``timestamp`` kam mit 200 zurueck und der
    Wert stand danach so da. Anders als bei ``id`` und ``createdAt`` faengt
    die API das nicht ab - diese Zeile ist die Sicherung.
    """
    assert "timestamp" in BLOCKED_FIELDS
    problems = problems_of({"timestamp": "2020-01-01T00:00:00"})
    assert "nicht aenderbar" in problems[0]


def test_a_profile_change_is_refused_with_its_reason() -> None:
    """Der Profilwechsel bleibt in v1 der Maschine vorbehalten."""
    problems = problems_of({"profile": {}}, WORKFLOW)
    assert "an der Maschine" in problems[0]


def test_decaid_has_no_thaw_date_and_says_so() -> None:
    """Am 2026-09-14 geprueft: das Feld gibt es nicht.

    Die Meldung nennt den gangbaren Weg, statt nur abzuweisen.
    """
    problems = problems_of({"unfreezeDate": "2026-09-01"}, BATCH)
    assert "kein Auftaudatum" in problems[0]
    assert "frozen" in problems[0]


def test_empty_fields_is_an_error() -> None:
    assert "leer" in problems_of({})[0]


def test_all_problems_are_collected() -> None:
    problems = problems_of({"unbekannt": 1, "id": "x", "enjoyment": 999})
    assert len(problems) == 3


# ----------------------------------------------------------------- Bewertung


@pytest.mark.parametrize("value", [0, 50, 100, "75", 80.0])
def test_enjoyment_accepts_whole_numbers_in_range(value) -> None:
    assert validate_fields({"enjoyment": value})["enjoyment"] == int(float(value))


def test_zero_is_a_deliberate_rating_here() -> None:
    """Anders als beim Lesen: was der Nutzer ausdruecklich setzt, gilt.

    Die Null-Regel aus ``decaid_mapping`` betrifft den Vorgabewert der
    Import-Aera, nicht eine Eingabe.
    """
    assert validate_fields({"enjoyment": 0})["enjoyment"] == 0


@pytest.mark.parametrize("value", [-1, 101, 999, 50.5, "viel", True])
def test_enjoyment_rejects_everything_else(value) -> None:
    # Die API speichert 999 und -5 anstandslos - hier ist der einzige Schutz.
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
    assert "Text" in problems_of({"espressoNotes": 42})[0]


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
    assert "true oder false" in problems_of({"decaf": "vielleicht"}, BEAN)[0]


# ------------------------------------------------------------ Charge


def test_roast_date_accepts_iso() -> None:
    assert validate_fields({"roastDate": "2026-06-01"}, BATCH) == {
        "roastDate": "2026-06-01"
    }


def test_decaid_date_with_time_is_truncated() -> None:
    """Decaid liefert Datumsangaben mit Uhrzeit zurueck - die kommt nicht mit."""
    assert validate_fields({"roastDate": "2026-09-02T00:00:00.000Z"}, BATCH) == {
        "roastDate": "2026-09-02"
    }


def test_roast_date_in_the_future_is_rejected() -> None:
    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    assert "Zukunft" in problems_of({"roastDate": future}, BATCH)[0]


def test_an_absurdly_old_roast_date_is_a_typo() -> None:
    assert "Tippfehler" in problems_of({"roastDate": "1999-01-01"}, BATCH)[0]


def test_roast_date_error_mentions_the_de1_format() -> None:
    # Die DE1-App schreibt TT.MM.JJJJ - der Hinweis erspart das Raten.
    assert "TT.MM.JJJJ" in problems_of({"roastDate": "01.06.2026"}, BATCH)[0]


def test_the_bean_of_a_batch_is_not_repointed_from_here() -> None:
    assert "in Decaid gesetzt" in problems_of({"beanId": "x"}, BATCH)[0]


# ------------------------------------------------------------ Workflow


def test_grinder_setting_keeps_its_text_form() -> None:
    # "3.30" ist keine Zahl, sondern eine Skalenangabe.
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
    assert "Kennung" in problems_of({"beanBatchId": "die von gestern"}, WORKFLOW)[0]


def test_the_error_names_the_endpoint() -> None:
    """Sonst raet das Modell, an welchem Tool es das Feld versuchen soll."""
    assert "am Workflow" in problems_of({"enjoyment": 50}, WORKFLOW)[0]
    assert "am Bezug" in problems_of({"grinderSetting": "3.3"}, SHOT)[0]
