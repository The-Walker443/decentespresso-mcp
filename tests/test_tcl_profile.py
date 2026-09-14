"""TCL-Parser gegen echte Profile, mit Visualizers format=json als Gegenprobe."""

from __future__ import annotations

import json
import pathlib
import threading

import pytest

from decentespresso_mcp.tcl_profile import (
    PROFILE_TYPE_BY_SETTINGS,
    TCL_INTERPRETER_AVAILABLE,
    normalize_tcl,
    parse_profile,
    semantic_hash,
    split_list,
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


# ---------------------------------------------------------- legacy_settings


def test_legacy_settings_capture_the_head_values(legacy: dict) -> None:
    """Ohne Schritte stecken die Sollwerte eines Legacy-Profils hier."""
    settings = legacy["legacy_settings"]
    assert settings["target_pressure_bar"] == 8.9
    assert settings["hold_time_s"] == 4
    assert settings["decline_time_s"] == 35
    assert settings["pressure_end_bar"] == 6
    assert settings["preinfusion_time_s"] == 20
    assert settings["preinfusion_stop_pressure_bar"] == 4
    assert settings["preinfusion_flow_rate_mls"] == 8
    # espresso_temperature_steps_enabled ist 1 -> die Stufen zaehlen.
    assert settings["temperature_steps_c"] == [88, 86, 86, 86]


def test_pressure_profile_omits_the_unused_flow_block(legacy: dict) -> None:
    # Die DE1-App schreibt flow_profile_* auch in ein Druckprofil, wertet sie
    # dort aber nicht aus - mitgehasht ergaeben sie Scheinversionen.
    assert "flow_profile_hold" in tcl("profile_recent.tcl")
    assert not any(k.startswith("flow_") for k in legacy["legacy_settings"])


def test_advanced_profile_has_no_legacy_settings(advanced: dict) -> None:
    # Dort sind dieselben Felder Altlasten ohne Wirkung.
    assert "espresso_pressure 6.0" in tcl("profile_reference.tcl")
    assert advanced["legacy_settings"] is None


def test_disabled_temperature_steps_are_omitted(legacy: dict) -> None:
    raw = tcl("profile_recent.tcl").replace(
        "espresso_temperature_steps_enabled 1", "espresso_temperature_steps_enabled 0"
    )
    assert "temperature_steps_c" not in parse_profile(raw)["legacy_settings"]


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


# ------------------------------------------------------------- semantic_hash
#
# Die drei Fixtures sind echte Versionen des Decent-Default-Profils aus dem
# Archiv. a und a_cosmetic unterscheiden sich nur durch zwei leere
# Zusatzschluessel und ein doppeltes Leerzeichen in den Notizen; b aendert
# Druck (8.6 -> 8.9) und Temperatur (90 -> 88).


def sem(name: str) -> str | None:
    return semantic_hash(parse_profile(tcl(name)))


def test_cosmetic_change_keeps_the_semantic_hash() -> None:
    base = "profile_default_a.tcl"
    cosmetic = "profile_default_a_cosmetic.tcl"

    assert version_hash(tcl(base)) != version_hash(tcl(cosmetic)), (
        "die Versionen sind unterschiedliche Dateien - version_hash muss das zeigen"
    )
    assert sem(base) == sem(cosmetic)


def test_brewing_change_changes_the_semantic_hash() -> None:
    assert sem("profile_default_a.tcl") != sem("profile_default_b.tcl")


def test_pressure_only_change_changes_the_semantic_hash() -> None:
    """Der Fall, der den legacy_settings-Block noetig gemacht hat.

    Die Fixture unterscheidet sich von profile_default_a.tcl in genau einer
    Zeile: espresso_pressure 8.6 -> 8.9, Temperatur unveraendert. Ohne die
    Kopf-Sollwerte im Hash waeren beide Versionen semantisch gleich gewesen -
    obwohl mit einem Bar mehr gebrueht wird.
    """
    base = tcl("profile_default_a.tcl")
    louder = tcl("profile_default_a_pressure_only.tcl")

    diff = [(a, b) for a, b in zip(base.splitlines(), louder.splitlines(), strict=True)
            if a != b]
    assert diff == [("espresso_pressure 8.6", "espresso_pressure 8.9")]

    assert parse_profile(base)["target_temp_c"] == parse_profile(louder)["target_temp_c"]
    assert sem("profile_default_a.tcl") != sem("profile_default_a_pressure_only.tcl")


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("espresso_hold_time 4", "espresso_hold_time 9"),
        ("espresso_decline_time 35", "espresso_decline_time 20"),
        ("pressure_end 6.0", "pressure_end 4.0"),
        ("preinfusion_time 20", "preinfusion_time 12"),
        ("preinfusion_stop_pressure 4.0", "preinfusion_stop_pressure 3.0"),
        ("espresso_temperature_1 86.0", "espresso_temperature_1 84.0"),
    ],
)
def test_every_legacy_head_value_moves_the_hash(original: str, replacement: str) -> None:
    raw = tcl("profile_recent.tcl")
    assert original in raw
    changed = raw.replace(original, replacement)
    assert semantic_hash(parse_profile(raw)) != semantic_hash(parse_profile(changed))


