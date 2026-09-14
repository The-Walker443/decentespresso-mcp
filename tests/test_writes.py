"""Whitelist und Validierung schreibender Zugriffe (SPEC ss18.2, ss20.5)."""

from __future__ import annotations

import pytest

from visualizer_mcp.writes import (
    ALLOWED_FIELDS,
    BLOCKED_FIELDS,
    MAX_NOTE_CHARS,
    ValidationError,
    validate_fields,
)


def problems_of(fields: dict) -> list[str]:
    with pytest.raises(ValidationError) as excinfo:
        validate_fields(fields)
    return excinfo.value.problems


# ------------------------------------------------------------------ Whitelist


def test_the_whitelist_is_exactly_the_verified_set() -> None:
    """Aufgenommen ist nur, was gegen Decaid geschrieben und gelesen wurde.

    Aendert sich diese Liste, ist das eine bewusste Entscheidung nach einer
    Verifikation - kein Versehen.
    """
    assert set(ALLOWED_FIELDS) == {"espressoNotes", "enjoyment"}


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


def test_unverified_fields_are_blocked_with_a_reason() -> None:
    """Dosis und Bezugsgewicht stehen in denselben Annotationen.

    Sie sind aber nicht schreibend geprueft; ein stillschweigend verworfenes
    Feld waere schlimmer als eine klare Abweisung.
    """
    for field in ("actualDoseWeight", "actualYield"):
        assert field in BLOCKED_FIELDS
        assert "verifiziert" in BLOCKED_FIELDS[field]


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
