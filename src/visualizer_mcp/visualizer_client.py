"""HTTP-Client fuer die Visualizer-API (SPEC ss4).

Gegen https://apidocs.visualizer.coffee/ verifiziert: Lesepfade am 2026-08-01
(API v1.15.0), Schreibpfad am 2026-08-01 gegen v1.17.1. Was die Probe-Requests
ergeben haben und wovon die SPEC abweicht, steht bei den jeweiligen Konstanten.

Sicherheit: Credentials gehen ausschliesslich an ``httpx.BasicAuth``. Dieses
Modul loggt niemals Header, Query-Strings mit Auth oder Response-Bodies.
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
#: 200 req/10 min pro Nutzer. Wir bleiben mit Sicherheitsabstand darunter.
RATE_WINDOWS = ((60.0, 40), (600.0, 170))

#: Zeitreihen im Detail-Response: Visualizer liefert Zahlen als STRINGS.
#: Links unsere Spaltennamen (SPEC ss5), rechts die API-Schluessel unter "data".
SERIES_FIELD_MAP = {
    "pressure": "espresso_pressure",
    "flow_in": "espresso_flow",           # Pumpenfluss
    "flow_out": "espresso_flow_weight",   # aus der Waage abgeleiteter Fluss
    "weight": "espresso_weight",
    "temp_mix": "espresso_temperature_mix",
    "temp_basket": "espresso_temperature_basket",
    "state_change": "espresso_state_change",
}

#: Sentinelwert in espresso_state_change fuer "keine Phasenaenderung an diesem Punkt".
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
    """Ein 4xx, das kein Wiederholen rechtfertigt (400, 403, 405, 422, ...).

    Ohne diese Klasse waere daraus eine ``httpx.HTTPStatusError`` geworden - die
    faengt der Sync-Worker nicht, und ein einzelner abweisender Endpunkt haette
    den ganzen Lauf beendet.
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
    """Zugriff auf die eigenen Shots.

    Alle Methoden sind idempotent. Schreibend ist einzig ``update_shot``
    (SPEC ss18); es wird nur aufgerufen, wenn ``WRITE_ENABLED`` gesetzt ist.
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
            headers={"User-Agent": user_agent or f"visualizer-mcp/{__version__} (privat)"},
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
        """Eine Anfrage samt Ratelimit, Retry und Fehlerabbildung.

        Wiederholt wird nur bei 429 und 5xx. Das ist auch fuer PATCH
        unbedenklich: ein Update setzt Felder auf feste Werte und ist damit
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
                # Nur den Typ loggen: die Exception kann die URL enthalten.
                last_error = Unreachable(f"Netzwerkfehler: {type(exc).__name__}")
                log.warning(
                    "request failed",
                    extra={"fields": {"path": path, "kind": type(exc).__name__,
                                      "attempt": attempt + 1}},
                )
            else:
                if response.status_code == 304:
                    # Muss vor raise_for_status() raus: httpx wertet 304 als
                    # Redirect-Fehler, obwohl es die erwartete ETag-Antwort ist.
                    return response
                if response.status_code in (401, 403):
                    raise AuthFailed(
                        f"Visualizer lehnt den Zugriff ab ({response.status_code}) - "
                        "VISUALIZER_EMAIL/VISUALIZER_PASSWORD pruefen."
                    )
                if response.status_code in not_found_statuses:
                    raise ShotNotFound(f"Nicht gefunden: {path}")
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    # Kein Retry: die Anfrage ist falsch, nicht der Zeitpunkt.
                    raise RequestRejected(
                        f"Visualizer weist die Anfrage ab ({response.status_code}) "
                        f"fuer {path}."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = (
                        RateLimited("Visualizer drosselt (429).")
                        if response.status_code == 429
                        else Unreachable(f"Visualizer antwortet mit {response.status_code}.")
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

        raise last_error or Unreachable(f"Keine Antwort von Visualizer fuer {path}.")

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        """Exponentiell mit Jitter, gedeckelt bei 1 h (SPEC ss4)."""
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
        """``GET /me`` -> ``{id, name, public, avatar_url}``. Prueft die Credentials."""
        return (await self._get("/me")).json()

    async def list_shots(
        self, *, page: int = 1, items: int = 100,
        updated_after: int | None = None, etag: str | None = None,
    ) -> Page:
        """``GET /shots`` - liefert pro Shot nur ``id``, ``clock``, ``updated_at``.

        ``updated_after`` (Unix-Sekunden) verlangt ``sort=updated_at`` und wirkt
        nur authentifiziert. Damit werden auch *geaenderte* Shots gefunden, was
        die Seitenheuristik aus SPEC ss6.2 nicht leistet.
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

    #: Am Profil-Endpunkt heisst 422 laut API-Doku "Shot has no profile" - fuer
    #: uns dasselbe wie 404 und kein Grund, es erneut zu versuchen.
    _PROFILE_NOT_FOUND = (404, 422)

    async def update_shot(self, shot_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """``PATCH /shots/{id}`` - setzt die uebergebenen Felder (SPEC ss18).

        Am 2026-08-01 gegen die echte API geprueft; drei Dinge stehen so in
        keiner Doku:

        1. ``Accept: application/json`` ist Pflicht. Ohne den Header antwortet
           die API mit 422 ``"Request must be JSON."`` - auch bei korrektem
           Content-Type.
        2. Nicht erlaubte Felder werden **stillschweigend verworfen**. 400 kommt
           nur, wenn nach dem Filtern nichts uebrig bleibt (``"param is missing
           or the value is empty or invalid: shot"``). Ein Aufruf mit einem
           erlaubten *und* einem gesperrten Feld liefert also 200, ohne das
           gesperrte zu schreiben - deshalb vergleicht ``sync.update_shot``
           hinterher zurueckgelesene Werte statt der Annahme.
        3. Wertebereiche prueft die API nicht. ``espresso_enjoyment`` wurde mit
           999 und -5 anstandslos gespeichert; die Pruefung in ``writes.py`` ist
           der einzige Schutz.

        Die Antwort ist der aktualisierte Shot; der Aufrufer liest trotzdem
        frisch nach, weil das PATCH-Ergebnis nicht denselben Weg nimmt wie der
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
        """``GET /shots/{id}/profile?format=json`` - von Visualizer geparstes Profil.

        Dient als Gegenprobe zum eigenen TCL-Parser; die Versionierung haengt
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
    """Wie ``_to_float``, aber 0 gilt als 'nicht gemessen'.

    Visualizer setzt drink_tds/drink_ey/espresso_enjoyment auf 0, wenn nichts
    erfasst wurde. Eine TDS von 0 % gibt es nicht - als Zahl gespeichert wuerde
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
    """API-Detail -> Zeile fuer ``shots`` (Feldnamen verifiziert am 2026-08-01).

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
    private_notes       private_notes              nur fuer den Eigentuemer
    updated_at          updated_at                 Unix-Sekunden
    raw_json            gesamter Response
    ==================  =========================  ==============================

    Nicht befuellt: ``profile_id`` (M2). Nicht im Response enthalten, obwohl in
    der OpenAPI-Doku gelistet: ``metadata``, ``roaster_id``, ``coffee_bag_id``,
    ``brewdata`` (leeres Objekt bei DE1-Uploads) - sie fehlen, wenn leer.
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
    """``timeframe`` + ``data.*`` -> Zeilen fuer ``shot_series``.

    Die Arrays sind parallel und gleich lang; kuerzere Kanaele werden mit NULL
    aufgefuellt statt den Shot zu verwerfen. Werte kommen als Strings.
    """
    timeframe = detail.get("timeframe") or []
    data = detail.get("data") or {}
    shot_id = detail["id"]

    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for index, raw_elapsed in enumerate(timeframe):
        elapsed = _to_float(raw_elapsed)
        if elapsed is None or elapsed in seen:
            # elapsed ist Teil des Primaerschluessels - Duplikate wuerden den
            # Insert sprengen. In den Echtdaten kommen keine vor.
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
