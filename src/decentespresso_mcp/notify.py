"""Notification over ntfy (SPEC §10).

Three rules, all serving the same purpose - that the messages keep being
read:

*At most one message per shot.* Four rules can fire on the same shot. Nobody
reads four messages in a row to the end; one with four lines, yes. Which
shots have already been reported lives in the sync state and therefore
survives a restart.

*No content.* A message names the identifier, the rule and the numbers.
Notes, bean names and ratings stay here - ntfy runs on someone else's server,
and whatever once landed there stays there.

*Silence is not a failure.* If ntfy is down it gets logged and the sync
carries on. A notification is not part of archiving.
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

#: Shots already reported. Kept in the sync state so a restart does not
#: report everything again.
STATE_NOTIFIED = "notified_shots"

#: How many identifiers are kept. No more are needed: anything that far back
#: has either been dealt with or deliberately left alone.
KEEP_NOTIFIED = 200

#: Longest message body. Otherwise ntfy truncates on its own, and in a worse
#: place.
MAX_BODY_CHARS = 900

#: Only shots inside this window get reported. A notification says "something
#: just went wrong"; what happened weeks ago sits in ``audit_archive`` and
#: needs no chime. Measured against the archive (2026-09-14): 63 findings
#: across the whole history - nobody reads that many messages at once, and
#: none afterwards either.
NOTIFY_WINDOW_HOURS = 48

#: Hard cap per run. Catches the case where a lot does pile up inside the
#: window - after the tablet was down, for instance.
MAX_MESSAGES_PER_RUN = 5


def pending(
    db: Database, findings: Sequence[Finding], *, at: datetime | None = None,
    window_hours: int = NOTIFY_WINDOW_HOURS,
) -> list[Finding]:
    """Findings worth reporting: recent enough and not yet reported."""
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
    """``(title, body)`` for a group of findings on one shot."""
    shot_id = findings[0].shot_id
    when = (findings[0].started_at or "")[:16].replace("T", " ")
    title = f"Shot {when}" if when else f"Shot {shot_id[:12]}"
    if len(findings) > 1:
        title += f" - {len(findings)} findings"

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
    """Report what has not been reported yet. Returns the number of messages.

    Without ``NTFY_URL`` nothing happens - the guards still run and their
    findings remain available through ``audit_archive``.
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

    # Newest first, so that the current one gets through when capped.
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
                # No abort: a notification is not part of archiving.
                log.warning("ntfy failed",
                            extra={"fields": {"error": type(exc).__name__}})
                continue
            sent.append(shot_id)

    if sent:
        mark_notified(db, sent)
        log.info("guards notified", extra={"fields": {"messages": len(sent)}})
    return len(sent)


def _ascii(text: str) -> str:
    """ntfy headers do not carry UTF-8.

    The title is only a timestamp and numbers anyway; the body text is left
    untouched.
    """
    return text.encode("ascii", "replace").decode("ascii")
