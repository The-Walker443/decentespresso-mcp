"""Whitelists und Validierung fuer schreibende Zugriffe (SPEC ss18, ss20.5).

Bewusst ein eigenes Modul: die Regeln, *was* geschrieben werden darf, sollen
ohne Server, ohne Netz und ohne Datenbank pruefbar sein.

Es gibt vier Regelwerke - Bezug, Bohne, Charge, Workflow -, je eines pro
Endpunkt. Aufgenommen ist ausschliesslich, was gegen die echte API geschrieben
und anschliessend wieder gelesen wurde (Verifikation 2026-08-01 und
2026-09-14, SPEC ss20.2). Lieber ein Feld zu wenig als eines, das
stillschweigend verworfen wird.

WARUM DIE BLOCKLISTE TRAEGT. Bei der Verifikation am 2026-09-14 hat sich
gezeigt, dass Decaid ``id`` und ``createdAt`` zwar mit 400 abweist, **den
Bezugszeitstempel aber annimmt**: ein ``PUT`` mit ``timestamp`` kam mit 200
zurueck und der Wert stand danach wirklich so da. Fuer die Telemetriefelder ist
die Whitelist hier also nicht die zweite Sicherung, sondern die einzige.

Wertebereiche prueft die API ebenfalls nicht - was hier durchgeht, landet
unkorrigiert im Archiv.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

#: Maximale Laenge von Freitextfeldern.
MAX_NOTE_CHARS = 5_000

#: Grenzen fuer Gewichte. Alles ausserhalb ist ein Tippfehler, kein Bezug.
_WEIGHT_RANGES: dict[str, tuple[float, float]] = {
    "actualDoseWeight": (5.0, 30.0),
    "actualYield": (5.0, 150.0),
    "targetDoseWeight": (5.0, 30.0),
    "targetYield": (5.0, 150.0),
}

#: Wie weit ein Roestdatum zurueckliegen darf, bevor es ein Tippfehler ist.
_MAX_ROAST_AGE_DAYS = 3 * 365

_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class ValidationError(ValueError):
    """Eine oder mehrere Regelverletzungen - gesammelt, nicht einzeln."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class Ruleset:
    """Was an einem Endpunkt geschrieben werden darf, und wie geprueft wird."""

    name: str
    #: Feldname -> Beschreibung fuer Fehlermeldungen und Docstrings.
    allowed: dict[str, str]
    #: Feldname -> Grund. Fuer eine Meldung, die erklaert statt nur abzuweisen.
    blocked: dict[str, str] = field(default_factory=dict)
    #: Feldname -> Pruefart. Fehlt ein Eintrag, gilt ``text``.
    kinds: dict[str, str] = field(default_factory=dict)

    def help(self) -> str:
        return ", ".join(f"{n} ({label})" for n, label in self.allowed.items())


# --------------------------------------------------------------- Bezug


SHOT = Ruleset(
    name="Bezug",
    allowed={
        "espressoNotes": "Notiz zum Bezug",
        "enjoyment": "Bewertung 0-100 (0 ist eine Bewertung, kein Leerwert)",
        "actualDoseWeight": "tatsaechliche Dosis in g",
        "actualYield": "tatsaechliches Bezugsgewicht in g",
    },
    kinds={
        "enjoyment": "enjoyment",
        "actualDoseWeight": "weight",
        "actualYield": "weight",
    },
    blocked={
        # Diese drei nimmt Decaid teilweise an - siehe Modul-Docstring.
        "id": "Die Kennung eines Bezugs ist unveraenderlich.",
        "timestamp": "Zeitstempel kommen von der Maschine.",
        "createdAt": "Setzt Decaid selbst.",
        "updatedAt": "Setzt Decaid selbst.",
        "stopReason": "Telemetrie der Maschine.",
        "measurements": "Telemetrie der Maschine.",
        "workflow": "Bohne, Muehle und Profil aendert set_workflow.",
        "extras": "Herkunftsangaben von Decaid, keine Nutzereingabe.",
    },
)


# --------------------------------------------------------------- Bohne


BEAN = Ruleset(
    name="Bohne",
    allowed={
        "name": "Bezeichnung der Bohne",
        "roaster": "Roesterei",
        "species": "Art (z. B. arabica)",
        "processing": "Aufbereitung (z. B. washed)",
        "notes": "Beschreibung",
        "decaf": "entkoffeiniert (true/false)",
    },
    kinds={"decaf": "bool"},
    blocked={
        "id": "Die Kennung einer Bohne ist unveraenderlich.",
        "createdAt": "Setzt Decaid selbst.",
        "updatedAt": "Setzt Decaid selbst.",
        "archived": "Archivieren geschieht in Decaid, nicht von hier aus.",
    },
)


# --------------------------------------------------------------- Charge


BATCH = Ruleset(
    name="Charge",
    allowed={
        "roastDate": "Roestdatum (ISO, YYYY-MM-DD)",
        "buyDate": "Kaufdatum (ISO, YYYY-MM-DD)",
        "freezeDate": "Einfrierdatum (ISO, YYYY-MM-DD)",
        "frozen": "aktuell eingefroren (true/false)",
    },
    kinds={
        "roastDate": "date",
        "buyDate": "date",
        "freezeDate": "date",
        "frozen": "bool",
    },
    blocked={
        "id": "Die Kennung einer Charge ist unveraenderlich.",
        "beanId": "Die Zuordnung zur Bohne wird in Decaid gesetzt.",
        "createdAt": "Setzt Decaid selbst.",
        "updatedAt": "Setzt Decaid selbst.",
        "archived": "Archivieren geschieht in Decaid, nicht von hier aus.",
        # Am 2026-09-14 gegen die API geprueft: das Feld gibt es nicht.
        "unfreezeDate": (
            "Decaid fuehrt kein Auftaudatum. Zum Auftauen frozen auf false "
            "setzen; das Bohnenalter rechnet dann ab diesem Zeitpunkt weiter."
        ),
    },
)


