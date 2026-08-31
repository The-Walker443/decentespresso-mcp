"""Whitelist und Validierung schreibender Zugriffe (SPEC ss18.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def test_the_whitelist_is_exactly_the_agreed_set() -> None:
    # Aendert sich diese Liste, ist das eine bewusste Entscheidung - kein Versehen.
    assert set(ALLOWED_FIELDS) == {
        "bean_brand", "bean_type", "roast_date", "roast_level", "bean_notes",
        "grinder_setting", "bean_weight", "drink_weight",
        "espresso_enjoyment", "espresso_notes", "private_notes",
        "drink_tds", "drink_ey", "barista",
    }


def test_unknown_field_is_rejected_with_the_allowed_list() -> None:
    problems = problems_of({"espresso_enjoyment": 50, "kaffeesorte": "x"})
    assert len(problems) == 1
    assert "kaffeesorte" in problems[0]
    # Die Meldung muss weiterhelfen, nicht nur abweisen.
    assert "bean_type" in problems[0]
    assert "espresso_enjoyment" in problems[0]


@pytest.mark.parametrize("field", sorted(BLOCKED_FIELDS))
def test_blocked_fields_name_the_reason(field: str) -> None:
    problems = problems_of({field: "x"})
    assert "nicht aenderbar" in problems[0]
    assert problems[0] != f"{field}: unbekanntes Feld"


def test_telemetry_and_identity_are_blocked() -> None:
    for field in ("id", "start_time", "duration", "data", "profile_title"):
        assert field in BLOCKED_FIELDS


def test_empty_fields_is_an_error() -> None:
    assert "leer" in problems_of({})[0]


def test_all_problems_are_collected() -> None:
    problems = problems_of({"unbekannt": 1, "id": "x", "espresso_enjoyment": 999})
    assert len(problems) == 3


# ----------------------------------------------------------------- Bewertung


@pytest.mark.parametrize("value", [0, 50, 100, "75", 80.0])
def test_enjoyment_accepts_whole_numbers_in_range(value) -> None:
    assert validate_fields({"espresso_enjoyment": value})["espresso_enjoyment"] == int(
        float(value)
    )


@pytest.mark.parametrize("value", [-1, 101, 999, 50.5, "viel", True])
def test_enjoyment_rejects_everything_else(value) -> None:
    # Die API speichert 999 und -5 anstandslos - hier ist der einzige Schutz.
    problems = problems_of({"espresso_enjoyment": value})
    assert problems[0].startswith("espresso_enjoyment:")


# ------------------------------------------------------------------ Gewichte


@pytest.mark.parametrize(("field", "value"), [
    ("bean_weight", 5.0), ("bean_weight", 18.5), ("bean_weight", 30.0),
    ("drink_weight", 10.0), ("drink_weight", 36.2), ("drink_weight", 100.0),
])
def test_weights_inside_the_range(field: str, value: float) -> None:
    assert validate_fields({field: value})[field] == f"{value:g}"


@pytest.mark.parametrize(("field", "value"), [
    ("bean_weight", 4.9), ("bean_weight", 30.1), ("bean_weight", 0),
    ("drink_weight", 9.9), ("drink_weight", 100.1),
])
def test_weights_outside_the_range(field: str, value: float) -> None:
    assert "ausserhalb" in problems_of({field: value})[0]


def test_comma_decimal_is_accepted() -> None:
    # Deutsche Tastatur, deutscher Nutzer.
    assert validate_fields({"bean_weight": "18,5"})["bean_weight"] == "18.5"


def test_weights_go_out_as_strings() -> None:
    # Visualizer liefert diese Felder als Strings und nimmt sie so an.
    assert validate_fields({"bean_weight": 18})["bean_weight"] == "18"


# ----------------------------------------------------------------- Roestdatum


def test_roast_date_accepts_iso() -> None:
    assert validate_fields({"roast_date": "2026-06-01"})["roast_date"] == "2026-06-01"


def test_roast_date_today_is_fine() -> None:
    today = datetime.now(UTC).date().isoformat()
    assert validate_fields({"roast_date": today})["roast_date"] == today


def test_roast_date_in_the_future_is_rejected() -> None:
    future = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    assert "Zukunft" in problems_of({"roast_date": future})[0]


@pytest.mark.parametrize("value", ["01.06.2026", "2026/06/01", "Juni 2026", "", 20260601])
def test_roast_date_rejects_non_iso(value) -> None:
    problems = problems_of({"roast_date": value})
    assert problems[0].startswith("roast_date:")


def test_roast_date_error_mentions_the_de1_format() -> None:
    # Die DE1-App schreibt TT.MM.JJJJ - der Hinweis erspart das Raten.
    assert "TT.MM.JJJJ" in problems_of({"roast_date": "01.06.2026"})[0]


# --------------------------------------------------------------------- Texte


def test_notes_at_the_limit_pass() -> None:
    text = "x" * MAX_NOTE_CHARS
    assert validate_fields({"espresso_notes": text})["espresso_notes"] == text


def test_notes_beyond_the_limit_fail() -> None:
    problems = problems_of({"espresso_notes": "x" * (MAX_NOTE_CHARS + 1)})
    assert str(MAX_NOTE_CHARS) in problems[0]


def test_text_fields_reject_numbers() -> None:
    assert "Text" in problems_of({"bean_brand": 42})[0]


# ---------------------------------------------------------------- Loeschen


@pytest.mark.parametrize("field", sorted(ALLOWED_FIELDS))
def test_none_clears_any_allowed_field(field: str) -> None:
    assert validate_fields({field: None}) == {field: None}
