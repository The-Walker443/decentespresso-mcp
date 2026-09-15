"""Everything in `src/` is written in English (CLAUDE.md, "Language").

This is the grep from commit 2006b1e cast as a test. That commit found thirty
German passages surviving a switch that had been reported as complete four
commits earlier - among them two error messages, a metric warning and clauses
inside two tool docstrings, which travel to Claude in the tool definitions on
every request. They survived because the switch was reviewed by reading diffs,
and a half-translated sentence reads as translated.

So: a translation is verified by search, and the search runs in the suite.
"""

from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize
from collections.abc import Iterator

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "decentespresso_mcp"

#: German function words that have no English homograph. Deliberately not on
#: the list, because they are also English and would fire on correct prose:
#: "die" (the loop must never die), "was", "war", "hat", "man", "also", "am",
#: "im", "den", "in". The list does not need to be exhaustive - it needs to be
#: impossible to write a German sentence without tripping over it.
GERMAN_WORDS = frozenset({
    "aber", "auch", "aus", "bei", "beim", "beliebig", "damit", "dann", "dass",
    "diese", "diesem", "diesen", "dieser", "dieses", "durch", "eine", "einem",
    "einen", "einer", "eines", "erst", "fuer", "für", "geht", "gibt", "hier",
    "ihre", "ihrer", "immer", "jede", "jedem", "jeden", "jeder", "jedes",
    "kann", "kein", "keine", "keinem", "keinen", "keiner", "koennen", "können",
    "mehr", "mit", "muss", "muessen", "müssen", "nach", "nicht", "noch", "nur",
    "oder", "ohne", "schon", "sehr", "seine", "seiner", "sich", "sind", "soll",
    "sollen", "sonst", "ueber", "über", "und", "vom", "von", "vor", "weil",
    "wenn", "werden", "wie", "wird", "wurde", "wurden", "zum", "zur", "zwei",
})

#: The specific words the M8 switch left behind, so this test would have caught
#: that one and not only the shape of it.
GERMAN_WORDS |= frozenset({
    "angefordert", "bauzeit", "berechnet", "einmaliger", "einzelfehler",
    "endpunkte", "enthaelt", "enthält", "fehlende", "frische", "geprueft",
    "inkrementeller", "konfiguration", "kuerzel", "kürzel", "laeuft", "läuft",
    "messwert", "metriken", "netzzugriff", "profilnamen", "rueckgabe",
    "schritte", "sollwerte", "steigung", "ungueltig", "ungueltige", "ungültig",
    "unzuverlaessig", "unzuverlaessiger", "veraltete", "verbindung",
    "vollstaendiger", "vorhanden", "voruebergehend", "voruebergehende",
    "waage", "waechter", "wiederholbar", "zeitreihe",
})

#: Deliberately German, with the reason. Everything else is a finding.
#: User data is never translated (CLAUDE.md) - if a bean name or a note ever
#: needs to appear in `src/`, it belongs here with its justification.
ALLOWED_WORDS: frozenset[str] = frozenset()

#: Umlauts are a strong signal on their own: the repository transliterates
#: (ue/oe/ae) where it writes German at all, so any of these in `src/` is new
#: German rather than old.
UMLAUTS = "ÄÖÜäöüß"

WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+")


def modules() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def texts(path: pathlib.Path) -> Iterator[tuple[str, int, str]]:
    """Every piece of prose in a module: docstrings, literals, comments.

    Docstrings are `ast.Constant` nodes like any other string, so one walk
    covers them, the tool docstrings and `INSTRUCTIONS` alike.
    """
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield "string", node.lineno, node.value
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield "comment", token.start[0], token.string


def german_in(text: str) -> set[str]:
    found = set()
    for word in WORD.findall(text):
        if word.isupper() or word in ALLOWED_WORDS:
            continue  # MIT, UTC, API - acronyms are not prose
        if word.lower() in GERMAN_WORDS:
            found.add(word)
    return found


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_no_german_words(path: pathlib.Path) -> None:
    findings = [
        f"{path.name}:{line} ({kind}) {sorted(hits)} in {text.strip()[:70]!r}"
        for kind, line, text in texts(path)
        if (hits := german_in(text))
    ]
    assert not findings, "German left in src/:\n  " + "\n  ".join(findings)


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_no_umlauts(path: pathlib.Path) -> None:
    findings = [
        f"{path.name}:{line} ({kind}) {text.strip()[:70]!r}"
        for kind, line, text in texts(path)
        if any(ch in UMLAUTS for ch in text)
    ]
    assert not findings, "Umlauts in src/:\n  " + "\n  ".join(findings)


# ------------------------------------------------- the guard guards itself


@pytest.mark.parametrize("sentence", [
    "Ungueltige oder fehlende Konfiguration.",          # config.py, M8 leftover
    "Keine Zeitreihe vorhanden.",                       # a metric warning
    "Dieser Server laeuft ohne Decaid-Verbindung.",     # a tool error
    "beliebig wiederholbar",                            # inside a docstring
    "Berechnet fehlende oder veraltete Metriken.",      # a module docstring
])
def test_the_guard_catches_what_slipped_through_before(sentence: str) -> None:
    """A stopword list that cannot fail is decoration.

    Every sentence here stood in `src/` until 2006b1e and passed a diff review.
    """
    assert german_in(sentence), f"not detected: {sentence!r}"


def test_the_guard_leaves_english_alone() -> None:
    """Including the words deliberately kept off the list.

    "die" is in the codebase ("the loop must never die") and "MIT" appears in
    the attribution - a guard that fires on those would be turned off within a
    week, and a guard that is off catches nothing.
    """
    english = [
        "the loop must never die",
        "informed by the gaggimate-mcp project (MIT)",
        "The war between precision and recall was not the point here",
        "Timestamps are ISO8601 in UTC; elapsed counts from the start",
        "A bed that loses resistance while the pressure is held is opening up.",
    ]
    for text in english:
        assert not german_in(text), f"false positive in {text!r}"