# --------------------------------------------------------------- Workflow


WORKFLOW = Ruleset(
    name="Workflow",
    allowed={
        "grinderSetting": "Muehleneinstellung (Freitext, z. B. \"3.30\")",
        "grinderModel": "Muehle",
        "targetDoseWeight": "Zieldosis in g",
        "targetYield": "Zielgewicht in g",
        "beanBatchId": "Kennung der Charge, aus der bezogen wird",
    },
    kinds={
        "targetDoseWeight": "weight",
        "targetYield": "weight",
        "beanBatchId": "id",
    },
    blocked={
        "profile": (
            "Ein Profilwechsel aendert das Bruehverhalten grundlegend und "
            "geschieht an der Maschine, nicht aus einem Gespraech heraus."
        ),
        "id": "Die Kennung des Workflows ist unveraenderlich.",
        "steamSettings": "Dampf gehoert nicht zum Bezug.",
        "rinseData": "Spuelen gehoert nicht zum Bezug.",
        "hotWaterData": "Heisswasser gehoert nicht zum Bezug.",
    },
)


RULESETS = {r.name: r for r in (SHOT, BEAN, BATCH, WORKFLOW)}

#: Rueckwaertskompatible Namen - bis M8 gab es nur das Bezugs-Regelwerk.
ALLOWED_FIELDS = SHOT.allowed
BLOCKED_FIELDS = SHOT.blocked


def allowed_field_help(ruleset: Ruleset = SHOT) -> str:
    """Erlaubte Felder als eine Zeile - fuer Fehlermeldungen."""
    return ruleset.help()


def validate_fields(
    fields: dict[str, Any], ruleset: Ruleset = SHOT
) -> dict[str, Any]:
    """Prueft Whitelist und Wertebereiche, bevor irgendetwas rausgeht.

    Gibt die Felder in der Form zurueck, in der sie an die API gehen.
    ``None`` bleibt ``None`` - das ist das Loeschen eines Feldes.
    """
    if not isinstance(fields, dict) or not fields:
        raise ValidationError(
            [f"fields ist leer - erwartet wird mindestens ein Feld. "
             f"Erlaubt am {ruleset.name}: {ruleset.help()}"]
        )

    problems: list[str] = []
    payload: dict[str, Any] = {}

    for name, value in fields.items():
        if name in ruleset.blocked:
            problems.append(f"{name}: nicht aenderbar - {ruleset.blocked[name]}")
            continue
        if name not in ruleset.allowed:
            problems.append(
                f"{name}: unbekanntes Feld am {ruleset.name}. "
                f"Erlaubt sind {ruleset.help()}"
            )
            continue

        try:
            payload[name] = _coerce(ruleset, name, value)
        except ValueError as exc:
            problems.append(f"{name}: {exc}")

    if problems:
        raise ValidationError(problems)
    return payload


def _coerce(ruleset: Ruleset, name: str, value: Any) -> Any:
    if value is None:
        return None

    kind = ruleset.kinds.get(name, "text")
    if kind == "enjoyment":
        return _enjoyment(value)
    if kind == "weight":
        return _weight(name, value)
    if kind == "date":
        return _iso_date(name, value)
    if kind == "bool":
        return _bool(value)
    if kind == "id":
        return _identifier(value)
    return _text(value)


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


def _weight(name: str, value: Any) -> float:
    low, high = _WEIGHT_RANGES[name]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"erwartet wird eine Zahl von {low} bis {high} g")
    try:
        # Deutsche Tastatur, deutscher Nutzer.
        number = float(str(value).replace(",", "."))
    except ValueError:
        raise ValueError(f"{value!r} ist keine Zahl") from None
    if not low <= number <= high:
        raise ValueError(f"{number} g liegt ausserhalb von {low} bis {high} g")
    # Decaid nimmt und liefert diese Felder als Zahl, nicht als String.
    return round(number, 2)


def _iso_date(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("erwartet wird ein Datum als Text im Format YYYY-MM-DD")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(
            f"{text!r} ist kein ISO-Datum (YYYY-MM-DD). Die DE1-App schreibt "
            "TT.MM.JJJJ; hier wird ISO erwartet und auch so gespeichert."
        ) from None
    today = datetime.now(UTC).date()
    if parsed > today:
        raise ValueError(f"{text} liegt in der Zukunft (heute ist {today.isoformat()})")
    if name == "roastDate" and (today - parsed).days > _MAX_ROAST_AGE_DAYS:
        raise ValueError(
            f"{text} liegt mehr als {_MAX_ROAST_AGE_DAYS // 365} Jahre zurueck - "
            "das ist eher ein Tippfehler als eine Roestung."
        )
    return parsed.isoformat()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError("erwartet wird true oder false")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _UUID.match(value.strip()):
        raise ValueError("erwartet wird eine Kennung, wie list_beans sie liefert")
    return value.strip()


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("erwartet wird Text")
    if len(value) > MAX_NOTE_CHARS:
        raise ValueError(f"{len(value)} Zeichen, erlaubt sind hoechstens {MAX_NOTE_CHARS}")
    return value
