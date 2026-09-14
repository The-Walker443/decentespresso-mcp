"""Whitelist und Validierung fuer schreibende Zugriffe (SPEC ss18, ss20.5).

Bewusst ein eigenes Modul: die Regeln, *was* geschrieben werden darf, sollen
ohne Server, ohne Netz und ohne Datenbank pruefbar sein.

Die Felder sind die von Decaids ``annotations``. Aufgenommen ist nur, was
gegen die echte API geschrieben und wieder gelesen wurde - beim
Annotationsumzug in M8 (2/n) waren das ``espressoNotes`` und ``enjoyment``.
Dosis und Bezugsgewicht stehen zwar in denselben Annotationen, sind aber noch
nicht schreibend verifiziert; sie kommen dazu, sobald das nachgeholt ist.
Lieber ein Feld zu wenig als eines, das stillschweigend verworfen wird.

Wertebereiche prueft die API nicht - was hier durchgeht, landet unkorrigiert
im Archiv.
"""

from __future__ import annotations

from typing import Any

#: Maximale Laenge von Freitextfeldern.
MAX_NOTE_CHARS = 5_000

#: Erlaubte Felder in ``annotations``. Schluessel sind Decaids Feldnamen,
#: gegen PUT /api/v1/shots/<id> mit Read-back verifiziert.
ALLOWED_FIELDS: dict[str, str] = {
    "espressoNotes": "Notiz zum Bezug",
    "enjoyment": "Bewertung 0-100 (0 ist eine Bewertung, kein Leerwert)",
}

#: Felder, die ausdruecklich nicht schreibbar sind - fuer eine Fehlermeldung,
#: die den Grund nennt statt nur "unbekannt".
BLOCKED_FIELDS: dict[str, str] = {
    "id": "Die Kennung eines Bezugs ist unveraenderlich.",
    "timestamp": "Zeitstempel kommen von der Maschine.",
    "createdAt": "Setzt Decaid selbst.",
    "updatedAt": "Setzt Decaid selbst.",
    "stopReason": "Telemetrie der Maschine.",
    "measurements": "Telemetrie der Maschine.",
    "workflow": "Bohne, Muehle und Profil gehoeren zum Workflow und werden dort geaendert.",
    "extras": "Herkunftsangaben von Decaid, keine Nutzereingabe.",
    "actualDoseWeight": "Noch nicht schreibend verifiziert - siehe Modul-Docstring.",
    "actualYield": "Noch nicht schreibend verifiziert - siehe Modul-Docstring.",
}

#: Zahlenfelder mit plausiblen Grenzen. Alles ausserhalb ist ein Tippfehler,
#: kein Bezug.
_TEXT_FIELDS = ("espressoNotes",)


class ValidationError(ValueError):
    """Eine oder mehrere Regelverletzungen - gesammelt, nicht einzeln."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def allowed_field_help() -> str:
    """Erlaubte Felder als eine Zeile - fuer Fehlermeldungen."""
    return ", ".join(f"{name} ({label})" for name, label in ALLOWED_FIELDS.items())


def validate_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Prueft die Whitelist und alle Wertebereiche, bevor irgendetwas rausgeht.

    Gibt die Felder in der Form zurueck, in der sie an die API gehen.
    ``None`` bleibt ``None`` - das ist das Loeschen eines Feldes.
    """
    if not isinstance(fields, dict) or not fields:
        raise ValidationError(
            ["fields ist leer - erwartet wird mindestens ein Feld. "
             f"Erlaubt: {allowed_field_help()}"]
        )

    problems: list[str] = []
    payload: dict[str, Any] = {}

    for name, value in fields.items():
        if name in BLOCKED_FIELDS:
            problems.append(f"{name}: nicht aenderbar - {BLOCKED_FIELDS[name]}")
            continue
        if name not in ALLOWED_FIELDS:
            problems.append(
                f"{name}: unbekanntes Feld. Erlaubt sind {allowed_field_help()}"
            )
            continue

        try:
            payload[name] = _coerce(name, value)
        except ValueError as exc:
            problems.append(f"{name}: {exc}")

    if problems:
        raise ValidationError(problems)
    return payload


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None

    if name == "enjoyment":
        return _enjoyment(value)
    if name in _TEXT_FIELDS:
        return _text(value)
    raise ValueError("kein Pruefpfad hinterlegt")   # pragma: no cover - Programmierfehler


def _enjoyment(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("erwartet wird eine Ganzzahl von 0 bis 100")
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{value!r} ist keine Zahl") from None
    if number != int(number):
        raise ValueError(f"{value!r} ist keine Ganzzahl")
    number = int(number)
    if not 0 <= number <= 100:
        raise ValueError(f"{number} liegt ausserhalb von 0 bis 100")
    return number


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("erwartet wird Text")
    if len(value) > MAX_NOTE_CHARS:
        raise ValueError(f"{len(value)} Zeichen, erlaubt sind hoechstens {MAX_NOTE_CHARS}")
    return value
