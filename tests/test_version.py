"""Version, milestone and build identity (SPEC §19).

Anlass war ein konkreter Fehlgriff: beim M7-Deployment meldete ``status()``
version 0.1.0 and milestone M6 while M7 code was running. Both numbers were
hand-maintained and stale for exactly that reason - indistinguishable from
"the old image is still running".
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest
from fastmcp import Client

from decentespresso_mcp import __version__, milestone
from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.server import build_mcp

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
    assert match, "pyproject.toml has no version field"
    return match.group(1)


# ----------------------------------------------------------------- Kopplung


def test_version_comes_from_the_package_metadata() -> None:
    """One source, not two. The second is the one that gets forgotten."""
    assert __version__ == pyproject_version()
    assert __version__ != "0+unknown", "package not installed?"


def test_no_hardcoded_release_version_in_the_source() -> None:
    """Frueher stand ``__version__ = "0.1.0"`` im Quelltext - sieben
    Meilensteine lang unveraendert.

    The sentinel ``0+unknown`` for the uninstalled case stays allowed; what is
    forbidden is a spelled-out release number.
    """
    hardcoded = re.compile(r'__version__\s*=\s*"\d+\.\d+')
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not hardcoded.search(text), f"{path.name} sets the version by hand"


def test_version_is_readable_in_a_fresh_interpreter() -> None:
    # importlib.metadata reads the installed metadata - that has to work
    # without the source tree on the path too, as in the container.
    result = subprocess.run(
        [sys.executable, "-c", "import decentespresso_mcp; print(decentespresso_mcp.__version__)"],
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
    ("0.7.3", "M7"),      # a patch level does not change the milestone
    ("0.12.0", "M12"),
    ("1.0.0", None),      # from 1.0 on the milestone count is over
    ("2.4.1", None),
    ("0+unbekannt", None),
    ("kaputt", None),
])
def test_milestone_derivation(monkeypatch, version: str, expected: str | None) -> None:
    import decentespresso_mcp

    monkeypatch.setattr(decentespresso_mcp, "__version__", version)
    assert decentespresso_mcp.milestone() == expected


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
    # Started locally there is no commit - better null than a
    # erfundene Angabe.
    monkeypatch.delenv("BUILD_REF", raising=False)

    async with Client(build_mcp(config, db)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["build_ref"] is None


async def test_user_agent_carries_the_real_version(config: Config) -> None:
    # SPEC §4 wants an identifiable User-Agent; with a frozen version it would
    # only nominally be one.
    assert config.user_agent.startswith(f"decentespresso-mcp/{pyproject_version()} ")


# ------------------------------------------------------- Meilenstein gepflegt


def test_version_matches_the_last_milestone_in_the_spec() -> None:
    """Der Bump gehoert zum Meilenstein - dieser Test erinnert daran.

    Finds the highest milestone listed in SPEC §14 and compares it with the
    minor version.
    """
    spec = (ROOT / "SPEC_decentespresso-mcp.md").read_text(encoding="utf-8")
    section = spec.split("## 14.")[1].split("---")[0]
    milestones = [int(m) for m in re.findall(r"\*\*M(\d+)\*\*", section)]
    assert milestones, "SPEC ss14 listet keine Meilensteine mehr"

    assert milestone() == f"M{max(milestones)}", (
        f"pyproject.toml says {__version__} ({milestone()}), SPEC §14 is "
        f"aber bis M{max(milestones)} gebaut. Version pro Meilenstein bumpen."
    )
