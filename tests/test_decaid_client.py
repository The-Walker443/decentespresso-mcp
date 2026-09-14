"""Decaid-Client gegen Fixtures aus echten Antworten (SPEC ss20.2).

Die Fixtures unter ``tests/fixtures/decaid/`` stammen von der laufenden Instanz
(0.8.5+2624, abgerufen 2026-09-14) und sind anonymisiert. Jeder Test hier
bildet einen der Befunde T1-T19 ab.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from decentespresso_mcp.decaid_client import (
    MAX_PAGE_SIZE,
    VERIFIED_DECAID_VERSION,
    DecaidClient,
    DecaidRejected,
    DecaidUnreachable,
    ShotNotFound,
    measurement_times,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "decaid"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def client(handler, **kwargs) -> DecaidClient:
    api = DecaidClient(
        "http://10.100.100.171:8080",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    api._sleep_backoff = _no_sleep          # type: ignore[method-assign]
    return api


async def _no_sleep(attempt: int) -> None:
    return None


def route(request: httpx.Request) -> httpx.Response:
    """Bildet die echten Endpunkte auf die Fixtures ab."""
    path = request.url.path
    mapping = {
        "/api/v1/info": "info.json",
        "/api/v1/workflow": "workflow.json",
        "/api/v1/beans": "beans.json",
        "/api/v1/bean-batches": "bean_batches.json",
        "/api/v1/shots": "shots_page.json",
        "/api/v1/shots/ids": "shot_ids.json",
        "/api/v1/shots/latest": "shot_detail.json",
    }
    if path in mapping:
        return httpx.Response(200, json=load(mapping[path]))
    if path.startswith("/api/v1/beans/") and path.endswith("/batches"):
        return httpx.Response(200, json=load("bean_batches.json"))
    if path.startswith("/api/v1/shots/"):
        return httpx.Response(200, json=load("shot_detail.json"))
    return httpx.Response(404, text="not found")


# ------------------------------------------------------------------ Lesepfade


async def test_info_reports_the_version() -> None:
    async with client(route) as api:
        info = await api.info()
    assert info["version"] == VERIFIED_DECAID_VERSION
    assert info["fullVersion"].startswith(VERIFIED_DECAID_VERSION)
    assert len(info["commit"]) == 40


async def test_list_shots_parses_the_envelope() -> None:
    """T3: ``{items, total, limit, offset}``."""
    async with client(route) as api:
        page = await api.list_shots(limit=3)
    assert page.total > 0
    assert page.items
    assert page.offset == 0
    assert {"id", "timestamp", "createdAt", "updatedAt"} <= set(page.items[0])


async def test_limit_is_capped_before_sending() -> None:
    """T4: Die API deckelt still bei 100 - der Client tut es sichtbar."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=load("shots_page.json"))

    async with client(handler) as api:
        await api.list_shots(limit=500)
    assert seen["limit"] == str(MAX_PAGE_SIZE)


async def test_order_and_offset_are_passed_through() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=load("shots_page.json"))

    async with client(handler) as api:
        await api.list_shots(limit=10, offset=40, order="asc", bean_id="abc")
    assert seen == {"limit": "10", "offset": "40", "order": "asc", "beanId": "abc"}


