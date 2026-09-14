"""HTTP client for the Visualizer API (SPEC §4).

Field names and paths were verified against the live API on 2026-07-31
(API v1.15.0), the write path on 2026-08-01 against v1.17.1. What the probe
requests turned up, and where the spec deviates, is noted at the relevant
constants.

Credentials are Basic Auth over HTTPS (SPEC §4); with that in mind this module
never logs headers, query strings carrying auth, or response bodies.

SUPERSEDED as of M8: the archive's source is Decaid on the local network
(``decaid_client``). This module is kept because the Visualizer upload can carry
on as a community showcase - the server no longer uses it (SPEC §20.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

BASE_URL = "https://visualizer.coffee/api"

#: Dokumentierte Limits: 50 req/min pro IP, 200 req/10 min pro IP, zusaetzlich
#: 200 req/10 min per user. We stay comfortably below that.
RATE_WINDOWS = ((60.0, 40), (600.0, 170))

#: Zeitreihen im Detail-Response: Visualizer liefert Zahlen als STRINGS.
#: Our column names on the left (SPEC §5), the API keys under "data" on the
#: right.
SERIES_FIELD_MAP = {
    "pressure": "espresso_pressure",
    "flow_in": "espresso_flow",           # Pumpenfluss
    "flow_out": "espresso_flow_weight",   # flow derived from the scale
    "weight": "espresso_weight",
    "temp_mix": "espresso_temperature_mix",
    "temp_basket": "espresso_temperature_basket",
    "state_change": "espresso_state_change",
}

#: Sentinel in espresso_state_change for "no phase change at this point".
STATE_CHANGE_NONE = -10_000_000.0


class VisualizerError(RuntimeError):
    """Basisklasse. Die Meldung darf in Tool-Antworten sichtbar werden."""

    code = "visualizer_error"


class AuthFailed(VisualizerError):
    code = "auth_failed"


class ShotNotFound(VisualizerError):
    code = "shot_not_found"


class Unreachable(VisualizerError):
    code = "visualizer_unreachable"


class RequestRejected(VisualizerError):
    """A 4xx that does not warrant a retry (400, 403, 405, 422, ...).

    Without this class it would have surfaced as an ``httpx.HTTPStatusError`` -
    which the sync worker does not catch, so a single refusing endpoint would
    have ended the whole run.
    """

    code = "visualizer_rejected"


class RateLimited(VisualizerError):
    code = "rate_limited"


@dataclass(slots=True)
class Page:
    """Eine Seite von ``GET /shots``. ``rows`` sind ``{id, clock, updated_at}``."""

    rows: list[dict[str, Any]]
    count: int
    page: int
    pages: int
    etag: str | None = None
    not_modified: bool = False


@dataclass(slots=True)
class _SlidingWindow:
    span: float
    limit: int
    hits: deque[float] = field(default_factory=deque)

    def delay(self, now: float) -> float:
        while self.hits and now - self.hits[0] > self.span:
            self.hits.popleft()
        if len(self.hits) < self.limit:
            return 0.0
        return self.span - (now - self.hits[0])

    def record(self, now: float) -> None:
        self.hits.append(now)


class RateLimiter:
    """Sliding-Window-Begrenzer ueber mehrere Fenster gleichzeitig."""

    def __init__(self, windows: tuple[tuple[float, int], ...] = RATE_WINDOWS) -> None:
        self._windows = [_SlidingWindow(span, limit) for span, limit in windows]
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                wait = max(w.delay(now) for w in self._windows)
                if wait <= 0:
                    for w in self._windows:
                        w.record(now)
                    return
                log.info("rate limit pause", extra={"fields": {"seconds": round(wait, 1)}})
                await asyncio.sleep(wait)


class VisualizerClient:
    """Access to one's own shots.

    Every method is idempotent. Only ``update_shot`` writes (SPEC §18); it is
    called only when ``WRITE_ENABLED`` is set.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        user_agent: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
        limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._limiter = limiter or RateLimiter()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
            auth=httpx.BasicAuth(email, password),
            headers={"User-Agent": user_agent or f"decentespresso-mcp/{__version__} (privat)"},
            transport=transport,
        )

    async def __aenter__(self) -> VisualizerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ Requests

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None,
        etag: str | None = None, accept: str | None = None,
        not_found_statuses: tuple[int, ...] = (404,),
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if accept:
            headers["Accept"] = accept
        return await self._request(
            "GET", path, params=params, headers=headers,
            not_found_statuses=not_found_statuses,
        )

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        not_found_statuses: tuple[int, ...] = (404,),
    ) -> httpx.Response:
        """One request including rate limit, retry and error mapping.

        Retries happen only on 429 and 5xx. That is safe for PATCH as well: an
        update sets fields to fixed values and is therefore
        idempotent - ein zweiter Versuch schreibt dasselbe.
        """
        headers = dict(headers or {})

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            await self._limiter.acquire()
            try:
                response = await self._client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                # Log the type only: the exception can contain the URL.
                last_error = Unreachable(f"Netzwerkfehler: {type(exc).__name__}")
                log.warning(
                    "request failed",
                    extra={"fields": {"path": path, "kind": type(exc).__name__,
                                      "attempt": attempt + 1}},
                )
            else:
                if response.status_code == 304:
                    # Muss vor raise_for_status() raus: httpx wertet 304 als
                    # A redirect error, even though this is the expected ETag
                    # response.
                    return response
                if response.status_code in (401, 403):
                    raise AuthFailed(
                        f"Visualizer refuses access ({response.status_code}) - "
                        "VISUALIZER_EMAIL/VISUALIZER_PASSWORD pruefen."
                    )
                if response.status_code in not_found_statuses:
                    raise ShotNotFound(f"Nicht gefunden: {path}")
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    # No retry: the request is wrong, not the timing.
                    raise RequestRejected(
                        f"Visualizer refuses the request ({response.status_code}) "
                        f"for {path}."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = (
                        RateLimited("Visualizer drosselt (429).")
                        if response.status_code == 429
                        else Unreachable(f"Visualizer responded with {response.status_code}.")
                    )
                    log.warning(
                        "retryable response",
                        extra={"fields": {"path": path, "status": response.status_code,
                                          "attempt": attempt + 1}},
                    )
                    await self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                    continue
                response.raise_for_status()
                return response

            await self._sleep_backoff(attempt, None)

        raise last_error or Unreachable(f"No response from Visualizer for {path}.")

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        """Exponential with jitter, capped at 1 h (SPEC §4)."""
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 3600.0))
                return
            except ValueError:
                pass
        delay = min(2.0**attempt, 3600.0) * (1.0 + random.random() * 0.25)  # noqa: S311
        await asyncio.sleep(delay)

    # --------------------------------------------------------------- Endpunkte

    async def get_me(self) -> dict[str, Any]:
        """``GET /me`` -> ``{id, name, public, avatar_url}``. Checks the credentials."""
        return (await self._get("/me")).json()

    async def list_shots(
        self, *, page: int = 1, items: int = 100,
        updated_after: int | None = None, etag: str | None = None,
    ) -> Page:
        """``GET /shots`` - returns only ``id``, ``clock``, ``updated_at`` per shot.

        ``updated_after`` (Unix seconds) requires ``sort=updated_at`` and works
        only when authenticated. It also finds *changed* shots, which the page
        heuristic from SPEC §6.2 does not manage.
        """
        params: dict[str, Any] = {"page": page, "items": items}
        if updated_after is not None:
            params["sort"] = "updated_at"
            params["updated_after"] = int(updated_after)

        response = await self._get("/shots", params=params, etag=etag)
        if response.status_code == 304:
            return Page(rows=[], count=0, page=page, pages=0,
                        etag=etag, not_modified=True)
        body = response.json()
        paging = body.get("paging") or {}
        return Page(
            rows=list(body.get("data") or []),
            count=int(paging.get("count", 0)),
            page=int(paging.get("page", page)),
            pages=int(paging.get("pages", 0)),
            etag=response.headers.get("ETag"),
        )

    async def iter_all_shot_rows(self, *, items: int = 100) -> list[dict[str, Any]]:
        """Alle Listenzeilen ueber alle Seiten (SPEC ss6.1 Initial-Backfill)."""
        first = await self.list_shots(page=1, items=items)
        rows = list(first.rows)
        for page_no in range(2, first.pages + 1):
            rows.extend((await self.list_shots(page=page_no, items=items)).rows)
        return rows

    async def get_shot(self, shot_id: str, *, etag: str | None = None) -> dict[str, Any] | None:
        """``GET /shots/{id}`` - volles Detail inkl. Zeitreihen. ``None`` bei 304."""
        response = await self._get(f"/shots/{shot_id}", etag=etag)
        if response.status_code == 304:
            return None
        return response.json()

    #: At the profile endpoint a 422 means "Shot has no profile" per the API
    #: docs - for us the same as a 404 and no reason to try again.
    _PROFILE_NOT_FOUND = (404, 422)

    async def update_shot(self, shot_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """``PATCH /shots/{id}`` - sets the fields handed in (SPEC §18).

        Checked against the live API on 2026-08-01; three things are documented
        nowhere in
        keiner Doku:

        1. ``Accept: application/json`` is mandatory. Without the header the API
           answers 422 ``"Request must be JSON."`` - even with a correct
           Content-Type.
        2. Nicht erlaubte Felder werden **stillschweigend verworfen**. 400 kommt
           only when nothing survives the filtering (``"param is missing
           or the value is empty or invalid: shot"``). A call with one allowed
           *and* one blocked field therefore returns 200 without the
           gesperrte zu schreiben - deshalb vergleicht ``sync.update_shot``
           values read back afterwards rather than the assumption.
        3. The API does not check value ranges. ``espresso_enjoyment`` was
           stored with 999 and -5 without complaint; the validation in
           ``writes.py`` is the only guard.

        The response is the updated shot; the caller still reads back fresh,
        because the PATCH result does not take the same path as the
        normale Detailabruf.
        """
        response = await self._request(
            "PATCH",
            f"/shots/{shot_id}",
            json_body={"shot": fields},
            headers={"Accept": "application/json"},
        )
        return response.json()

    async def get_profile_tcl(self, shot_id: str) -> str:
        """``GET /shots/{id}/profile`` - Rohprofil, Content-Type application/x-tcl."""
        response = await self._get(
            f"/shots/{shot_id}/profile", not_found_statuses=self._PROFILE_NOT_FOUND
        )
        return response.text

    async def get_profile_json(self, shot_id: str) -> dict[str, Any]:
        """``GET /shots/{id}/profile?format=json`` - the profile as Visualizer parsed it.

        Serves as a cross-check against our own TCL parser; versioning hangs
        weiterhin am Hash des Roh-TCL (SPEC ss5).
        """
        response = await self._get(
            f"/shots/{shot_id}/profile",
            params={"format": "json"},
            not_found_statuses=self._PROFILE_NOT_FOUND,
        )
        return response.json()


# ------------------------------------------------------------------- Mapping


def _to_float(value: Any) -> float | None:
    """API liefert Zahlen teils als String, teils als Zahl, teils als None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive(value: Any) -> float | None:
    """Like ``_to_float``, but 0 counts as 'not measured'.

    Visualizer sets drink_tds/drink_ey/espresso_enjoyment to 0 when nothing was
    recorded. A TDS of 0 % does not exist - stored as a number it would
    sie jede Auswertung verzerren.
    """
    number = _to_float(value)
    return None if number is None or number == 0 else number


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def shot_row_from_detail(detail: dict[str, Any], synced_at: str) -> dict[str, Any]:
    """API detail -> a row for ``shots`` (field names verified 2026-08-01).

    ==================  =========================  ==============================
    Spalte (SPEC ss5)   API-Feld                   Anmerkung
    ==================  =========================  ==============================
    id                  id                         UUID
    started_at          start_time                 ISO8601, bereits UTC ("...Z")
    bean_brand          bean_brand
    bean_type           bean_type
    bean_notes          bean_notes
    profile_name        profile_title
    grinder_model       grinder_model
    grinder_setting     grinder_setting            Freitext, z. B. "4,2"
    dose_g              bean_weight                String -> float
    yield_g             drink_weight               String -> float
    duration_s          duration                   float
    ratio               berechnet                  yield_g / dose_g
    drink_tds           drink_tds                  String, 0 -> NULL
    drink_ey            drink_ey                   String, 0 -> NULL
    enjoyment           espresso_enjoyment         int, 0 -> NULL
    notes               espresso_notes
    private_notes       private_notes              owner only
    updated_at          updated_at                 Unix-Sekunden
    raw_json            gesamter Response
    ==================  =========================  ==============================

    Nicht befuellt: ``profile_id`` (M2). Nicht im Response enthalten, obwohl in
    listed in the OpenAPI docs: ``metadata``, ``roaster_id``, ``coffee_bag_id``,
    ``brewdata`` (an empty object for DE1 uploads) - they are absent when empty.
    """
    dose = _to_float(detail.get("bean_weight"))
    yield_g = _to_float(detail.get("drink_weight"))
    ratio = round(yield_g / dose, 3) if dose and yield_g else None
    enjoyment = _positive(detail.get("espresso_enjoyment"))

    return {
        "id": detail["id"],
        "started_at": _normalize_ts(detail.get("start_time")),
        "bean_brand": _clean(detail.get("bean_brand")),
        "bean_type": _clean(detail.get("bean_type")),
        "bean_notes": _clean(detail.get("bean_notes")),
        "profile_name": _clean(detail.get("profile_title")),
        "grinder_model": _clean(detail.get("grinder_model")),
        "grinder_setting": _clean(detail.get("grinder_setting")),
        "dose_g": dose,
        "yield_g": yield_g,
        "duration_s": _to_float(detail.get("duration")),
        "ratio": ratio,
        "drink_tds": _positive(detail.get("drink_tds")),
        "drink_ey": _positive(detail.get("drink_ey")),
        "enjoyment": int(enjoyment) if enjoyment is not None else None,
        "notes": _clean(detail.get("espresso_notes")),
        "private_notes": _clean(detail.get("private_notes")),
        "raw_json": _dump(detail),
        "updated_at": int(detail.get("updated_at") or 0),
        "synced_at": synced_at,
    }


def series_rows_from_detail(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """``timeframe`` + ``data.*`` -> rows for ``shot_series``.

    The arrays are parallel and of equal length; shorter channels are padded
    with NULL rather than discarding the shot. Values arrive as strings.
    """
    timeframe = detail.get("timeframe") or []
    data = detail.get("data") or {}
    shot_id = detail["id"]

    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for index, raw_elapsed in enumerate(timeframe):
        elapsed = _to_float(raw_elapsed)
        if elapsed is None or elapsed in seen:
            # elapsed is part of the primary key - duplicates would break the
            # insert. None occur in the real data.
            continue
        seen.add(elapsed)
        row: dict[str, Any] = {"shot_id": shot_id, "elapsed": elapsed}
        for column, api_key in SERIES_FIELD_MAP.items():
            channel = data.get(api_key) or []
            value = _to_float(channel[index]) if index < len(channel) else None
            if column == "state_change" and value == STATE_CHANGE_NONE:
                value = None
            row[column] = value
        rows.append(row)
    return rows


def _normalize_ts(value: Any) -> str:
    """``2026-07-31T19:16:00.000Z`` -> ``2026-07-31T19:16:00Z`` (SPEC ss6.6: UTC)."""
    text = _clean(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dump(detail: dict[str, Any]) -> str:
    return json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
