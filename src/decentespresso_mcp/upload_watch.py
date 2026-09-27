"""Did the profile reach the machine? Read off Decaid's log (SPEC §11.7, T47).

`PUT /api/v1/workflow` answers as soon as the workflow is stored. The profile
goes to the DE1 afterwards, over Bluetooth, from Decaid's WorkflowDeviceSync -
asynchronously, retried on failure, skipped silently while the DE1 is not
connected - and nothing in the API reports that it arrived.

The log does. Decaid's profile upload ends with an MMR write of the tank
temperature, logged at INFO as ``DE1 - mmr write: tankTemp``; a failed attempt
is logged as a WARNING ``setProfile failed``. Measured on 0.8.6+2801 with the
DE1 connected and asleep: the tankTemp line followed the workflow PUT after
0.79 s and 0.86 s (six-step profiles), and every profile PUT in a day of the
live log was followed by one within about a second.

Pure functions over the log text; the polling lives in the coordinator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

#: One line of Decaid's text log: ``[zone] date time LEVEL logger - message``.
#: Logger names can hold spaces ("App Lifecycle", "Scale handler").
_LINE = re.compile(
    r"^\[[^\]]*\] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) ([A-Z]+) (.+?) - (.*)$"
)

#: The last write of Decaid's _sendProfile (unified_de1.profile.dart).
UPLOAD_DONE = ("DE1", "mmr write: tankTemp")
#: WorkflowDeviceSync's message when an attempt fails; it retries after 3, 10
#: and 30 s. Taken from the source - no failure has occurred live yet.
UPLOAD_FAILED = ("WorkflowDeviceSync", "setProfile failed")
#: The request line the web service writes for every call.
WORKFLOW_PUT = re.compile(r"\bPUT\s+\S+\s+\[(\d{3})\]\s+/api/v1/workflow\s*$")

#: How long to watch after the PUT. The measured upload is under a second;
#: this leaves room for one retry (3 s) and a slow radio.
WATCH_SECONDS = 6.0
POLL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    level: str
    logger: str
    message: str


def parse(text: str) -> list[LogLine]:
    """The lines that match the format, oldest first, whatever order came in."""
    lines = []
    for raw in text.splitlines():
        match = _LINE.match(raw.strip())
        if match:
            stamp, level, logger, message = match.groups()
            lines.append(LogLine(datetime.fromisoformat(stamp), level, logger, message))
    lines.sort(key=lambda line: line.at)
    return lines


def newest(text: str) -> datetime | None:
    """The tablet's clock, as far as the log shows it - the mark to watch from."""
    lines = parse(text)
    return lines[-1].at if lines else None


def outcome(text: str, mark: datetime | None) -> dict | None:
    """What happened to the upload after ``mark``, or ``None`` while unknown.

    Only lines after our workflow PUT count: another client's upload before
    it says nothing about ours. Should a second client write the workflow in
    the same second, the first upload after our PUT is taken - the log does
    not say whose it was.
    """
    lines = [line for line in parse(text) if mark is None or line.at > mark]
    put = next((line for line in lines if WORKFLOW_PUT.search(line.message)), None)
    if put is None:
        return None
    for line in lines:
        if line.at < put.at:
            continue
        if (line.logger, line.message) == UPLOAD_DONE:
            return {"profile_upload": "confirmed",
                    "seconds": round((line.at - put.at).total_seconds(), 2)}
        if line.logger == UPLOAD_FAILED[0] and line.message.startswith(UPLOAD_FAILED[1]):
            return {"profile_upload": "failed_retrying", "decaid": line.message[:160]}
    return None


__all__ = ["POLL_SECONDS", "WATCH_SECONDS", "LogLine", "newest", "outcome", "parse"]