async def test_no_time_filter_is_ever_sent() -> None:
    """T8: Serverseitig gibt es keinen - wer einen mitschickt, taeuscht sich."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=load("shots_page.json"))

    async with client(handler) as api:
        await api.list_shots()
    assert not {"updated_after", "updatedAfter", "since"} & set(seen)


async def test_shot_ids_come_unpaginated() -> None:
    """T9: alle IDs in einem Zug."""
    async with client(route) as api:
        ids = await api.shot_ids()
    assert isinstance(ids, list)
    assert all(isinstance(i, str) for i in ids)


async def test_shot_detail_carries_measurements() -> None:
    """T11/T12."""
    async with client(route) as api:
        shot = await api.get_shot("egal")
    assert shot["measurements"]
    point = shot["measurements"][0]
    assert set(point) == {"machine", "scale", "volume"}
    assert "time" not in point, "T12: es gibt kein time-Feld"
    assert "profileFrame" in point["machine"], "T12: profileFrame liegt unter machine"
    for key in ("targetFlow", "targetPressure"):
        assert key in point["machine"], "Sollwerte je Messpunkt (SPEC ss20.4)"


async def test_beans_and_batches_use_the_real_paths() -> None:
    """T16: /api/v1/bean-batches, nicht /api/v1/batches."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return route(request)

    async with client(handler) as api:
        await api.beans()
        await api.bean_batches()
        await api.bean_batches(bean_id="b1")

    assert paths == ["/api/v1/beans", "/api/v1/bean-batches", "/api/v1/beans/b1/batches"]


async def test_batches_carry_the_freeze_fields() -> None:
    async with client(route) as api:
        batches = await api.bean_batches()
    assert batches
    assert {"roastDate", "freezeDate", "frozen", "beanId"} <= set(batches[0])


# ------------------------------------------------------------------ Zeitachse


def test_measurement_times_are_derived_from_timestamps() -> None:
    """T12: kein time-Feld, also aus machine.timestamp ableiten."""
    shot = load("shot_detail.json")
    times = measurement_times(shot["measurements"])

    assert len(times) == len(shot["measurements"])
    assert times[0] == 0.0
    assert times == sorted(times), "die Achse muss monoton sein"
    assert times[-1] > 10, "ein Bezug dauert laenger als zehn Sekunden"


def test_measurement_times_of_an_empty_series() -> None:
    assert measurement_times([]) == []


def test_measurement_times_survive_a_broken_stamp() -> None:
    rows = [
        {"machine": {"timestamp": "2026-09-14T07:50:12.000000"}},
        {"machine": {"timestamp": "kaputt"}},
        {"machine": {"timestamp": "2026-09-14T07:50:14.000000"}},
    ]
    assert measurement_times(rows) == [0.0, 0.0, 2.0]


# --------------------------------------------------------------- Schreibpfad


async def test_update_shot_sends_a_deep_merge_patch() -> None:
    """T13: nur das Mitgeschickte aendert sich."""
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=load("shot_detail.json"))

    async with client(handler) as api:
        await api.update_shot("s1", {"annotations": {"enjoyment": 70.0}})
    assert sent == {"annotations": {"enjoyment": 70.0}}


async def test_protected_fields_come_back_as_a_rejection() -> None:
    """T14: Decaid antwortet mit 400 statt still zu verwerfen."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "protected field"})

    async with client(handler) as api:
        with pytest.raises(DecaidRejected) as excinfo:
            await api.update_shot("s1", {"createdAt": "2000-01-01"})
    assert excinfo.value.code == "decaid_rejected"


# ------------------------------------------------- Tablet offline (SPEC ss20.3)


async def test_unreachable_tablet_is_not_an_error_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with client(handler, max_retries=2) as api:
        with pytest.raises(DecaidUnreachable) as excinfo:
            await api.info()

    # Der Code ist die Betriebsaussage, nicht "kaputt".
    assert excinfo.value.code == "waiting_for_tablet"


async def test_server_error_is_retried_then_reported_as_waiting() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    async with client(handler, max_retries=3) as api:
        with pytest.raises(DecaidUnreachable):
            await api.info()
    assert calls["n"] == 3


async def test_transient_failure_recovers() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("tablet schlaeft")
        return httpx.Response(200, json=load("info.json"))

    async with client(handler) as api:
        assert (await api.info())["version"] == VERIFIED_DECAID_VERSION
    assert calls["n"] == 2


async def test_missing_shot_is_distinguishable_from_an_offline_tablet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with client(handler) as api:
        with pytest.raises(ShotNotFound):
            await api.get_shot("gibt-es-nicht")
