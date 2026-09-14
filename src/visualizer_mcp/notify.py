"""Benachrichtigung ueber ntfy (SPEC ss20.7).

Drei Regeln, die alle denselben Zweck haben - dass die Meldungen gelesen
bleiben:

*Hoechstens eine Nachricht je Bezug.* Vier Regeln koennen an einem Bezug
gleichzeitig anschlagen. Vier Nachrichten hintereinander liest niemand zu Ende;
eine mit vier Zeilen schon. Welcher Bezug schon gemeldet wurde, steht im
Sync-Zustand und ueberlebt damit einen Neustart.

*Keine Inhalte.* Eine Nachricht nennt Kennung, Regel und Zahlen. Notizen,
Bohnennamen und Bewertungen bleiben hier - ntfy laeuft auf einem fremden
Server, und was einmal dort war, ist dort.

*Ausbleiben ist kein Fehler.* Geht ntfy nicht, wird das protokolliert und der
Sync laeuft weiter. Eine Benachrichtigung ist kein Teil der Archivierung.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx

from .config import Config
from .db import Database
from .guards import Finding

log = logging.getLogger(__name__)

#: Bereits gemeldete Bezuege. Im Sync-Zustand, damit ein Neustart nicht alles
#: erneut meldet.
STATE_NOTIFIED = "notified_shots"

#: So viele Kennungen werden behalten. Mehr braucht es nicht: was so lange her
#: ist, wurde entweder bearbeitet oder bewusst stehen gelassen.
KEEP_NOTIFIED = 200

#: Laengster Text einer Nachricht. ntfy schneidet sonst selbst ab, und zwar an
#: einer schlechteren Stelle.
MAX_BODY_CHARS = 900

#: Nur Bezuege aus diesem Fenster werden gemeldet. Eine Benachrichtigung sagt
#: "eben ist etwas schiefgegangen"; was vor Wochen war, steht in
#: ``audit_archive`` und braucht kein Klingeln. Am Bestand gemessen
#: (2026-09-14): 63 Befunde ueber die ganze Historie - so viele Nachrichten auf
#: einmal liest niemand, und danach auch keine weitere mehr.
NOTIFY_WINDOW_HOURS = 48

#: Harte Obergrenze je Lauf. Faengt den Fall ab, dass im Fenster doch viel
#: zusammenkommt - etwa nach einem Ausfall des Tablets.
MAX_MESSAGES_PER_RUN = 5


def pending(
    db: Database, findings: Sequence[Finding], *, at: datetime | None = None,
    window_hours: int = NOTIFY_WINDOW_HOURS,
) -> list[Finding]:
    """Befunde, die gemeldet gehoeren: frisch genug und noch nicht gemeldet."""
    seen = set(db.get_json_state(STATE_NOTIFIED, []) or [])
    cutoff = (at or datetime.now(UTC)) - timedelta(hours=window_hours)
    return [
        f for f in findings
        if f.shot_id not in seen and _recent(f.started_at, cutoff)
    ]


def _recent(started_at: str | None, cutoff: datetime) -> bool:
    if not started_at:
        return False
    try:
        moment = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment >= cutoff


def mark_notified(db: Database, shot_ids: Sequence[str]) -> None:
    seen = list(db.get_json_state(STATE_NOTIFIED, []) or [])
    seen.extend(s for s in shot_ids if s not in seen)
    db.set_json_state(STATE_NOTIFIED, seen[-KEEP_NOTIFIED:])


def compose(findings: Sequence[Finding]) -> tuple[str, str]:
    """``(Titel, Text)`` fuer eine Gruppe von Befunden zu einem Bezug."""
    shot_id = findings[0].shot_id
    when = (findings[0].started_at or "")[:16].replace("T", " ")
    title = f"Bezug {when}" if when else f"Bezug {shot_id[:12]}"
    if len(findings) > 1:
        title += f" - {len(findings)} Befunde"

    lines = [f"- {f.message}" for f in findings]
    lines.append(f"({shot_id})")
    body = "\n".join(lines)
    return title, body[:MAX_BODY_CHARS]


def group_by_shot(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.shot_id, []).append(finding)
    return grouped


async def send(config: Config, findings: Sequence[Finding], db: Database) -> int:
    """Meldet, was noch nicht gemeldet wurde. Gibt die Zahl der Nachrichten zurueck.

    Ohne ``NTFY_URL`` passiert nichts - der Waechter laeuft trotzdem und seine
    Befunde stehen in ``audit_archive``.
    """
    if not config.ntfy_url or not config.ntfy_topic:
        return 0

    fresh = pending(db, findings)
    if not fresh:
        return 0

    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if config.ntfy_token:
        headers["Authorization"] = f"Bearer {config.ntfy_token}"

    url = f"{config.ntfy_url.rstrip('/')}/{config.ntfy_topic}"
    sent: list[str] = []

    # Neueste zuerst, damit bei Deckelung das Aktuelle durchkommt.
    grouped = sorted(
        group_by_shot(fresh).items(),
        key=lambda kv: kv[1][0].started_at or "", reverse=True,
    )[:MAX_MESSAGES_PER_RUN]

    async with httpx.AsyncClient(timeout=15) as client:
        for shot_id, group in grouped:
            title, body = compose(group)
            try:
                response = await client.post(
                    url, content=body.encode("utf-8"),
                    headers={**headers, "Title": _ascii(title)},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # Kein Abbruch: eine Benachrichtigung ist kein Teil der
                # Archivierung.
                log.warning("ntfy failed",
                            extra={"fields": {"error": type(exc).__name__}})
                continue
            sent.append(shot_id)

    if sent:
        mark_notified(db, sent)
        log.info("guards notified", extra={"fields": {"messages": len(sent)}})
    return len(sent)


def _ascii(text: str) -> str:
    """ntfy-Header vertragen kein UTF-8.

    Der Titel ist ohnehin nur Zeitstempel und Zahlen; der Text im Rumpf bleibt
    unangetastet.
    """
    return text.encode("ascii", "replace").decode("ascii")
