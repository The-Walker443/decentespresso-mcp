"""Parser for DE1 profiles in Tcl format (SPEC §7.1).

No regex fiddling: the file *is* a Tcl list, so the Tcl interpreter from the
standard library parses it. That is the intended route; inside the container
``libtk8.6`` provides the runtime library needed (see the Dockerfile).

Should the interpreter still be unavailable, a small list splitter of our own
(``_split_tcl_list``) takes over rather than the service shutting down - a
profile parser is no reason to stop the whole server from starting. Both routes
are checked against each other in the tests, so the fallback does not drift
unnoticed.

Versioning hangs on the sha256 of the **normalised** raw TCL (SPEC §5);
``raw_tcl`` is stored unchanged. Alongside it ``semantic_hash`` groups versions
that brew identically.

SUPERSEDED as of M8: Decaid ships the profile as JSON inside the workflow, and
versioning runs through ``decaid_profile``. This module stays readable for the
archived era but is no longer called (SPEC §20.4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# The import is deliberately soft. In python:*-slim _tkinter is compiled in but
# the Tk runtime libraries are missing - without libtk8.6 even 'import tkinter'
# fails with "libtk8.6.so: cannot open shared object file". The Dockerfile
# installs the package; if it ever falls away the server should still start and
# fall back to our own splitter rather than dying at import. That exact case
# happened once in production.
try:  # pragma: no cover - depends on the environment
    from tkinter import Tcl, TclError

    TCL_INTERPRETER_AVAILABLE = True
except Exception as exc:  # pragma: no cover - depends on the environment
    Tcl = None  # type: ignore[assignment]
    TCL_INTERPRETER_AVAILABLE = False
    TCL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

    class TclError(Exception):  # type: ignore[no-redef]
        """Placeholder so error handling stays the same without tkinter."""
else:  # pragma: no cover - depends on the environment
    TCL_IMPORT_ERROR = None

#: Stored inside ``parsed_json``. Bump it as soon as the parser returns
#: different fields - on the next start every profile is reparsed and rehashed.
#: 1 -> 2: legacy_settings for non-advanced profiles.
PARSER_VERSION = 2

#: settings_profile_type -> profile type. Checked against Visualizer's own
#: verifiziert: settings_2a -> "pressure", settings_2c -> "advanced".
#: settings_2b -> "flow" follows the DE1 convention but does not (yet) appear as
#: Echtdatenbeleg vor.
PROFILE_TYPE_BY_SETTINGS = {
    "settings_2a": "pressure",
    "settings_2b": "flow",
    "settings_2c": "advanced",
}

#: exit_type -> the field holding the threshold.
EXIT_VALUE_FIELD = {
    "pressure_over": "exit_pressure_over",
    "pressure_under": "exit_pressure_under",
    "flow_over": "exit_flow_over",
    "flow_under": "exit_flow_under",
}

# --- Headline targets of legacy profiles ------------------------------------
#
# Profiles without advanced_shot describe their curve through these fields.
# They belong in parsed_json and in the semantic_hash, otherwise two versions
# with different brew pressure would be semantically identical.
#
# Split by type, because the DE1 app writes the *unused* blocks into the file
# as well: a pressure profile carries flow_profile_* values it never evaluates.
# Hashing those along would create exactly the phantom versions the hash is
# meant to avoid - the same reasoning as with ``exit_if 0``.

LEGACY_SHARED_FIELDS = {
    "preinfusion_time_s": "preinfusion_time",
    "preinfusion_stop_pressure_bar": "preinfusion_stop_pressure",
    "preinfusion_flow_rate_mls": "preinfusion_flow_rate",
    "preinfusion_guarantee": "preinfusion_guarantee",
    "maximum_flow_mls": "maximum_flow",
    "maximum_pressure_bar": "maximum_pressure",
}

LEGACY_PRESSURE_FIELDS = {
    "target_pressure_bar": "espresso_pressure",
    "hold_time_s": "espresso_hold_time",
    "decline_time_s": "espresso_decline_time",
    "pressure_end_bar": "pressure_end",
}

LEGACY_FLOW_FIELDS = {
    "flow_preinfusion_mls": "flow_profile_preinfusion",
    "flow_preinfusion_time_s": "flow_profile_preinfusion_time",
    "flow_hold_mls": "flow_profile_hold",
    "flow_hold_time_s": "flow_profile_hold_time",
    "flow_decline_mls": "flow_profile_decline",
    "flow_decline_time_s": "flow_profile_decline_time",
    "flow_minimum_pressure_bar": "flow_profile_minimum_pressure",
}

_TCL_LOCK = threading.Lock()


# ---------------------------------------------------------------- Listensplit


def _unescape(char: str) -> str:
    return {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f",
            "v": "\v"}.get(char, char)


def _split_tcl_list(text: str) -> list[str]:
    """Splits a Tcl list without an interpreter.

    Covers what occurs in profile files: bare words, ``{...}`` with arbitrary
    nesting and newlines, ``"..."`` with backslash substitution. Inside braces
    no substitution takes place - as in Tcl - and the content comes back
    verbatim.

    A test compares the result across all fixtures with
    ``tkinter.Tcl().splitlist``.
    """
    items: list[str] = []
    index, size = 0, len(text)
    whitespace = " \t\n\r\f\v"

    while index < size:
        while index < size and text[index] in whitespace:
            index += 1
        if index >= size:
            break

        char = text[index]
        if char == "{":
            depth, index = 1, index + 1
            start = index
            while index < size:
                current = text[index]
                if current == "\\":
                    index += 2
                    continue
                if current == "{":
                    depth += 1
                elif current == "}":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            if depth != 0:
                raise ValueError("unbalancierte geschweifte Klammer")
            items.append(text[start:index])
            index += 1
            if index < size and text[index] not in whitespace:
                raise ValueError("Zeichen direkt hinter schliessender Klammer")

        elif char == '"':
            index += 1
            buffer: list[str] = []
            while index < size and text[index] != '"':
                if text[index] == "\\" and index + 1 < size:
                    buffer.append(_unescape(text[index + 1]))
                    index += 2
                    continue
                buffer.append(text[index])
                index += 1
            if index >= size:
                raise ValueError("unbalanciertes Anfuehrungszeichen")
            items.append("".join(buffer))
            index += 1

        else:
            buffer = []
            while index < size and text[index] not in whitespace:
                if text[index] == "\\" and index + 1 < size:
                    buffer.append(_unescape(text[index + 1]))
                    index += 2
                    continue
                buffer.append(text[index])
                index += 1
            items.append("".join(buffer))

    return items


def split_list(text: str, *, prefer_tcl: bool = True) -> list[str]:
    """Tcl-Liste zerlegen - per Interpreter, wo vorhanden."""
    if prefer_tcl and TCL_INTERPRETER_AVAILABLE:
        with _TCL_LOCK:
            return list(Tcl().splitlist(text))
    return _split_tcl_list(text)


# --------------------------------------------------------------------- Hashes


def normalize_tcl(raw: str) -> str:
    """Normalises line endings and surrounding whitespace (SPEC §6.4).

    The point is a stable hash: the same profile version should produce the
    same hash whether it arrived with CRLF or LF.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def version_hash(raw: str) -> str:
    """sha256 of the normalised TCL, hex. This is the *identity* of a version."""
    return hashlib.sha256(normalize_tcl(raw).encode("utf-8")).hexdigest()


