"""Version, Ausbaustufe und Build-Kennung (SPEC ss19).

Anlass war ein konkreter Fehlgriff: beim M7-Deployment meldete ``status()``
Version 0.1.0 und Meilenstein M6, obwohl M7-Code lief. Beide Zahlen waren von
Hand gepflegt, und genau deshalb veraltet - unterscheidbar von "das alte Image
laeuft noch" war das nicht.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest
from fastmcp import Client

from visualizer_mcp import __version__, milestone
from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.server import build_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path: pathlib.Path):
    database = Database(tmp_path / "version.db")
    database.migrate()
    yield database
    database.close()


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml hat kein version-Feld"
    return match.group(1)


# ----------------------------------------------------------------- Kopplung


def test_version_comes_from_the_package_metadata() -> None:
    """Eine Quelle, nicht zwei. Die zweite ist die, die man vergisst."""
    assert __version__ == pyproject_version()
    assert __version__ != "0+unbekannt", "Paket nicht installiert?"


def test_no_hardcoded_release_version_in_the_source() -> None:
    """Frueher stand ``__version__ = "0.1.0"`` im Quelltext - sieben
    Meilensteine lang unveraendert.

    Der Sentinel ``0+unbekannt`` fuer den nicht installierten Fall bleibt
    erlaubt; verboten ist eine ausgeschriebene Release-Nummer.
    """
    hardcoded = re.compile(r'__version__\s*=\s*"\d+\.\d+')
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not hardcoded.search(text), f"{path.name} setzt die Version von Hand"


def test_version_is_readable_in_a_fresh_interpreter() -> None:
    # importlib.metadata liest die installierten Metadaten - das muss auch
    # ohne den Quellbaum im Pfad klappen, so wie im Container.
    result = subprocess.run(
        [sys.executable, "-c", "import visualizer_mcp; print(visualizer_mcp.__version__)"],
        capture_output=True, text=True, cwd=str(ROOT.parent), check=True,
    )
    assert result.stdout.strip() == __version__


# --------------------------------------------------------------- Meilenstein


def test_milestone_is_derived_from_the_minor_version() -> None:
    minor = int(__version__.split(".")[1])
    assert milestone() == f"M{minor}"


@pytest.mark.parametrize(("version", "expected"), [
    ("0.0.1", "M0"),
    ("0.7.0", "M7"),
    ("0.7.3", "M7"),      # Patchstand aendert die Ausbaustufe nicht
    ("0.12.0", "M12"),
    ("1.0.0", None),      # ab 1.0 ist die Meilensteinzaehlung vorbei
    ("2.4.1", None),
    ("0+unbekannt", None),
    ("kaputt", None),
])
def test_milestone_derivation(monkeypatch, version: str, expected: str | None) -> None:
    import visualizer_mcp

    monkeypatch.setattr(visualizer_mcp, "__version__", version)
    assert visualizer_mcp.milestone() == expected


# -------------------------------------------------------------------- status


async def test_status_reports_version_milestone_and_build(
    config: Config, db: Database, monkeypatch
) -> None:
    monkeypatch.setenv("BUILD_REF", "abc1234def5678")

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["version"] == pyproject_version()
    assert payload["milestone"] == milestone()
    assert payload["build_ref"] == "abc1234def5678"


async def test_build_ref_is_null_outside_a_built_image(
    config: Config, db: Database, monkeypatch
) -> None:
    # Lokal gestartet gibt es keinen Commit-Stand - dann lieber null als eine
    # erfundene Angabe.
    monkeypatch.delenv("BUILD_REF", raising=False)

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["build_ref"] is None


async def test_user_agent_carries_the_real_version(config: Config) -> None:
    # SPEC ss4 will einen identifizierbaren User-Agent; mit einer eingefrorenen
    # Version waere er das nur noch nominell.
    assert config.user_agent.startswith(f"visualizer-mcp/{pyproject_version()} ")


# ------------------------------------------------------- Meilenstein gepflegt


def test_version_matches_the_last_milestone_in_the_spec() -> None:
    """Der Bump gehoert zum Meilenstein - dieser Test erinnert daran.

    Sucht die hoechste in SPEC ss14 gelistete Ausbaustufe und vergleicht sie
    mit der Nebenversion.
    """
    spec = (ROOT / "SPEC_visualizer-mcp.md").read_text(encoding="utf-8")
    section = spec.split("## 14.")[1].split("---")[0]
    milestones = [int(m) for m in re.findall(r"\*\*M(\d+)\*\*", section)]
    assert milestones, "SPEC ss14 listet keine Meilensteine mehr"

    assert milestone() == f"M{max(milestones)}", (
        f"pyproject.toml steht auf {__version__} ({milestone()}), SPEC ss14 ist "
        f"aber bis M{max(milestones)} gebaut. Version pro Meilenstein bumpen."
    )
