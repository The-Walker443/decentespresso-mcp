"""Benachrichtigung der Waechter (SPEC ss20.7).

Die drei Zusagen aus dem Modul-Docstring von ``notify`` stehen hier unter Test:
hoechstens eine Nachricht je Bezug, keine Inhalte, und ein Ausfall von ntfy
kippt nichts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from visualizer_mcp.config import Config
from visualizer_mcp.db import Database
from visualizer_mcp.guards import Finding
from visualizer_mcp.notify import (
    MAX_MESSAGES_PER_RUN,
    STATE_NOTIFIED,
    compose,
    group_by_shot,
    pending,
    send,
)


@pytest.fixture
def notifying(valid_env: dict[str, str]) -> Config:
    return Config.from_env({
        **valid_env,
        "NTFY_URL": "https://ntfy.example.org",
        "NTFY_TOPIC": "espresso",
        "NTFY_TOKEN": "tk_geheim",
    })


def finding(rule: str, shot_id: str = "s1", message: str = "Befund",
            *, age_hours: int = 2) -> Finding:
    """Ein Befund, standardmaessig frisch genug fuer eine Meldung."""
    when = datetime.now(UTC) - timedelta(hours=age_hours)
    return Finding(rule=rule, shot_id=shot_id,
                   started_at=when.isoformat(timespec="seconds").replace("+00:00", "Z"),
                   message=message, detail={})


class FakeNtfy:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(request)
        return httpx.Response(self.status, text="ok")


@pytest.fixture
def ntfy(monkeypatch):
    fake = FakeNtfy()
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("visualizer_mcp.notify.httpx.AsyncClient", patched)
    return fake


# ------------------------------------------------ Eine Nachricht je Bezug


def test_several_findings_become_one_message(archive: Database, notifying, ntfy) -> None:
    findings = [finding("bean_age"), finding("dose_outlier"),
                finding("missing_rating")]
    assert len(group_by_shot(findings)) == 1


async def test_one_message_per_shot(archive: Database, notifying, ntfy) -> None:
    sent = await send(notifying, [finding("bean_age", "s1"),
                                  finding("dose_outlier", "s1"),
                                  finding("bean_age", "s2")], archive)
    assert sent == 2
    assert len(ntfy.posts) == 2


async def test_a_shot_is_never_reported_twice(archive: Database, notifying, ntfy) -> None:
    """Sonst kaeme bei jedem Abgleich dieselbe Meldung erneut."""
    await send(notifying, [finding("bean_age", "s1")], archive)
    again = await send(notifying, [finding("bean_age", "s1")], archive)
    assert again == 0
    assert len(ntfy.posts) == 1


async def test_the_memory_survives_a_restart(archive: Database, notifying, ntfy) -> None:
    await send(notifying, [finding("bean_age", "s1")], archive)
    assert archive.get_json_state(STATE_NOTIFIED) == ["s1"]
    # Der Zustand liegt in der Datenbank, nicht im Prozess.
    assert pending(archive, [finding("bean_age", "s1")]) == []


async def test_a_new_shot_is_still_reported(archive: Database, notifying, ntfy) -> None:
    await send(notifying, [finding("bean_age", "s1")], archive)
    assert await send(notifying, [finding("bean_age", "s2")], archive) == 1


# ------------------------------------------------------ Keine Inhalte


def test_the_message_carries_no_free_text() -> None:
    title, body = compose([finding("bean_age", "s1", "Bohne war 75 Tage alt.")])
    for text in (title, body):
        assert "Notiz" not in text
    assert "75 Tage" in body
    assert "s1" in body


def test_the_title_survives_ascii_headers(archive: Database, notifying, ntfy) -> None:
    """ntfy-Header vertragen kein UTF-8 - der Titel darf daran nicht scheitern."""
    title, _ = compose([finding("bean_age", "s1", "Bohne zu alt")])
    assert title.startswith("Bezug 20")


async def test_the_body_is_capped(archive: Database, notifying, ntfy) -> None:
    long_findings = [finding("bean_age", "s1", "x" * 400) for _ in range(10)]
    _, body = compose(long_findings)
    assert len(body) <= 900


async def test_the_token_goes_in_the_header_not_the_body(
    archive: Database, notifying, ntfy
) -> None:
    await send(notifying, [finding("bean_age", "s1")], archive)
    request = ntfy.posts[0]
    assert request.headers["Authorization"] == "Bearer tk_geheim"
    assert b"tk_geheim" not in request.content


# ------------------------------------------------------ Ausfall


async def test_a_failing_ntfy_does_not_raise(
    archive: Database, notifying, monkeypatch
) -> None:
    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "visualizer_mcp.notify.httpx.AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(refusing)}),
    )
    assert await send(notifying, [finding("bean_age", "s1")], archive) == 0


async def test_a_failed_message_is_retried_next_time(
    archive: Database, notifying, monkeypatch
) -> None:
    """Nur erfolgreich Gemeldetes gilt als gemeldet."""
    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "visualizer_mcp.notify.httpx.AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(refusing)}),
    )
    await send(notifying, [finding("bean_age", "s1")], archive)
    assert pending(archive, [finding("bean_age", "s1")]), "darf nicht als erledigt gelten"


async def test_without_a_topic_nothing_is_sent(archive: Database, config: Config) -> None:
    # Der Waechter laeuft trotzdem; seine Befunde stehen in audit_archive.
    assert await send(config, [finding("bean_age", "s1")], archive) == 0


# ------------------------------------------------------ Fenster und Deckel


async def test_old_findings_never_ring(archive: Database, notifying, ntfy) -> None:
    """Eine Benachrichtigung sagt "eben ist etwas schiefgegangen".

    Am Bestand stehen 63 Befunde ueber die ganze Historie - so viele
    Nachrichten auf einmal liest niemand, und danach keine weitere mehr. Was
    laenger her ist, steht in audit_archive.
    """
    assert await send(notifying, [finding("bean_age", "alt", age_hours=24 * 30)],
                      archive) == 0
    assert ntfy.posts == []


async def test_a_flood_is_capped(archive: Database, notifying, ntfy) -> None:
    """Auch im Fenster kann viel zusammenkommen - etwa nach einem Ausfall."""
    findings = [finding("dose_outlier", f"s{i}", age_hours=i + 1) for i in range(20)]
    sent = await send(notifying, findings, archive)
    assert sent == MAX_MESSAGES_PER_RUN
    assert len(ntfy.posts) == MAX_MESSAGES_PER_RUN


async def test_the_newest_finding_gets_through_the_cap(
    archive: Database, notifying, ntfy
) -> None:
    findings = [finding("dose_outlier", f"s{i}", age_hours=i + 1) for i in range(20)]
    await send(notifying, findings, archive)
    reported = set(archive.get_json_state(STATE_NOTIFIED))
    assert "s0" in reported, "der juengste Bezug muss durchkommen"
    assert "s19" not in reported


async def test_the_rest_is_not_marked_as_done(
    archive: Database, notifying, ntfy
) -> None:
    findings = [finding("dose_outlier", f"s{i}", age_hours=i + 1) for i in range(8)]
    await send(notifying, findings, archive)
    # Was der Deckel abgeschnitten hat, bleibt offen - solange es im Fenster ist.
    assert pending(archive, findings)
