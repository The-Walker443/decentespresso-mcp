"""visualizer-mcp: MCP-Server fuer Espresso-Shot-Analyse."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

#: Kommt aus den Paketmetadaten, also aus pyproject.toml - eine zweite,
#: handgepflegte Zahl im Code waere genau die, die man beim Release vergisst.
#: Das ist einmal passiert: status() meldete "M6", waehrend M7-Code lief.
try:
    __version__ = _package_version("visualizer-mcp")
except PackageNotFoundError:  # pragma: no cover - nur ohne Installation
    # Direkt aus dem Quellbaum gestartet (kein pip install). Bewusst kein
    # Rateversuch aus pyproject.toml: eine erfundene Zahl waere schlimmer als
    # eine sichtbare Luecke.
    __version__ = "0+unbekannt"


def milestone() -> str | None:
    """Ausbaustufe aus der Nebenversion (SPEC ss14): 0.7.x -> "M7".

    Nur waehrend der Hauptversion 0 - danach ist die Meilensteinzaehlung
    vorbei und eine abgeleitete Angabe waere irrefuehrend.
    """
    try:
        major, minor, *_ = (int(part) for part in __version__.split(".")[:2])
    except ValueError:
        return None
    return f"M{minor}" if major == 0 else None