def test_unused_flow_block_does_not_move_the_hash() -> None:
    # Gegenprobe: an einem Druckprofil ist flow_profile_hold wirkungslos.
    raw = tcl("profile_recent.tcl")
    changed = raw.replace("flow_profile_hold 2", "flow_profile_hold 7")
    assert version_hash(raw) != version_hash(changed)
    assert semantic_hash(parse_profile(raw)) == semantic_hash(parse_profile(changed))


def test_semantic_hash_ignores_title_author_and_notes() -> None:
    raw = tcl("profile_reference.tcl")
    edited = (
        # Geklammert, sonst waeren es zwei Tcl-Listenelemente.
        raw.replace("author Damian", "author {Jemand Anders}")
        .replace("profile_title {D-Flow / default}", "profile_title {Anderer Name}")
        .replace("A simple to use profiling system", "Voellig andere Notiz")
    )
    assert version_hash(raw) != version_hash(edited)
    assert semantic_hash(parse_profile(raw)) == semantic_hash(parse_profile(edited))


def test_semantic_hash_ignores_step_names() -> None:
    # Einen Schritt umzubenennen aendert am Bezug nichts - sonst erzeugte die
    # Umbenennung genau die Scheinversion, die der Hash vermeiden soll.
    raw = tcl("profile_reference.tcl")
    renamed = raw.replace("name Pouring", "name Ausschenken")
    assert semantic_hash(parse_profile(raw)) == semantic_hash(parse_profile(renamed))


def test_semantic_hash_reacts_to_step_target_and_temperature() -> None:
    raw = tcl("profile_reference.tcl")
    base = semantic_hash(parse_profile(raw))
    assert semantic_hash(parse_profile(raw.replace("flow 1.7", "flow 2.4"))) != base
    assert semantic_hash(parse_profile(raw.replace("temperature 88", "temperature 92"))) != base
    assert semantic_hash(parse_profile(raw.replace("seconds 25.00", "seconds 30"))) != base


def test_semantic_hash_reacts_to_an_active_exit_condition() -> None:
    raw = tcl("profile_reference.tcl")
    base = semantic_hash(parse_profile(raw))
    # exit_pressure_over 2.1 gehoert zum Schritt mit exit_if 1 -> zaehlt.
    assert semantic_hash(parse_profile(raw.replace("exit_pressure_over 2.1",
                                                   "exit_pressure_over 3.3"))) != base


def test_semantic_hash_ignores_disabled_exit_fields() -> None:
    # exit_pressure_over 3.0 steht im Schritt "Infusing" mit exit_if 0 - der
    # Wert ist tot und darf keine neue Version vortaeuschen.
    raw = tcl("profile_reference.tcl")
    dead = raw.replace("exit_pressure_over 3.0", "exit_pressure_over 9.9")
    assert version_hash(raw) != version_hash(dead)
    assert semantic_hash(parse_profile(raw)) == semantic_hash(parse_profile(dead))


def test_semantic_hash_is_none_for_unparsable_profiles() -> None:
    assert semantic_hash(parse_profile("profile_title {Kaputt\n")) is None


def test_semantic_hash_differs_between_unrelated_profiles() -> None:
    assert sem("profile_reference.tcl") != sem("profile_recent.tcl")


# ------------------------------------------------- Parser ohne Tcl-Interpreter
#
# Der Interpreter ist der vorgesehene Weg; libtk8.6 im Dockerfile sorgt dafuer,
# dass er auch im Container laeuft. Faellt er trotzdem aus, darf der Server
# nicht sterben - der eigene Listensplitter muss dann dasselbe liefern wie der
# Interpreter. Hier auf allen Fixtures gegengeprueft.


