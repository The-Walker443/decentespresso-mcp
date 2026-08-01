"""TCL-Parser gegen echte Profile, mit Visualizers format=json als Gegenprobe."""

from __future__ import annotations

import json
import pathlib
import threading

import pytest

from visualizer_mcp.tcl_profile import (
    PROFILE_TYPE_BY_SETTINGS,
    normalize_tcl,
    parse_profile,
    version_hash,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def tcl(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def vis_json(name: str) -> dict:
    """Visualizers eigene Interpretation desselben Profils."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def advanced() -> dict:
    """D-Flow / default - settings_2c, drei echte Schritte."""
    return parse_profile(tcl("profile_reference.tcl"))


@pytest.fixture
def legacy() -> dict:
    """Decent Default - settings_2a, advanced_shot ist leer."""
    return parse_profile(tcl("profile_recent.tcl"))


# --------------------------------------------------------------- Grundfaelle


def test_advanced_profile_header(advanced: dict) -> None:
    assert advanced["parse_ok"] is True
    assert advanced["title"] == "D-Flow / default"
    assert advanced["author"] == "Damian"
    assert advanced["type"] == "advanced"
    assert advanced["settings_profile_type"] == "settings_2c"
    assert advanced["beverage_type"] == "espresso"
    assert advanced["target_temp_c"] == 88
    # Advanced-Profile fuehren das Ziel im _advanced-Feld (36), nicht in
    # final_desired_shot_weight (50).
    assert advanced["target_weight_g"] == 36


def test_advanced_steps(advanced: dict) -> None:
    steps = advanced["steps"]
    assert [s["name"] for s in steps] == ["Filling", "Infusing", "Pouring"]

    filling, infusing, pouring = steps
    assert filling["mode"] == "pressure"
    assert filling["target"] == 3.0          # pressure, weil pump=pressure
    assert filling["temp_c"] == 88
    assert filling["duration_s"] == 25
    assert filling["transition"] == "fast"
    assert filling["exit"] == {"type": "pressure_over", "value": 2.1}

    # exit_if 0 -> die exit_*-Felder stehen zwar im TCL, gelten aber nicht.
    assert infusing["exit"] is None
    assert pouring["exit"] is None
    assert pouring["mode"] == "flow"
    assert pouring["target"] == 1.7          # flow, weil pump=flow


def test_legacy_profile_has_no_steps(legacy: dict) -> None:
    assert legacy["parse_ok"] is True
    assert legacy["title"] == "Default"
    assert legacy["type"] == "pressure"
    assert legacy["settings_profile_type"] == "settings_2a"
    assert legacy["steps"] == []
    # Legacy-Profile fuehren das Ziel im Basisfeld (40.0), nicht im _advanced (36).
    assert legacy["target_weight_g"] == 40.0


def test_notes_survive_multiline_braces(advanced: dict) -> None:
    assert advanced["notes"].startswith("A simple to use profiling system")
    assert "Downloaded from Visualizer" in advanced["notes"]
    assert "\n" in advanced["notes"]


def test_profile_type_mapping_is_complete() -> None:
    assert PROFILE_TYPE_BY_SETTINGS == {
        "settings_2a": "pressure",
        "settings_2b": "flow",
        "settings_2c": "advanced",
    }


# ------------------------------------------------------- Hash / Normalisierung


def test_hash_is_stable_across_line_endings() -> None:
    raw = tcl("profile_reference.tcl")
    assert version_hash(raw) == version_hash(raw.replace("\n", "\r\n"))
    assert version_hash(raw) == version_hash(raw + "\n\n\n")
    assert version_hash(raw) == version_hash("\n" + raw)


def test_hash_changes_when_content_changes() -> None:
    raw = tcl("profile_reference.tcl")
    changed = raw.replace("espresso_pressure 6.0", "espresso_pressure 7.0")
    assert version_hash(raw) != version_hash(changed)


def test_different_profiles_have_different_hashes() -> None:
    assert version_hash(tcl("profile_reference.tcl")) != version_hash(tcl("profile_recent.tcl"))


def test_normalize_strips_trailing_whitespace() -> None:
    assert normalize_tcl("a 1   \r\nb 2\t\n\n\n") == "a 1\nb 2\n"
    assert normalize_tcl("") == ""
    assert normalize_tcl("   \n  \n") == ""


# ------------------------------------------------------------------ Robustheit


def test_broken_tcl_does_not_raise() -> None:
    # Unbalancierte Klammer - der Interpreter steigt aus.
    broken = "profile_title {Kaputt\nauthor Niemand\n"
    parsed = parse_profile(broken)
    assert parsed["parse_ok"] is False
    assert "parse_error" in parsed
    assert parsed["steps"] == []


def test_fallback_recovers_title_and_notes() -> None:
    broken = (
        "advanced_shot {{unbalanced\n"
        "profile_title {Mein Profil}\n"
        "profile_notes {Eine Notiz}\n"
    )
    parsed = parse_profile(broken)
    assert parsed["parse_ok"] is False
    assert parsed["title"] == "Mein Profil"
    assert parsed["notes"] == "Eine Notiz"


def test_odd_number_of_top_level_items_is_caught() -> None:
    parsed = parse_profile("profile_title Test\ndangling\n")
    assert parsed["parse_ok"] is False


def test_empty_input_does_not_raise() -> None:
    parsed = parse_profile("")
    assert parsed["parse_ok"] is True     # leere Liste ist gueltiges Tcl
    assert parsed["steps"] == []
    assert parsed["title"] is None


def test_parsing_works_from_a_worker_thread() -> None:
    # Der Sync-Worker laeuft async; tkinter reagiert empfindlich auf Threads.
    results: list[dict] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(parse_profile(tcl("profile_reference.tcl")))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert all(r["title"] == "D-Flow / default" for r in results)


# ----------------------------------------------- Gegenprobe gegen format=json


def test_crosscheck_advanced_against_visualizer(advanced: dict) -> None:
    """Eigener Parser vs. Visualizers format=json - Advanced-Profil.

    Der eigene Parser ist die Referenz (er liest das TCL, an dem auch der
    Versionshash haengt). Diese Gegenprobe faellt aus, sobald eine der beiden
    Seiten etwas anderes versteht.
    """
    ref = vis_json("profile_reference.json")

    assert advanced["title"] == ref["title"]
    assert advanced["author"] == ref["author"]
    assert advanced["type"] == ref["type"]
    assert advanced["beverage_type"] == ref["beverage_type"]
    assert advanced["settings_profile_type"] == ref["legacy_profile_type"]
    assert advanced["target_weight_g"] == float(ref["target_weight"])

    assert len(advanced["steps"]) == len(ref["steps"])
    for mine, theirs in zip(advanced["steps"], ref["steps"], strict=True):
        assert mine["name"] == theirs["name"]
        assert mine["mode"] == theirs["pump"]
        assert mine["transition"] == theirs["transition"]
        assert mine["temp_c"] == float(theirs["temperature"])
        assert mine["duration_s"] == float(theirs["seconds"])

        expected_target = theirs["pressure"] if theirs["pump"] == "pressure" else theirs["flow"]
        assert mine["target"] == float(expected_target)

        if mine["exit"] is None:
            assert "exit" not in theirs, "Visualizer sieht hier ein exit, wir nicht"
        else:
            # Visualizer teilt exit_type in type+condition, wir behalten
            # "pressure_over" am Stueck (SPEC ss7.1).
            assert mine["exit"]["type"] == f"{theirs['exit']['type']}_{theirs['exit']['condition']}"
            assert mine["exit"]["value"] == float(theirs["exit"]["value"])


def test_crosscheck_legacy_against_visualizer(legacy: dict) -> None:
    """Legacy-Profil: Kopfdaten muessen stimmen, Schritte weichen bewusst ab."""
    ref = vis_json("profile_recent.json")

    assert legacy["title"] == ref["title"]
    assert legacy["author"] == ref["author"]
    assert legacy["type"] == ref["type"]
    assert legacy["beverage_type"] == ref["beverage_type"]
    assert legacy["settings_profile_type"] == ref["legacy_profile_type"]
    assert legacy["target_weight_g"] == float(ref["target_weight"])

    # DOKUMENTIERTE ABWEICHUNG 1
    # Im TCL steht "advanced_shot {}" - es gibt keine Schritte. Visualizer
    # synthetisiert fuer Legacy-Profile sechs Schritte aus den
    # flow_profile_*- und preinfusion_*-Settings ("preinfusion temp boost",
    # "preinfusion", "forced rise without limit", "rise and hold", ...).
    # Das ist eine Rekonstruktion, keine Information aus der Datei; wir bilden
    # sie nicht nach. Die Sollwerte stehen als Kopffelder in parsed_json.
    assert legacy["steps"] == []
    assert len(ref["steps"]) == 6
    assert ref["steps"][0]["name"] == "preinfusion temp boost"


def test_documented_difference_in_notes_whitespace(advanced: dict) -> None:
    """DOKUMENTIERTE ABWEICHUNG 2: Notizen-Weissraum.

    Visualizers JSON enthaelt an einer Stelle die zwei Zeichen \\n gefolgt von
    acht Leerzeichen, wo im TCL ein einzelnes Leerzeichen steht. Die Ursache
    liegt in Visualizers Serializer, nicht bei uns - unsere Notiz ist die des
    TCL, und der Versionshash haengt ohnehin am TCL.
    """
    ref = vis_json("profile_reference.json")
    assert "\\n        " in ref["notes"]
    assert "\\n        " not in advanced["notes"]
    assert "Brakel D-Flow" in advanced["notes"]

    # Abgesehen davon ist der Text identisch.
    assert advanced["notes"] == ref["notes"].replace("\\n        ", " ")
