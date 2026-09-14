"""Whitelists and validation for write access (SPEC §18, §20.5).

Deliberately its own module: the rules for *what* may be written should be
testable without a server, without a network and without a database.

There are four rulesets - shot, bean, batch, workflow - one per endpoint. A
field is listed only if it was written against the real API and read back
afterwards (verification 2026-08-01 and 2026-09-14, SPEC §20.2). Better one
field too few than one that gets silently discarded.

WHY THE BLOCK LIST CARRIES WEIGHT. The verification on 2026-09-14 showed that
Decaid rejects ``id`` and ``createdAt`` with 400 but **accepts the shot
timestamp**: a ``PUT`` carrying ``timestamp`` came back 200 and the value really
did stand afterwards. For the telemetry fields the whitelist here is not the
second line of defence, it is the only one.

The API does not check value ranges either - whatever passes through here lands
in the archive uncorrected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

#: Maximum length of free-text fields.
MAX_NOTE_CHARS = 5_000

#: Bounds for weights. Anything outside is a typo, not a shot.
_WEIGHT_RANGES: dict[str, tuple[float, float]] = {
    "actualDoseWeight": (5.0, 30.0),
    "actualYield": (5.0, 150.0),
    "targetDoseWeight": (5.0, 30.0),
    "targetYield": (5.0, 150.0),
}

#: How far back a roast date may lie before it is a typo.
_MAX_ROAST_AGE_DAYS = 3 * 365

_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class ValidationError(ValueError):
    """One or more rule violations - collected, not raised one at a time."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class Ruleset:
    """What may be written at one endpoint, and how it is checked."""

    name: str
    #: Field name -> description, used in error messages and docstrings.
    allowed: dict[str, str]
    #: Field name -> reason. For a message that explains instead of just refusing.
    blocked: dict[str, str] = field(default_factory=dict)
    #: Field name -> kind of check. Missing entries default to ``text``.
    kinds: dict[str, str] = field(default_factory=dict)

    def help(self) -> str:
        return ", ".join(f"{n} ({label})" for n, label in self.allowed.items())


# ---------------------------------------------------------------- Shot


SHOT = Ruleset(
    name="shot",
    allowed={
        "espressoNotes": "note on the shot",
        "enjoyment": "rating 0-100 (0 is a rating, not an empty value)",
        "actualDoseWeight": "dose actually used, in g",
        "actualYield": "yield actually pulled, in g",
    },
    kinds={
        "enjoyment": "enjoyment",
        "actualDoseWeight": "weight",
        "actualYield": "weight",
    },
    blocked={
        # Decaid accepts some of these - see the module docstring.
        "id": "The identifier of a shot is immutable.",
        "timestamp": "Timestamps come from the machine.",
        "createdAt": "Decaid sets this itself.",
        "updatedAt": "Decaid sets this itself.",
        "stopReason": "Machine telemetry.",
        "measurements": "Machine telemetry.",
        "workflow": "Bean, grinder and profile are changed through set_workflow.",
        "extras": "Provenance data written by Decaid, not user input.",
    },
)


# ---------------------------------------------------------------- Bean


BEAN = Ruleset(
    name="bean",
    allowed={
        "name": "name of the bean",
        "roaster": "roastery",
        "species": "species (arabica, for instance)",
        "processing": "processing method (washed, for instance)",
        "notes": "description",
        "decaf": "decaffeinated (true/false)",
    },
    kinds={"decaf": "bool"},
    blocked={
        "id": "The identifier of a bean is immutable.",
        "createdAt": "Decaid sets this itself.",
        "updatedAt": "Decaid sets this itself.",
        "archived": "Archiving happens in Decaid, not from here.",
    },
)


# ---------------------------------------------------------------- Batch


BATCH = Ruleset(
    name="batch",
    allowed={
        "roastDate": "roast date (ISO, YYYY-MM-DD)",
        "buyDate": "purchase date (ISO, YYYY-MM-DD)",
        "freezeDate": "date it went into the freezer (ISO, YYYY-MM-DD)",
        "frozen": "currently frozen (true/false)",
    },
    kinds={
        "roastDate": "date",
        "buyDate": "date",
        "freezeDate": "date",
        "frozen": "bool",
    },
    blocked={
        "id": "The identifier of a batch is immutable.",
        "beanId": "The link to the bean is set in Decaid.",
        "createdAt": "Decaid sets this itself.",
        "updatedAt": "Decaid sets this itself.",
        "archived": "Archiving happens in Decaid, not from here.",
        # Checked against the API on 2026-09-14: the field does not exist.
        "unfreezeDate": (
            "Decaid keeps no thaw date. To thaw, set frozen to false; bean age "
            "then continues counting from that moment."
        ),
    },
)