def semantic_hash(parsed: dict[str, Any]) -> str | None:
    """sha256 over the brewing-relevant fields of ``parsed_json``.

    The purpose is **grouping** only: two profile versions with the same
    ``semantic_hash`` brew identically and differ merely cosmetically (note text,
    serialisation artefacts, empty extra keys). The identity of a version remains
    the ``version_hash``.

    Deliberately **not** included:

    - ``title``, ``author``, ``notes`` - labelling, nothing more.
    - The name of a step. Renaming it changes nothing about the shot, exactly as
      with the profile title.
    - Exit conditions disabled via ``exit_if 0`` and the headline targets of the
      *other* profile type - ``parse_profile`` does not emit either in the first
      place.
    - ``settings_profile_type``: redundant, ``type`` is derived from it.

    Returns ``None`` when the profile could not be parsed - without a parse there
    is no semantic view, and a hash over raw text would merely be the
    ``version_hash`` under a different name.
    """
    if not parsed.get("parse_ok"):
        return None

    canonical = {
        "type": parsed.get("type"),
        "beverage_type": parsed.get("beverage_type"),
        "target_weight_g": parsed.get("target_weight_g"),
        "target_temp_c": parsed.get("target_temp_c"),
        # Headline targets of legacy profiles: without them two versions with
        # unterschiedlichem Bruehdruck semantisch gleich.
        "legacy_settings": parsed.get("legacy_settings"),
        "steps": [
            {
                "mode": step.get("mode"),
                "target": step.get("target"),
                "temp_c": step.get("temp_c"),
                "duration_s": step.get("duration_s"),
                "transition": step.get("transition"),
                "exit": step.get("exit"),
            }
            for step in parsed.get("steps") or []
        ],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- Parser


def parse_profile(raw: str, *, prefer_tcl: bool = True) -> dict[str, Any]:
    """TCL -> ``parsed_json`` (SPEC ss7.1).

    Never raises: on broken TCL it returns ``parse_ok=False`` along with title
    and notes from a tolerant line scan, so the shot stays usable
    bleibt.
    """
    try:
        fields = _top_level_fields(raw, prefer_tcl=prefer_tcl)
    except (TclError, ValueError) as exc:
        log.warning("tcl parse failed", extra={"fields": {"error": type(exc).__name__}})
        return _fallback(raw, f"{type(exc).__name__}: {exc}")

    settings_type = fields.get("settings_profile_type", "")
    profile_type = PROFILE_TYPE_BY_SETTINGS.get(settings_type, settings_type or None)

    try:
        steps = _parse_steps(fields.get("advanced_shot", ""), prefer_tcl=prefer_tcl)
    except (TclError, ValueError) as exc:
        log.warning("advanced_shot unparsable", extra={"fields": {"error": type(exc).__name__}})
        return _fallback(raw, f"advanced_shot: {type(exc).__name__}: {exc}")

    # Advanced profiles keep their target weight in the _advanced field, legacy
    # profiles in the base field - Visualizer's own serializer does the same.
    weight_key = (
        "final_desired_shot_weight_advanced"
        if profile_type == "advanced"
        else "final_desired_shot_weight"
    )

    return {
        "parser_version": PARSER_VERSION,
        "title": _text(fields.get("profile_title")) or _text(fields.get("original_profile_title")),
        "author": _text(fields.get("author")),
        "type": profile_type,
        "beverage_type": _text(fields.get("beverage_type")),
        "notes": _text(fields.get("profile_notes")),
        "target_weight_g": _number(fields.get(weight_key)),
        "target_temp_c": _number(fields.get("espresso_temperature")),
        "settings_profile_type": settings_type or None,
        "legacy_settings": _legacy_settings(fields, profile_type),
        "steps": steps,
        "parse_ok": True,
    }


def _legacy_settings(fields: dict[str, str], profile_type: str | None) -> dict[str, Any] | None:
    """Headline targets for profiles without ``advanced_shot``.

    ``None`` for advanced profiles: there these fields are leftovers the machine
    does not evaluate. For an unknown type both blocks come along - better one
    phantom version too many than a real change missed.
    """
    if profile_type == "advanced":
        return None

    mapping = dict(LEGACY_SHARED_FIELDS)
    if profile_type == "pressure":
        mapping |= LEGACY_PRESSURE_FIELDS
    elif profile_type == "flow":
        mapping |= LEGACY_FLOW_FIELDS
    else:
        mapping |= LEGACY_PRESSURE_FIELDS | LEGACY_FLOW_FIELDS

    settings: dict[str, Any] = {}
    for key, source in mapping.items():
        value = _number(fields.get(source))
        if value is not None:
            settings[key] = value

    # Temperature steps only count when they are switched on - otherwise the
    # values do sit in the file but have no effect (as with exit_if 0).
    if _number(fields.get("espresso_temperature_steps_enabled")) == 1:
        steps = [_number(fields.get(f"espresso_temperature_{i}")) for i in range(4)]
        if any(step is not None for step in steps):
            settings["temperature_steps_c"] = steps

    return settings or None


# ------------------------------------------------------------------ intern


def _top_level_fields(raw: str, *, prefer_tcl: bool = True) -> dict[str, str]:
    items = split_list(raw, prefer_tcl=prefer_tcl)
    if len(items) % 2:
        raise ValueError(f"ungerade Anzahl Top-Level-Elemente ({len(items)})")
    return dict(zip(items[::2], items[1::2], strict=True))


def _parse_steps(advanced_shot: str, *, prefer_tcl: bool = True) -> list[dict[str, Any]]:
    """``advanced_shot {{...} {...}}`` -> a list of steps.

    Legacy profiles (settings_2a/2b) have ``{}`` here; they get an empty
    Liste, ihre Sollwerte stehen in ``legacy_settings``. Visualizer
    synthesises steps for such profiles from the flow_profile_* settings - we
    deliberately do not reproduce that, because the TCL holds no steps (see
    Testkommentar in test_tcl_profile.py).
    """
    if not advanced_shot.strip():
        return []

    raw_steps = split_list(advanced_shot, prefer_tcl=prefer_tcl)
    parsed = [split_list(step, prefer_tcl=prefer_tcl) for step in raw_steps]

    steps: list[dict[str, Any]] = []
    for index, items in enumerate(parsed):
        if len(items) % 2:
            raise ValueError(f"step {index}: odd number of elements")
        step = dict(zip(items[::2], items[1::2], strict=True))
        mode = _text(step.get("pump"))
        target = step.get("pressure") if mode == "pressure" else step.get("flow")
        steps.append({
            "name": _text(step.get("name")),
            "mode": mode,
            "target": _number(target),
            "temp_c": _number(step.get("temperature")),
            "duration_s": _number(step.get("seconds")),
            "transition": _text(step.get("transition")),
            "exit": _parse_exit(step),
        })
    return steps


def _parse_exit(step: dict[str, str]) -> dict[str, Any] | None:
    """``exit_if 0`` means: the exit_* fields are present but do not apply.

    Visualizer likewise omits the exit block from its JSON output in that case.
    """
    if _number(step.get("exit_if")) != 1:
        return None
    exit_type = _text(step.get("exit_type"))
    if not exit_type:
        return None
    value_field = EXIT_VALUE_FIELD.get(exit_type)
    return {
        "type": exit_type,
        "value": _number(step.get(value_field)) if value_field else None,
    }


def _fallback(raw: str, error: str) -> dict[str, Any]:
    """Tolerant line scan for when the list split gives up (SPEC §7.1)."""
    title = None
    notes = None
    for line in normalize_tcl(raw).split("\n"):
        key, _, value = line.partition(" ")
        text = value.strip().strip("{}").strip()
        if key == "profile_title" and text and not title:
            title = text
        elif key == "original_profile_title" and text and not title:
            title = text
        elif key == "profile_notes" and text and not notes:
            notes = text
    return {
        "parser_version": PARSER_VERSION,
        "title": title,
        "author": None,
        "type": None,
        "beverage_type": None,
        "notes": notes,
        "target_weight_g": None,
        "target_temp_c": None,
        "settings_profile_type": None,
        "legacy_settings": None,
        "steps": [],
        "parse_ok": False,
        "parse_error": error,
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number