def test_module_import_survives_a_broken_tkinter(monkeypatch) -> None:
    """Der Produktionsfall: _tkinter da, libtk8.6.so nicht.

    Damals starb der Container beim Import. Das Modul muss stattdessen laden,
    ``TCL_INTERPRETER_AVAILABLE`` auf False setzen, den Grund festhalten und
    weiterparsen.
    """
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def refuse_tkinter(name, *args, **kwargs):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("libtk8.6.so: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    module = sys.modules["decentespresso_mcp.tcl_profile"]
    monkeypatch.setattr(builtins, "__import__", refuse_tkinter)
    monkeypatch.delitem(sys.modules, "tkinter", raising=False)
    try:
        crippled = importlib.reload(module)

        assert crippled.TCL_INTERPRETER_AVAILABLE is False
        assert "libtk8.6.so" in crippled.TCL_IMPORT_ERROR

        parsed = crippled.parse_profile(tcl("profile_recent.tcl"))
        assert parsed["parse_ok"] is True
        assert parsed["title"] == "Default"
        assert parsed["legacy_settings"]["target_pressure_bar"] == 8.9
        assert crippled.semantic_hash(parsed)
    finally:
        # Zustand wiederherstellen, sonst sehen alle folgenden Tests das
        # verkrueppelte Modul.
        monkeypatch.undo()
        importlib.reload(module)

    assert module.TCL_INTERPRETER_AVAILABLE is TCL_INTERPRETER_AVAILABLE

ALL_TCL_FIXTURES = sorted(p.name for p in FIXTURES.glob("*.tcl"))


def test_there_are_fixtures_to_compare() -> None:
    assert len(ALL_TCL_FIXTURES) >= 5


@pytest.mark.skipif(
    not TCL_INTERPRETER_AVAILABLE, reason="ohne tkinter gibt es nichts zu vergleichen"
)
@pytest.mark.parametrize("name", ALL_TCL_FIXTURES)
def test_both_backends_split_identically(name: str) -> None:
    raw = tcl(name)
    assert split_list(raw, prefer_tcl=True) == split_list(raw, prefer_tcl=False)


@pytest.mark.skipif(
    not TCL_INTERPRETER_AVAILABLE, reason="ohne tkinter gibt es nichts zu vergleichen"
)
@pytest.mark.parametrize("name", ALL_TCL_FIXTURES)
def test_both_backends_parse_identically(name: str) -> None:
    raw = tcl(name)
    with_tcl = parse_profile(raw, prefer_tcl=True)
    without = parse_profile(raw, prefer_tcl=False)
    assert with_tcl == without
    assert semantic_hash(with_tcl) == semantic_hash(without)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a b c", ["a", "b", "c"]),
        ("  a   b  ", ["a", "b"]),
        ("a {b c} d", ["a", "b c", "d"]),
        ("a {b {c d} e}", ["a", "b {c d} e"]),          # Verschachtelung bleibt roh
        ("a {}", ["a", ""]),                             # leere Klammer
        ('a "b c" d', ["a", "b c", "d"]),                # Anfuehrungszeichen
        (r'a "b\nc"', ["a", "b\nc"]),                    # Ersetzung in Quotes
        (r"a {b\nc}", ["a", r"b\nc"]),                   # keine Ersetzung in Klammern
        ("a\nb\tc", ["a", "b", "c"]),                    # Zeilenumbruch trennt
        ("", []),
    ],
)
def test_fallback_split_matches_tcl_semantics(raw: str, expected: list[str]) -> None:
    assert split_list(raw, prefer_tcl=False) == expected
    if TCL_INTERPRETER_AVAILABLE:
        assert split_list(raw, prefer_tcl=True) == expected


#: Kaputte Listen. Tcl wirft TclError, der Fallback ValueError - parse_profile
#: faengt beides. Entscheidend ist, dass beide *dieselben* Eingaben ablehnen.
BROKEN_LISTS = ["{unbalanciert", 'a "offen', "a {b}c"]

#: Sieht kaputt aus, ist es aber nicht: eine schliessende Klammer im blanken
#: Wort ist in Tcl ein normales Zeichen.
ODD_BUT_VALID = {
    "a {b} c}": ["a", "b", "c}"],
    "a }b": ["a", "}b"],
    "a b{c": ["a", "b{c"],
}


@pytest.mark.parametrize("raw", BROKEN_LISTS)
def test_fallback_rejects_broken_lists(raw: str) -> None:
    with pytest.raises(ValueError):
        split_list(raw, prefer_tcl=False)


@pytest.mark.skipif(not TCL_INTERPRETER_AVAILABLE, reason="braucht tkinter")
@pytest.mark.parametrize("raw", BROKEN_LISTS)
def test_tcl_rejects_the_same_inputs(raw: str) -> None:
    from decentespresso_mcp.tcl_profile import TclError

    with pytest.raises((TclError, ValueError)):
        split_list(raw, prefer_tcl=True)


@pytest.mark.parametrize(("raw", "expected"), ODD_BUT_VALID.items())
def test_odd_but_valid_lists_are_accepted(raw: str, expected: list[str]) -> None:
    assert split_list(raw, prefer_tcl=False) == expected
    if TCL_INTERPRETER_AVAILABLE:
        assert split_list(raw, prefer_tcl=True) == expected


def test_parse_without_tcl_survives_broken_input() -> None:
    parsed = parse_profile("profile_title {Kaputt\n", prefer_tcl=False)
    assert parsed["parse_ok"] is False
    assert parsed["title"] == "Kaputt"


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
