"""Client behaviour against a MockTransport - no network traffic."""

from __future__ import annotations

import base64
import json
import logging
import pathlib

import httpx
import pytest

from decentespresso_mcp.visualizer_client import (
    AuthFailed,
    RateLimiter,
    RequestRejected,
    ShotNotFound,
    Unreachable,
    VisualizerClient,
    VisualizerError,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EMAIL = "shots@example.org"
PASSWORD = "hunter2-but-long-enough"


def make_client(handler, **kwargs) -> VisualizerClient:
    client = VisualizerClient(
        EMAIL, PASSWORD,
        transport=httpx.MockTransport(handler),
        limiter=RateLimiter(windows=((60.0, 10_000),)),
        **kwargs,
    )
    # Backoff off: what is tested is the retry logic, not the waiting.
    client._sleep_backoff = _no_sleep  # type: ignore[method-assign]
    return client


async def _no_sleep(attempt: int, retry_after: str | None) -> None:
    return None


async def test_basic_auth_header_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"id": "x", "name": "tester"})

    async with make_client(handler) as client:
        await client.get_me()

    expected = base64.b64encode(f"{EMAIL}:{PASSWORD}".encode()).decode()
    assert seen["authorization"] == f"Basic {expected}"
    assert seen["user-agent"].startswith("decentespresso-mcp/")


async def test_401_raises_auth_failed() -> None:
    async with make_client(lambda r: httpx.Response(401, json={"error": "nope"})) as c:
        with pytest.raises(AuthFailed) as excinfo:
            await c.get_me()
    assert excinfo.value.code == "auth_failed"
    assert PASSWORD not in str(excinfo.value)


async def test_404_raises_shot_not_found() -> None:
    async with make_client(lambda r: httpx.Response(404, json={"error": "Shot not found"})) as c:
        with pytest.raises(ShotNotFound):
            await c.get_shot("missing")


async def test_429_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "Too many requests"})
        return httpx.Response(200, json={"id": "x"})

    async with make_client(handler) as client:
        assert await client.get_me() == {"id": "x"}
    assert calls["n"] == 3


async def test_422_on_the_profile_endpoint_means_no_profile() -> None:
    # Per the API docs a 422 there means "Shot has no profile" - for us the
    # same as a 404 and no reason to try again.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"error": "Shot has no profile"})

    async with make_client(handler) as client:
        with pytest.raises(ShotNotFound):
            await client.get_profile_tcl("shot-ohne-profil")
        with pytest.raises(ShotNotFound):
            await client.get_profile_json("shot-ohne-profil")

    assert calls["n"] == 2, "no retry"


async def test_422_elsewhere_is_a_rejection_not_a_missing_shot() -> None:
    # Auf /shots bedeutet 422 "Invalid pagination/sort/updated_after".
    async with make_client(lambda r: httpx.Response(422, json={"error": "bad"})) as c:
        with pytest.raises(RequestRejected) as excinfo:
            await c.list_shots(page=0)
    assert excinfo.value.code == "visualizer_rejected"


@pytest.mark.parametrize("status", [400, 405, 409, 418])
async def test_unexpected_4xx_becomes_a_structured_error(status: int) -> None:
    # This used to surface as an httpx.HTTPStatusError the sync worker does not
    # catch - one endpoint would have ended the whole run.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="nope")

    async with make_client(handler) as client:
        with pytest.raises(RequestRejected) as excinfo:
            await client.get_me()

    assert isinstance(excinfo.value, VisualizerError)
    assert str(status) in str(excinfo.value)
    assert calls["n"] == 1, "a 4xx is not retried"


async def test_403_is_treated_as_auth_failure() -> None:
    async with make_client(lambda r: httpx.Response(403, json={"error": "nope"})) as c:
        with pytest.raises(AuthFailed):
            await c.get_me()


async def test_persistent_5xx_gives_up_with_unreachable() -> None:
    async with make_client(lambda r: httpx.Response(503), max_retries=3) as client:
        with pytest.raises(Unreachable):
            await client.get_me()


async def test_list_shots_parses_paging() -> None:
    body = json.loads((FIXTURES / "shots_list_page1.json").read_text(encoding="utf-8"))

    async with make_client(lambda r: httpx.Response(200, json=body)) as client:
        page = await client.list_shots(page=1, items=3)

    assert page.count == body["paging"]["count"]
    assert page.pages == body["paging"]["pages"]
    assert len(page.rows) == 3
    assert set(page.rows[0]) == {"id", "clock", "updated_at"}


async def test_updated_after_sets_sort_param() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": [], "paging": {"count": 0, "page": 1,
                                                                "limit": 10, "pages": 0}})

    async with make_client(handler) as client:
        await client.list_shots(updated_after=1785575856)

    # Per the API docs updated_after only works together with sort=updated_at.
    assert seen["sort"] == "updated_at"
    assert seen["updated_after"] == "1785575856"


async def test_etag_304_is_reported_not_modified() -> None:
    async with make_client(lambda r: httpx.Response(304)) as client:
        page = await client.list_shots(etag='W/"abc"')
        assert page.not_modified is True
        assert page.rows == []
        assert await client.get_shot("some-id", etag='W/"abc"') is None


async def test_iter_all_shot_rows_walks_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json={
            "data": [{"id": f"shot-{page}", "clock": 1, "updated_at": page}],
            "paging": {"count": 3, "page": page, "limit": 1, "pages": 3},
        })

    async with make_client(handler) as client:
        rows = await client.iter_all_shot_rows(items=1)

    assert [r["id"] for r in rows] == ["shot-1", "shot-2", "shot-3"]


async def test_profile_endpoints_return_raw_bodies() -> None:
    tcl = (FIXTURES / "profile_reference.tcl").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("format") == "json":
            return httpx.Response(200, json={"title": "D-Flow / default"})
        return httpx.Response(200, text=tcl, headers={"Content-Type": "application/x-tcl"})

    async with make_client(handler) as client:
        assert (await client.get_profile_tcl("id")).startswith("advanced_shot")
        assert (await client.get_profile_json("id"))["title"] == "D-Flow / default"


async def test_credentials_never_reach_the_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)

    async with make_client(lambda r: httpx.Response(503), max_retries=2) as client:
        with pytest.raises(Unreachable):
            await client.get_me()

    blob = "\n".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert PASSWORD not in blob
    assert EMAIL not in blob
    assert "Basic" not in blob


class TestRateLimiter:
    async def test_allows_up_to_the_limit_without_waiting(self) -> None:
        limiter = RateLimiter(windows=((60.0, 3),))
        for _ in range(3):
            await limiter.acquire()          # laeuft sofort durch

    async def test_blocks_once_the_window_is_full(self, monkeypatch) -> None:
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            # Empty the window artificially so the second attempt gets through.
            for window in limiter._windows:
                window.hits.clear()

        monkeypatch.setattr("decentespresso_mcp.visualizer_client.asyncio.sleep", fake_sleep)
        limiter = RateLimiter(windows=((60.0, 2),))
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        assert len(slept) == 1
        assert 0 < slept[0] <= 60.0
