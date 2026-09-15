"""Version and build identity.

One source for the version and one for the commit. The occasion was a
deployment that reported a version and a milestone which were both maintained
by hand and both stale - whether the old image was running or only the fields
lagged behind could not be told apart from the response.

The milestone counter is gone with the development narrative it belonged to.
What is left is the mechanism that actually caught that bug: the version comes
from the package metadata, and a test keeps the specification honest about it.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest
from fastmcp import Client

from decentespresso_mcp import __version__
from decentespresso_mcp.config import Config
from decentespresso_mcp.db import Database
from decentespresso_mcp.server import build_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "SPEC_decentespresso-mcp.md"


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    assert match, "pyproject.toml has no version field"
    return match.group(1)


def test_version_comes_from_the_package_metadata() -> None:
    """One source, not two. The second is the one that gets forgotten."""
    assert __version__ == pyproject_version()
    assert __version__ != "0+unknown", "package not installed?"


def test_no_module_hardcodes_a_release_number() -> None:
    """The sentinel for the uninstalled case stays allowed.

    What is forbidden is a spelled-out release number anywhere in the source.
    """
    hardcoded = re.compile(r'__version__\s*=\s*["\']\d+\.\d+')
    for path in (ROOT / "src" / "decentespresso_mcp").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not hardcoded.search(text), f"{path.name} sets the version by hand"


def test_version_is_readable_without_the_source_tree() -> None:
    # importlib.metadata reads the installed metadata - that has to work
    # without the source tree on the path too, as in the container.
    result = subprocess.run(
        [sys.executable, "-c",
         "import decentespresso_mcp as m; print(m.__version__)"],
        cwd=ROOT.parent, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == __version__


def test_the_spec_states_the_version_it_describes() -> None:
    """The one convention left, and the only one that can go stale silently.

    A specification that names a version it no longer describes is worse than
    one that names none, because it invites trusting the wrong document.
    """
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(r"\*\*Applies to version ([0-9][^.\s]*\.[^.\s]*\.[^.\s]*)\.\*\*",
                      text)
    assert match, "the spec does not state which version it applies to"
    assert match.group(1) == __version__, (
        f"the spec says {match.group(1)}, the package is {__version__}"
    )


async def test_status_reports_version_and_build(
    config: Config, archive: Database, monkeypatch
) -> None:
    monkeypatch.setenv("BUILD_REF", "0123456789abcdef")
    async with Client(build_mcp(config, archive)) as client:
        payload = (await client.call_tool("status", {})).data

    assert payload["version"] == __version__
    assert payload["build_ref"] == "0123456789abcdef"
    assert "milestone" not in payload, "the milestone counter is gone"


async def test_build_ref_is_null_outside_an_image(
    config: Config, archive: Database, monkeypatch
) -> None:
    # Started locally there is no commit - better null than a made-up value.
    monkeypatch.delenv("BUILD_REF", raising=False)
    async with Client(build_mcp(config, archive)) as client:
        payload = (await client.call_tool("status", {})).data
    assert payload["build_ref"] is None


def test_the_user_agent_carries_the_real_version(config: Config) -> None:
    # An identifiable User-Agent is only identifiable while it tracks the
    # version it actually runs.
    assert config.user_agent.startswith(f"decentespresso-mcp/{pyproject_version()} ")


@pytest.mark.parametrize("path", ["README.md", "SPEC_decentespresso-mcp.md"])
def test_the_docs_do_not_name_a_stale_version(path: str) -> None:
    """A version number in prose is a second place to forget.

    Only the one line the test above checks is allowed to carry it.
    """
    text = (ROOT / path).read_text(encoding="utf-8")
    stale = [
        line for line in text.splitlines()
        if re.search(r"\b0\.\d+\.\d+\b", line)
        and "Applies to version" not in line   # the one place it belongs
        and "Decaid" not in line               # their version, not ours
        and "0.0.0.0" not in line              # a bind address
    ]
    assert not stale, f"{path} names a version in prose: {stale[:3]}"


def test_metrics_version_is_an_integer_that_only_grows() -> None:
    """The cache key. A definition change that forgets it serves stale numbers."""
    from decentespresso_mcp.metrics import METRICS_VERSION

    assert isinstance(METRICS_VERSION, int)
    assert METRICS_VERSION >= 3


def test_verified_decaid_version_is_pinned() -> None:
    """status() warns when the tablet reports something else."""
    from decentespresso_mcp.decaid_client import VERIFIED_DECAID_VERSION

    assert re.fullmatch(r"\d+\.\d+\.\d+", VERIFIED_DECAID_VERSION)


def test_the_spec_and_the_readme_are_not_empty() -> None:
    for path in (SPEC, ROOT / "README.md"):
        assert len(path.read_text(encoding="utf-8")) > 2000, path.name


def test_status_payload_is_json_serialisable(config: Config, archive: Database) -> None:
    from decentespresso_mcp.server import _status_payload

    json.dumps(_status_payload(config, archive), default=str)
