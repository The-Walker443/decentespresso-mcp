"""Parser fuer DE1-Profile im Tcl-Format (SPEC ss7.1).

Kein Regex-Gefrickel: die Datei *ist* eine Tcl-Liste, also parst sie der
Tcl-Interpreter aus der stdlib. Das ist der vorgesehene Weg; im Container
liefert ``libtk8.6`` die noetige Laufzeitbibliothek (siehe Dockerfile).

Faellt der Interpreter dennoch aus, greift ein eigener Listensplitter
(``_split_tcl_list``) statt den Dienst zu beenden - ein Profilparser ist kein
Grund, den ganzen Server nicht starten zu lassen. Beide Wege werden im Test
gegeneinander geprueft, damit der Ersatz nicht unbemerkt abdriftet.

Die Versionierung haengt am sha256 des **normalisierten** Roh-TCL (SPEC ss5);
``raw_tcl`` wird unveraendert gespeichert. ``semantic_hash`` gruppiert daneben
Versionen, die identisch bruehen.

ÜBERHOLT ab M8: Decaid liefert das Profil als JSON im Workflow mit, die
Versionierung läuft über ``decaid_profile``. Dieses Modul bleibt lesbar für
den Altbestand, wird aber nicht mehr aufgerufen (SPEC §20.4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# Der Import ist bewusst weich. In python:*-slim ist _tkinter zwar einkompiliert,
# die Tk-Laufzeitbibliotheken fehlen aber - ohne libtk8.6 scheitert schon
# 'import tkinter' an "libtk8.6.so: cannot open shared object file". Das
# Dockerfile installiert das Paket; faellt es weg, soll der Server trotzdem
# starten und auf den eigenen Splitter zurueckfallen, statt beim Import zu
# sterben. Genau dieser Fall ist einmal in Produktion aufgetreten.
try:  # pragma: no cover - abhaengig von der Umgebung
    from tkinter import Tcl, TclError

    TCL_INTERPRETER_AVAILABLE = True
except Exception as exc:  # pragma: no cover - abhaengig von der Umgebung
    Tcl = None  # type: ignore[assignment]
    TCL_INTERPRETER_AVAILABLE = False
    TCL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

    class TclError(Exception):  # type: ignore[no-redef]
        """Platzhalter, damit die Fehlerbehandlung ohne tkinter gleich bleibt."""
else:  # pragma: no cover - abhaengig von der Umgebung
    TCL_IMPORT_ERROR = None

#: Steckt in ``parsed_json``. Hochzaehlen, sobald der Parser andere Felder
#: liefert - beim naechsten Start werden alle Profile neu geparst und gehasht.
#: 1 -> 2: legacy_settings fuer Nicht-Advanced-Profile.
PARSER_VERSION = 2

#: settings_profile_type -> Profiltyp. Gegen die JSON-Ausgabe von Visualizer
#: verifiziert: settings_2a -> "pressure", settings_2c -> "advanced".
#: settings_2b -> "flow" folgt der DE1-Konvention, liegt aber (noch) nicht als
#: Echtdatenbeleg vor.
PROFILE_TYPE_BY_SETTINGS = {
    "settings_2a": "pressure",
    "settings_2b": "flow",
    "settings_2c": "advanced",
}

#: exit_type -> Feld, in dem der Schwellwert steht.
EXIT_VALUE_FIELD = {
    "pressure_over": "exit_pressure_over",
    "pressure_under": "exit_pressure_under",
    "flow_over": "exit_flow_over",
    "flow_under": "exit_flow_under",
}

# --- Kopf-Sollwerte von Legacy-Profilen ------------------------------------
#
# Profile ohne advanced_shot beschreiben ihren Verlauf ueber diese Felder. Sie
# gehoeren in parsed_json und in den semantic_hash, sonst waeren zwei Versionen
# mit unterschiedlichem Bruehdruck semantisch gleich.
#
# Getrennt nach Typ, weil die DE1-App auch die jeweils *ungenutzten* Bloecke in
# die Datei schreibt: ein Druckprofil traegt flow_profile_*-Werte, die es nie
# auswertet. Sie mitzuhashen erzeugte dieselben Scheinversionen, die der Hash
# gerade vermeiden soll - dieselbe Ueberlegung wie bei ``exit_if 0``.

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
    """Zerlegt eine Tcl-Liste ohne Interpreter.

    Deckt ab, was in Profildateien vorkommt: blanke Woerter, ``{...}`` mit
    beliebiger Verschachtelung und Zeilenumbruechen, ``"..."`` mit
    Backslash-Ersetzungen. Innerhalb von Klammern findet - wie in Tcl - keine
    Ersetzung statt; der Inhalt kommt woertlich zurueck.

    Ein Test vergleicht das Ergebnis auf allen Fixtures mit
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
    """Vereinheitlicht Zeilenenden und Randweissraum (SPEC ss6.4).

    Zweck ist ein stabiler Hash: dieselbe Profilversion soll denselben Hash
    liefern, egal ob sie mit CRLF oder LF durch die Leitung kam.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def version_hash(raw: str) -> str:
    """sha256 des normalisierten TCL, hex. Das ist die *Identität* einer Version."""
    return hashlib.sha256(normalize_tcl(raw).encode("utf-8")).hexdigest()


def semantic_hash(parsed: dict[str, Any]) -> str | None:
    """sha256 über die brührelevanten Felder von ``parsed_json``.

    Zweck ist ausschliesslich **Gruppierung**: zwei Profilversionen mit gleichem
    ``semantic_hash`` brühen identisch und unterscheiden sich nur kosmetisch
    (Notiztext, Serialisierungsartefakte, leere Zusatzschlüssel). Die Identität
    einer Version bleibt der ``version_hash``.

    Bewusst **nicht** enthalten:

    - ``title``, ``author``, ``notes`` — reine Beschriftung.
    - Der Name eines Schrittes. Ihn umzubenennen ändert am Bezug nichts, genau
      wie beim Profiltitel.
    - Per ``exit_if 0`` deaktivierte Abbruchbedingungen und die Kopf-Sollwerte
      des jeweils *anderen* Profiltyps — ``parse_profile`` liefert beides gar
      nicht erst aus.
    - ``settings_profile_type``: redundant, ``type`` wird daraus abgeleitet.

    Gibt ``None`` zurück, wenn das Profil nicht parsebar war — ohne Parse gibt es
    keine semantische Sicht, und ein Hash über Rohtext wäre nur der
    ``version_hash`` unter anderem Namen.
    """
    if not parsed.get("parse_ok"):
        return None

    canonical = {
        "type": parsed.get("type"),
        "beverage_type": parsed.get("beverage_type"),
        "target_weight_g": parsed.get("target_weight_g"),
        "target_temp_c": parsed.get("target_temp_c"),
        # Kopf-Sollwerte der Legacy-Profile: ohne sie waeren zwei Versionen mit
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

    Wirft nie: bei kaputtem TCL kommt ``parse_ok=False`` zurueck, dazu Titel und
    Notizen aus einem toleranten Zeilenscan, damit der Shot trotzdem nutzbar
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

    # Advanced-Profile fuehren ihr Zielgewicht im _advanced-Feld, Legacy-Profile
    # im Basisfeld - so macht es auch Visualizers eigener Serializer.
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
    """Kopf-Sollwerte fuer Profile ohne ``advanced_shot``.

    ``None`` bei Advanced-Profilen: dort sind diese Felder Altlasten, die die
    Maschine nicht auswertet. Bei unbekanntem Typ kommen beide Bloecke mit -
    lieber eine Scheinversion zu viel als eine echte Aenderung uebersehen.
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

    # Temperaturstufen zaehlen nur, wenn sie eingeschaltet sind - sonst stehen
    # die Werte zwar in der Datei, wirken aber nicht (wie bei exit_if 0).
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
    """``advanced_shot {{...} {...}}`` -> Liste von Schritten.

    Legacy-Profile (settings_2a/2b) haben hier ``{}``; sie bekommen eine leere
    Liste, ihre Sollwerte stehen in ``legacy_settings``. Visualizer
    synthetisiert fuer solche Profile Schritte aus den flow_profile_*-Settings -
    das bilden wir bewusst nicht nach, weil im TCL keine Schritte stehen (siehe
    Testkommentar in test_tcl_profile.py).
    """
    if not advanced_shot.strip():
        return []

    raw_steps = split_list(advanced_shot, prefer_tcl=prefer_tcl)
    parsed = [split_list(step, prefer_tcl=prefer_tcl) for step in raw_steps]

    steps: list[dict[str, Any]] = []
    for index, items in enumerate(parsed):
        if len(items) % 2:
            raise ValueError(f"Schritt {index}: ungerade Anzahl Elemente")
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
    """``exit_if 0`` heisst: die exit_*-Felder stehen zwar da, gelten aber nicht.

    Visualizer laesst den exit-Block in seiner JSON-Ausgabe dann ebenfalls weg.
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
    """Toleranter Zeilenscan, wenn der Listensplit aussteigt (SPEC ss7.1)."""
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
