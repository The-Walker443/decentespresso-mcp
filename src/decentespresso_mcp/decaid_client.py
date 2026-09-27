"""HTTP client for the Decaid REST API on the local network (SPEC §4).

Verified against the running instance, last on 2026-09-26 against Decaid
0.8.6+2801 (SPEC §4 says which findings were rechecked then). The
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

#: The Decaid build this was verified against, version and build number both.
#: status() warns on any deviation; bump it here after every checked update
#: (SPEC §4). The build counts: T10 changed between 0.8.5+2624 and 0.8.6+2801,
#: so "0.8.6" alone would say less than what was actually checked.
VERIFIED_DECAID_VERSION = "0.8.6+2801"

#: T4: the API silently caps at 100. Asking for more returns 100 items without
#: comment - not knowing that, one ends up paging in circles.
MAX_PAGE_SIZE = 100

#: T46: the shot id the scale probe writes to. It must never exist; the probe
#: relies on the lookup failing.
SCALE_PROBE_ID = "decentespresso-mcp-scale-probe"

#: T46: builds are numbered by commits on main, and #887 is in every build from
#: 2836 on. Only the fallback when the probe gives no clear answer: a build off
#: another branch can carry that count without the change.
ENJOYMENT_0_10_FROM_BUILD = 2836

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


class AlreadyExists(DecaidError):
    """Refused before sending: the thing to be created is already there."""

    code = "already_exists"


class Protected(DecaidError):
    """Refused before sending: a profile this server does not get to change."""

    code = "protected"


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
        json_body: Any = None, retry: bool = True,
    ) -> httpx.Response:
        """One request, retried only on 5xx and network errors.

        No rate limiter: the tablet sits on the local network and is not a
        burden on anyone else. A network error is the normal case here (sleeping
        tablet) and is therefore reported as ``DecaidUnreachable``, not as a
        fault.

        ``retry=False`` for anything that creates. A PUT sent twice leaves the
        same state; a POST sent twice after a timeout the server did act on
        leaves two beans. Better to report "unclear, check the list" once than
        to create a duplicate quietly.
        """
        last: Exception | None = None
        for attempt in range(self._max_retries if retry else 1):
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

    # --------------------------------------------------------------- Endpoints

    async def info(self) -> dict[str, Any]:
        """``GET /api/v1/info`` (T2) - version, commit, build time."""
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
        """``GET /api/v1/shots/ids`` (T9) - every id in one go, unpaginated."""
        return list((await self._request("GET", "/api/v1/shots/ids")).json())

    async def latest_shot(self) -> dict[str, Any]:
        """``GET /api/v1/shots/latest`` (T10).

        Since Decaid 0.8.6 this carries no ``measurements`` any more - use it
        for the id and fetch the detail with ``get_shot``. Nothing here calls
        it for the series.
        """
        return (await self._request("GET", "/api/v1/shots/latest")).json()

    async def get_shot(self, shot_id: str) -> dict[str, Any]:
        """``GET /api/v1/shots/<id>`` (T11) - Detail inklusive ``measurements``."""
        return (await self._request("GET", f"/api/v1/shots/{shot_id}")).json()

    async def rejects_enjoyment_over_ten(self) -> bool | None:
        """Whether this Decaid keeps ratings on 0-10 (decaid#887, T46).

        Asks the behaviour rather than the version, and changes nothing: a PUT
        of ``enjoyment: 11`` to a shot id that does not exist. With #887 the
        range check runs before the lookup and answers 400; before it, the
        lookup answers 404 (measured on 0.8.6+2801). ``None`` for any other
        answer - the caller then falls back to the build number.
        """
        try:
            await self._request("PUT", f"/api/v1/shots/{SCALE_PROBE_ID}",
                                json_body={"annotations": {"enjoyment": 11}},
                                retry=False)
        except ShotNotFound:
            return False
        except DecaidRejected as exc:
            return "enjoyment" in str(exc) or None
        return None

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

    async def bean(self, bean_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/api/v1/beans/{bean_id}")).json()

    async def bean_batch(self, batch_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/api/v1/bean-batches/{batch_id}")).json()

    async def create_bean(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/v1/beans`` - requires ``roaster`` and ``name``. Never retried."""
        return (await self._request("POST", "/api/v1/beans", json_body=payload,
                                    retry=False)).json()

    async def create_bean_batch(
        self, bean_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """``POST /api/v1/beans/<id>/batches``. Never retried."""
        return (await self._request("POST", f"/api/v1/beans/{bean_id}/batches",
                                    json_body=payload, retry=False)).json()

    # --- Plugin store (read-only) --------------------------------------------

    async def store_array(self, namespace: str, key: str) -> list[dict[str, Any]]:
        """``GET /api/v1/store/<namespace>/<key>`` as a list - never written from here.

        DYE2's KV contract: a key never written answers 200 with ``null``, and
        any non-array is to be read as empty. There is deliberately no writing
        counterpart in this client; the contract makes DYE2 the single writer.
        """
        value = (await self._request("GET", f"/api/v1/store/{namespace}/{key}")).json()
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    # --- Profiles ------------------------------------------------------------

    async def profiles(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """``GET /api/v1/profiles`` - ProfileRecords, content-hash ids."""
        params = {"includeHidden": "true"} if include_hidden else None
        return list((await self._request("GET", "/api/v1/profiles",
                                         params=params)).json())

    async def profile(self, profile_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/api/v1/profiles/{profile_id}")).json()

    async def create_profile(
        self, profile: dict[str, Any], *, parent_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """``POST /api/v1/profiles``. Never retried."""
        body: dict[str, Any] = {"profile": profile}
        if parent_id:
            body["parentId"] = parent_id
        if metadata is not None:
            body["metadata"] = metadata
        return (await self._request("POST", "/api/v1/profiles", json_body=body,
                                    retry=False)).json()

    async def update_profile(
        self, profile_id: str, profile: dict[str, Any]
    ) -> dict[str, Any]:
        """``PUT /api/v1/profiles/<id>``. The id changes with the content."""
        return (await self._request("PUT", f"/api/v1/profiles/{profile_id}",
                                    json_body={"profile": profile})).json()

    # --- Workflow ------------------------------------------------------------

    async def workflow(self) -> dict[str, Any]:
        """``GET /api/v1/workflow`` - the setting for the next shot, with its profile."""
        return (await self._request("GET", "/api/v1/workflow")).json()

    async def update_workflow(self, patch: dict[str, Any]) -> dict[str, Any]:
        return (await self._request("PUT", "/api/v1/workflow", json_body=patch)).json()

    async def devices(self) -> list[dict[str, Any]]:
        """``GET /api/v1/devices`` - machine and scale with their connection state."""
        body = (await self._request("GET", "/api/v1/devices")).json()
        return list(body) if isinstance(body, list) else []

    async def log_tail(self, kb: int = 32) -> str:
        """``GET /api/v1/logs``, newest line first (T47).

        The only place a finished profile upload shows: the workflow PUT
        answers before the upload has even started.
        """
        response = await self._request("GET", "/api/v1/logs",
                                       params={"kb": kb, "order": "desc"})
        return response.text


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
