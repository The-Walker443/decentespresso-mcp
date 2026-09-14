"""decentespresso-mcp: MCP server for espresso shot analysis."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

#: Taken from the package metadata, and thus from pyproject.toml - a second,
#: hand-maintained number in the code is exactly the one forgotten at release
#: time. That happened once: status() reported "M6" while M7 code was running.
try:
    __version__ = _package_version("decentespresso-mcp")
except PackageNotFoundError:  # pragma: no cover - only without an install
    # Started straight from the source tree (no pip install). Deliberately no
    # guess from pyproject.toml: an invented number would be worse than a
    # visible gap.
    __version__ = "0+unknown"


def milestone() -> str | None:
    """Milestone derived from the minor version (SPEC §14): 0.7.x -> "M7".

    Only while the major version is 0 - after that the milestone count is
    over and a derived value would mislead.
    """
    try:
        major, minor, *_ = (int(part) for part in __version__.split(".")[:2])
    except ValueError:
        return None
    return f"M{minor}" if major == 0 else None
