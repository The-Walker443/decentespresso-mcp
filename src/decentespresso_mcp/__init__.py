"""decentespresso-mcp: MCP server for espresso shot analysis."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

#: Taken from the package metadata, and thus from pyproject.toml - a second,
#: hand-maintained number in the code is exactly the one forgotten at release
#: time. That happened once: status() reported a version and a milestone that
#: were both stale, and neither could be told apart from a stale image.
try:
    __version__ = _package_version("decentespresso-mcp")
except PackageNotFoundError:  # pragma: no cover - only without an install
    # Started straight from the source tree (no pip install). Deliberately no
    # guess from pyproject.toml: an invented number would be worse than a
    # visible gap.
    __version__ = "0+unknown"
