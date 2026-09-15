"""HTTP client for the Decaid REST API on the local network (SPEC §4).

Verified against the running instance on 2026-09-14, Decaid 0.8.5+2624. The
findings are tabulated as T1-T19 in SPEC §4.2; whichever of them shapes the
behaviour of this module is noted at the relevant constant or method.

Unlike the Visualizer client there are no credentials here: Decaid runs on the
local network without authentication. In exchange the rule from SPEC §12
applies - this connection must never go through the Cloudflare tunnel.
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

#: The Decaid version this was verified against. status() warns on deviation;
#: nach jedem geprueften Update hier nachziehen (SPEC ss20.6).
VERIFIED_DECAID_VERSION = "0.8.5"

#: T4: the API silently caps at 100. Asking for more returns 100 items without
#: comment - not knowing that, one ends up paging in circles.
MAX_PAGE_SIZE = 100

#: T12: there is no time field. The time axis comes from machine.timestamp
#: minus the first data point.
MEASUREMENT_TIME_FIELD = "timestamp"


class DecaidError(RuntimeError):
    """Base class. The message may show up in tool responses."""

    code = "decaid_error"


class DecaidUnreachable(DecaidError):
    """Tablet not reachable.

    Per SPEC §6 this is **not an error state**: the tablet is asleep, being
    carried around, or struggling with the Wi-Fi. The caller waits and catches
    up later.
    """

    code = "waiting_for_tablet"


class ShotNotFound(DecaidError):
    code = "shot_not_found"


class DecaidRejected(DecaidError):
    """A 4xx that does not warrant a retry - a protected field, say (T14)."""

    code = "decaid_rejected"


@dataclass(slots=True)
class ShotPage:
    """One page of ``GET /api/v1/shots`` (T3)."""

    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class DecaidClient:
    """Access to Decaid. Only ``update_*`` writes anything (SPEC §11)."""

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
        """One request, retried only on 5xx and network errors.

        No rate limiter: the tablet sits on the local network and is not a
        burden on anyone else. A network error is the normal case here (sleeping
        tablet) and is therefore reported as ``DecaidUnreachable``, not as
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
                    f"Decaid not reachable ({type(exc).__name__})."
                )
                log.info(
                    "decaid unreachable",
                    extra={"fields": {"path": path, "kind": type(exc).__name__,
                                      "attempt": attempt + 1}},
                )
            else:
                if response.status_code == 404:
                    raise ShotNotFound(f"Not found: {path}")
                if 400 <= response.status_code < 500:
                    raise DecaidRejected(
                        f"Decaid refuses the request ({response.status_code}) "
                        f"for {path}: {response.text[:180]}"
                    )
                if response.status_code >= 500:
                    last = DecaidUnreachable(
                        f"Decaid responded with {response.status_code}."
                    )
                else:
                    return response
            await self._sleep_backoff(attempt)

        raise last or DecaidUnreachable(f"No response from Decaid for {path}.")

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

        ``limit`` is clamped to 100 because the API silently caps there (T4) -
        without the clamp the caller would believe it asked for more than it
        gets.

        There is no server-side time filter (T8); the incremental sync pages
        through with its own ``updatedAt`` cursor instead.
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

        Deep merge: sub-objects sent along add to what is there, they do not
        replace it. Decaid rejects ``measurements``, ``createdAt`` and ``id``
        with 400 - unlike Visualizer, which silently discarded what was not
        allowed. The caller still reads back afterwards, because a 200 does not
        prove that every field was actually taken.
        """
        return (await self._request("PUT", f"/api/v1/shots/{shot_id}",
                                    json_body=patch)).json()

    # --- Beans and batches (T16) --------------------------------------------

    async def beans(self) -> list[dict[str, Any]]:
        """``GET /api/v1/beans``."""
        return list((await self._request("GET", "/api/v1/beans")).json())

    async def bean_batches(self, bean_id: str | None = None) -> list[dict[str, Any]]:
        """``GET /api/v1/bean-batches``, or the batches of one bean.

        The path ``/api/v1/batches`` named in the brief does not exist (T16).
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
    """Seconds from the first data point (T12).

    Decaid provides no ``time`` field; each point carries only its own
    timestamp. The reference is the first ``machine.timestamp``.
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