# ---------------------------------------------------------------- Workflow


WORKFLOW = Ruleset(
    name="workflow",
    allowed={
        "grinderSetting": 'grind setting (free text, e.g. "3.30")',
        "grinderModel": "grinder",
        "targetDoseWeight": "target dose in g",
        "targetYield": "target yield in g",
        "beanBatchId": "identifier of the batch being pulled from",
    },
    kinds={
        "targetDoseWeight": "weight",
        "targetYield": "weight",
        "beanBatchId": "id",
    },
    blocked={
        "profile": (
            "Changing the profile changes brewing behaviour fundamentally and "
            "belongs at the machine, not in a conversation."
        ),
        "id": "The identifier of the workflow is immutable.",
        "steamSettings": "Steam is not part of a shot.",
        "rinseData": "Rinsing is not part of a shot.",
        "hotWaterData": "Hot water is not part of a shot.",
    },
)


RULESETS = {r.name: r for r in (SHOT, BEAN, BATCH, WORKFLOW)}

#: Backwards-compatible names - until M8 there was only the shot ruleset.
ALLOWED_FIELDS = SHOT.allowed
BLOCKED_FIELDS = SHOT.blocked


def allowed_field_help(ruleset: Ruleset = SHOT) -> str:
    """The permitted fields as a single line, for error messages."""
    return ruleset.help()


def validate_fields(
    fields: dict[str, Any], ruleset: Ruleset = SHOT
) -> dict[str, Any]:
    """Check the whitelist and every value range before anything goes out.

    Returns the fields in the shape the API expects. ``None`` stays ``None`` -
    that is how a field gets cleared.
    """
    if not isinstance(fields, dict) or not fields:
        raise ValidationError(
            [f"fields is empty - at least one field is expected. "
             f"Allowed on the {ruleset.name}: {ruleset.help()}"]
        )

    problems: list[str] = []
    payload: dict[str, Any] = {}

    for name, value in fields.items():
        if name in ruleset.blocked:
            problems.append(f"{name}: not writable - {ruleset.blocked[name]}")
            continue
        if name not in ruleset.allowed:
            problems.append(
                f"{name}: unknown field on the {ruleset.name}. "
                f"Allowed are {ruleset.help()}"
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
        raise ValueError("a whole number from 0 to 100 is expected")
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a number") from None
    if number != int(number):
        raise ValueError(f"{value!r} is not a whole number")
    number = int(number)
    if not 0 <= number <= 100:
        raise ValueError(f"{number} is outside the range 0 to 100")
    return number


def _weight(name: str, value: Any) -> float:
    low, high = _WEIGHT_RANGES[name]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"a number from {low} to {high} g is expected")
    try:
        # German keyboard, German user.
        number = float(str(value).replace(",", "."))
    except ValueError:
        raise ValueError(f"{value!r} is not a number") from None
    if not low <= number <= high:
        raise ValueError(f"{number} g is outside the range {low} to {high} g")
    # Decaid takes and returns these fields as numbers, not strings.
    return round(number, 2)


def _iso_date(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("a date as text in the format YYYY-MM-DD is expected")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(
            f"{text!r} is not an ISO date (YYYY-MM-DD). The DE1 app writes "
            "DD.MM.YYYY; ISO is expected here and stored that way."
        ) from None
    today = datetime.now(UTC).date()
    if parsed > today:
        raise ValueError(f"{text} lies in the future (today is {today.isoformat()})")
    if name == "roastDate" and (today - parsed).days > _MAX_ROAST_AGE_DAYS:
        raise ValueError(
            f"{text} lies more than {_MAX_ROAST_AGE_DAYS // 365} years back - "
            "that is a typo rather than a roast."
        )
    return parsed.isoformat()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError("true or false is expected")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _UUID.match(value.strip()):
        raise ValueError("an identifier is expected, as list_beans returns them")
    return value.strip()


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("text is expected")
    if len(value) > MAX_NOTE_CHARS:
        raise ValueError(f"{len(value)} characters, at most {MAX_NOTE_CHARS} allowed")
    return value
