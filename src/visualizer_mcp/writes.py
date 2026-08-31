"""Whitelist und Validierung fuer schreibende Zugriffe (SPEC ss18).

Bewusst ein eigenes Modul: die Regeln, *was* geschrieben werden darf, sollen
ohne Server, ohne Netz und ohne Datenbank pruefbar sein.

Zwei Befunde aus der API-Verifikation vom 2026-08-01 begruenden die Strenge:

- Visualizer verwirft nicht erlaubte Felder stillschweigend und meldet nur dann
  einen Fehler, wenn *gar nichts* Erlaubtes uebrig bleibt. Ein Tippfehler im
  Feldnamen waere sonst ein stiller Nulleffekt.
- Wertebereiche prueft die API nicht. ``espresso_enjoyment`` liess sich mit 999
  und mit -5 speichern. Was hier durchgeht, landet unkorrigiert im Archiv.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

#: Maximale Laenge von Freitextfeldern.
MAX_NOTE_CHARS = 5_000

#: Erlaubte Felder, gruppiert wie in SPEC ss18.2. Schluessel sind die
#: API-Feldnamen - gegen PATCH /shots/{id} einzeln verifiziert.
ALLOWED_FIELDS: dict[str, str] = {
    # Bohne
    "bean_brand": "Roesterei",
    "bean_type": "Sorte",
    "roast_date": "Roestdatum (ISO, YYYY-MM-DD)",
    "roast_level": "Roestgrad (Freitext)",
    "bean_notes": "Beschreibung der Bohne",
    # Zubereitung
    "grinder_setting": "Muehleneinstellung (Freitext)",
    "bean_weight": "Dosis in g",
    "drink_weight": "Bezugsgewicht in g",
    # Bewertung
    "espresso_enjoyment": "Bewertung 0-100",
    "espresso_notes": "Notiz zum Bezug",
    "private_notes": "private Notiz (nur mit Visualizer-Premium)",
    "drink_tds": "TDS in Prozent",
    "drink_ey": "Extraktionsausbeute in Prozent",
    # Sonstiges
    "barista": "Barista",
}

#: Felder, die ausdruecklich nicht schreibbar sind - fuer eine Fehlermeldung,
#: die den Grund nennt statt nur "unbekannt".
BLOCKED_FIELDS: dict[str, str] = {
    "id": "Die Kennung eines Bezugs ist unveraenderlich.",
    "start_time": "Zeitstempel kommen von der Maschine.",
    "clock": "Zeitstempel kommen von der Maschine.",
    "updated_at": "Setzt Visualizer selbst.",
    "duration": "Telemetrie der Maschine.",
    "profile_title": "Das Profil gehoert zum Bezug und wird versioniert.",
    "profile_url": "Das Profil gehoert zum Bezug und wird versioniert.",
    "data": "Telemetrie der Maschine.",
    "timeframe": "Telemetrie der Maschine.",
    "user_id": "Kontodaten.",
    "user_name": "Kontodaten.",
}

#: Zahlenfelder mit plausiblen Grenzen. Alles ausserhalb ist ein Tippfehler,
#: kein Bezug.
_NUMERIC_RANGES: dict[str, tuple[float, float, str]] = {
    "bean_weight": (5.0, 30.0, "g"),
    "drink_weight": (10.0, 100.0, "g"),
    "drink_tds": (0.0, 30.0, "%"),
    "drink_ey": (0.0, 50.0, "%"),
}

_TEXT_FIELDS = (
    "bean_brand", "bean_type", "roast_level", "bean_notes",
    "grinder_setting", "espresso_notes", "private_notes", "barista",
)


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

    Gibt die Felder in der Form zurueck, in der sie an die API gehen: Zahlen als
    Strings, wie Visualizer sie auch liefert; ``espresso_enjoyment`` als
    Ganzzahl. ``None`` bleibt ``None`` - das ist das Loeschen eines Feldes.
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

    if name == "espresso_enjoyment":
        return _enjoyment(value)
    if name == "roast_date":
        return _roast_date(value)
    if name in _NUMERIC_RANGES:
        return _bounded_number(name, value)
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


def _bounded_number(name: str, value: Any) -> str:
    low, high, unit = _NUMERIC_RANGES[name]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"erwartet wird eine Zahl von {low} bis {high} {unit}")
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        raise ValueError(f"{value!r} ist keine Zahl") from None
    if not low <= number <= high:
        raise ValueError(f"{number} {unit} liegt ausserhalb von {low} bis {high} {unit}")
    # Visualizer liefert diese Felder als Strings und nimmt sie auch so an.
    return f"{number:g}"


def _roast_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("erwartet wird ein Datum als Text im Format YYYY-MM-DD")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{text!r} ist kein ISO-Datum (YYYY-MM-DD). Die DE1-App schreibt "
            "TT.MM.JJJJ; hier wird ISO erwartet und auch so gespeichert."
        ) from None
    today = datetime.now(UTC).date()
    if parsed > today:
        raise ValueError(f"{text} liegt in der Zukunft (heute ist {today.isoformat()})")
    return parsed.isoformat()


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("erwartet wird Text")
    if len(value) > MAX_NOTE_CHARS:
        raise ValueError(f"{len(value)} Zeichen, erlaubt sind hoechstens {MAX_NOTE_CHARS}")
    return value
