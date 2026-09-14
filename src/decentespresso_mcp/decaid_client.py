"""HTTP-Client fuer die Decaid-REST-API im LAN (SPEC ss20).

Gegen die laufende Instanz verifiziert am 2026-09-14, Decaid 0.8.5+2624. Die
Befunde stehen als Tabelle T1-T19 in SPEC ss20.2; was davon das Verhalten dieses
Moduls bestimmt, steht bei den jeweiligen Konstanten und Methoden.

Anders als beim Visualizer-Client gibt es hier keine Zugangsdaten: Decaid laeuft
im eigenen LAN ohne Authentifizierung. Dafuer gilt die Regel aus SPEC ss20.6 -
diese Verbindung darf niemals durch den Cloudflare-Tunnel laufen.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

#: Gegen diese Decaid-Version wurde verifiziert. status() warnt bei Abweichung;
#: nach jedem geprueften Update hier nachziehen (SPEC ss20.6).
VERIFIED_DECAID_VERSION = "0.8.5"

#: T4: Die API deckelt still bei 100. Mehr anzufragen liefert kommentarlos 100
#: Elemente - wer das nicht weiss, blaettert versehentlich im Kreis.
MAX_PAGE_SIZE = 100

#: T12: Es gibt kein time-Feld. Die Zeitachse entsteht aus machine.timestamp
#: minus dem ersten Messpunkt.
MEASUREMENT_TIME_FIELD = "timestamp"


class DecaidError(RuntimeError):
    """Basisklasse. Die Meldung darf in Tool-Antworten sichtbar werden."""

    code = "decaid_error"


class DecaidUnreachable(DecaidError):
    """Tablet nicht erreichbar.

    Laut SPEC ss20.3 ist das **kein Fehlerzustand**: das Tablet schlaeft, wird
    bewegt oder haengt am WLAN. Der Aufrufer wartet und holt nach.
    """

    code = "waiting_for_tablet"


class ShotNotFound(DecaidError):
    code = "shot_not_found"


class DecaidRejected(DecaidError):
    """4xx, das kein Wiederholen rechtfertigt - etwa ein geschuetztes Feld (T14)."""

    code = "decaid_rejected"


@dataclass(slots=True)
class ShotPage:
    """Eine Seite von ``GET /api/v1/shots`` (T3)."""

    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class DecaidClient:
    """Zugriff auf Decaid. Schreibend ist einzig ``update_*`` (SPEC ss20.5)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "User-Agent": f"decentespresso-mcp/{__version__} (privat, LAN)",
                "Accept": "application/json",
            },
            transport=transport,
        )

    async def __aenter__(self) -> DecaidClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ intern

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Eine Anfrage mit Retry nur bei 5xx und Netzfehlern.

        Kein Ratelimiter: das Tablet steht im eigenen Netz und wird nicht
        fremdbelastet. Ein Netzfehler ist hier der Normalfall (schlafendes
        Tablet) und wird deshalb als ``DecaidUnreachable`` gemeldet, nicht als
        Stoerung.
        """
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self._client.request(
                    method, path, params=params, json=json_body
                )
            except httpx.HTTPError as exc:
                last = DecaidUnreachable(
                    f"Decaid nicht erreichbar ({type(exc).__name__})."
                )
                log.info(
                    "decaid unreachable",
                    extra={"fields": {"path": path, "kind": type(exc).__name__,
                                      "attempt": attempt + 1}},
                )
            else:
                if response.status_code == 404:
                    raise ShotNotFound(f"Nicht gefunden: {path}")
                if 400 <= response.status_code < 500:
                    raise DecaidRejected(
                        f"Decaid weist die Anfrage ab ({response.status_code}) "
                        f"fuer {path}: {response.text[:180]}"
                    )
                if response.status_code >= 500:
                    last = DecaidUnreachable(
                        f"Decaid antwortet mit {response.status_code}."
                    )
                else:
                    return response
            await self._sleep_backoff(attempt)

        raise last or DecaidUnreachable(f"Keine Antwort von Decaid fuer {path}.")

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(2.0**attempt, 30.0) * (1.0 + random.random() * 0.25)  # noqa: S311
        await asyncio.sleep(delay)

    # --------------------------------------------------------------- Endpunkte

    async def info(self) -> dict[str, Any]:
        """``GET /api/v1/info`` (T2) - Version, Commit, Bauzeit."""
        return (await self._request("GET", "/api/v1/info")).json()

    async def list_shots(
        self, *, limit: int = MAX_PAGE_SIZE, offset: int = 0,
        order: str = "desc", bean_id: str | None = None,
    ) -> ShotPage:
        """``GET /api/v1/shots`` (T3-T7).

        ``limit`` wird auf 100 begrenzt, weil die API stillschweigend dort
        deckelt (T4) - ohne die Begrenzung glaubte der Aufrufer, er habe mehr
        angefordert als er bekommt.

        Einen serverseitigen Zeitfilter gibt es nicht (T8); der inkrementelle
        Abgleich blaettert stattdessen mit einem eigenen ``updatedAt``-Cursor.
        """
        params: dict[str, Any] = {
            "limit": min(int(limit), MAX_PAGE_SIZE),
            "offset": int(offset),
            "order": order,
        }
        if bean_id:
            params["beanId"] = bean_id
        body = (await self._request("GET", "/api/v1/shots", params=params)).json()
        return ShotPage(
            items=list(body.get("items") or []),
            total=int(body.get("total", 0)),
            limit=int(body.get("limit", params["limit"])),
            offset=int(body.get("offset", params["offset"])),
        )

    async def shot_ids(self) -> list[str]:
        """``GET /api/v1/shots/ids`` (T9) - alle IDs in einem Zug, unpaginiert."""
        return list((await self._request("GET", "/api/v1/shots/ids")).json())

    async def latest_shot(self) -> dict[str, Any]:
        """``GET /api/v1/shots/latest`` (T10) - vollstaendiges Detail."""
        return (await self._request("GET", "/api/v1/shots/latest")).json()

    async def get_shot(self, shot_id: str) -> dict[str, Any]:
        """``GET /api/v1/shots/<id>`` (T11) - Detail inklusive ``measurements``."""
        return (await self._request("GET", f"/api/v1/shots/{shot_id}")).json()

    async def update_shot(self, shot_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """``PUT /api/v1/shots/<id>`` (T13/T14).

        Deep-Merge: mitgeschickte Teilobjekte ergaenzen, sie ersetzen nicht.
        ``measurements``, ``createdAt`` und ``id`` weist Decaid mit 400 ab -
        anders als Visualizer, das Unerlaubtes stillschweigend verwarf. Der
        Aufrufer liest trotzdem frisch nach, weil ein 200 nicht belegt, dass
        jedes Feld auch uebernommen wurde.
        """
        return (await self._request("PUT", f"/api/v1/shots/{shot_id}",
                                    json_body=patch)).json()

    # --- Bohnen und Chargen (T16) -------------------------------------------

    async def beans(self) -> list[dict[str, Any]]:
        """``GET /api/v1/beans``."""
        return list((await self._request("GET", "/api/v1/beans")).json())

    async def bean_batches(self, bean_id: str | None = None) -> list[dict[str, Any]]:
        """``GET /api/v1/bean-batches`` bzw. die Chargen einer Bohne.

        Der im Auftrag genannte Pfad ``/api/v1/batches`` existiert nicht (T16).
        """
        path = (
            f"/api/v1/beans/{bean_id}/batches" if bean_id else "/api/v1/bean-batches"
        )
        return list((await self._request("GET", path)).json())

    async def update_bean(self, bean_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("PUT", f"/api/v1/beans/{bean_id}",
                                    json_body=patch)).json()

    async def update_bean_batch(
        self, batch_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        return (await self._request("PUT", f"/api/v1/bean-batches/{batch_id}",
                                    json_body=patch)).json()

    # --- Workflow ------------------------------------------------------------

    async def workflow(self) -> dict[str, Any]:
        """``GET /api/v1/workflow`` - aktuelle Einstellung samt Profil."""
        return (await self._request("GET", "/api/v1/workflow")).json()

    async def update_workflow(self, patch: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("PUT", "/api/v1/workflow", json_body=patch)).json()


# ------------------------------------------------------------------- Mapping


def measurement_times(measurements: list[dict[str, Any]]) -> list[float]:
    """Sekunden ab dem ersten Messpunkt (T12).

    Decaid liefert kein ``time``-Feld; jeder Punkt traegt nur seinen eigenen
    Zeitstempel. Bezugspunkt ist der erste ``machine.timestamp``.
    """
    if not measurements:
        return []
    stamps = [_parse_ts(m.get("machine", {}).get(MEASUREMENT_TIME_FIELD))
              for m in measurements]
    base = next((s for s in stamps if s is not None), None)
    if base is None:
        return [0.0] * len(measurements)
    return [0.0 if s is None else (s - base).total_seconds() for s in stamps]


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
