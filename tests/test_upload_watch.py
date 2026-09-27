"""Did the profile reach the machine? (SPEC §11.7, T47)

The workflow PUT answers before Decaid has even started the Bluetooth upload.
The fixture is Decaid's own log from 2026-09-27 08:29 (IPs anonymised): two
workflow PUTs from this server, each followed by the upload's last write.
"""

from __future__ import annotations

import pathlib
from datetime import datetime
from typing import Any

import pytest

from decentespresso_mcp.db import Database
from decentespresso_mcp.decaid_client import ShotNotFound
from decentespresso_mcp.sync import MACHINE_UPLOAD, SyncCoordinator
from decentespresso_mcp.upload_watch import newest, outcome, parse

LOG = (pathlib.Path(__file__).parent / "fixtures" / "decaid"
       / "log_profile_upload.txt").read_text(encoding="utf-8")
BEFORE_FIRST = datetime(2026, 9, 27, 8, 29, 40, 265000)
AFTER_FIRST = datetime(2026, 9, 27, 8, 29, 42, 0)


def test_the_first_upload_after_the_put_confirms_it() -> None:
    """08:29:40.272 PUT, 08:29:41.290 tankTemp - measured, 1.02 s."""
    assert outcome(LOG, BEFORE_FIRST) == {"profile_upload": "confirmed", "seconds": 1.02}


def test_an_earlier_upload_says_nothing_about_this_one() -> None:
    """From a mark after the first upload, only the second PUT counts: 0.67 s."""
    assert outcome(LOG, AFTER_FIRST) == {"profile_upload": "confirmed", "seconds": 0.67}


def test_without_our_put_in_the_log_nothing_is_known_yet() -> None:
    assert outcome(LOG, datetime(2026, 9, 27, 8, 29, 44)) is None


def test_a_put_without_an_upload_yet_is_not_confirmed() -> None:
    cut = "\n".join(line for line in LOG.splitlines() if "08:29:41" not in line
                    and "08:29:43.6" not in line)
    assert outcome(cut, BEFORE_FIRST) is None


def test_a_failed_attempt_is_reported() -> None:
    """The WARNING WorkflowDeviceSync writes (workflow_device_sync.dart). No
    failure has happened live yet, so this line follows the source's message,
    in the format of the real lines around it."""
    failed = LOG.replace(
        "[main] 2026-09-27 08:29:41.290054 INFO DE1 - mmr write: tankTemp",
        "[main] 2026-09-27 08:29:41.290054 WARNING WorkflowDeviceSync - setProfile "
        "failed (attempt 0); retrying in 3000ms")
    found = outcome(failed, BEFORE_FIRST)
    assert found["profile_upload"] == "failed_retrying"
    assert "retrying in 3000ms" in found["decaid"]


def test_the_order_of_the_log_does_not_matter() -> None:
    """The API sends newest first; the fixture is oldest first."""
    reversed_log = "\n".join(reversed(LOG.splitlines()))
    assert outcome(reversed_log, BEFORE_FIRST) == outcome(LOG, BEFORE_FIRST)
    assert newest(reversed_log) == parse(LOG)[-1].at


# ------------------------------------------------------ The coordinator

PROFILE = {"title": "Blooming Espresso", "steps": [{"name": "bloom", "pressure": 3}]}
RUNNING = {"title": "D-Flow", "steps": [{"name": "fill", "flow": 4}]}


class Tablet:
    """Duck-typed client: a workflow, a device list and a log that grows."""

    def __init__(self, *, machine: str | None = "connected", uploads: bool = True) -> None:
        self.wf = {"context": {"grinderSetting": "3.8"}, "profile": dict(RUNNING)}
        self.machine = machine
        self.uploads = uploads
        self.log = [line for line in LOG.splitlines() if line < "[main] 2026-09-27 08:29:40.27"]
        self.log_reads = 0

    async def workflow(self):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.wf.items()}

    async def update_workflow(self, body):
        self.wf.update(body)
        # What Decaid does next, in the order it logs it.
        self.log += [x for x in LOG.splitlines() if "08:29:40.27" in x]
        if self.uploads:
            self.log += [x for x in LOG.splitlines() if "08:29:41.29" in x]
        return self.wf

    async def devices(self):
        if self.machine is None:
            return []
        return [{"name": "DE1", "type": "machine", "state": self.machine}]

    async def log_tail(self, kb: int = 32):
        self.log_reads += 1
        return "\n".join(reversed(self.log))


async def apply(tablet: Tablet, profile: dict[str, Any], db: Database):
    coordinator = SyncCoordinator(tablet, db)  # type: ignore[arg-type]
    naps = []

    async def nap(seconds):
        naps.append(seconds)

    coordinator._sleep = nap
    before, after = await coordinator._apply({}, profile, [{"id": "p1", "profile": profile}])
    return after.get(MACHINE_UPLOAD), naps


@pytest.fixture
def db(tmp_path: pathlib.Path):
    database = Database(tmp_path / "upload.db")
    database.migrate()
    yield database
    database.close()


async def test_a_profile_change_reports_the_confirmed_upload(db) -> None:
    upload, _ = await apply(Tablet(), PROFILE, db)
    assert upload == {"profile_upload": "confirmed", "de1": "connected", "seconds": 1.02}


async def test_a_disconnected_de1_is_said_before_anything_is_waited_for(db) -> None:
    """Decaid skips the upload silently and pushes it on the next connect
    (WorkflowDeviceSync._onInitSettled). Waiting would find nothing."""
    tablet = Tablet(machine="disconnected")
    upload, naps = await apply(tablet, PROFILE, db)
    assert upload["profile_upload"] == "pending_connection"
    assert "once it connects" in upload["note"]
    assert naps == [] and tablet.log_reads == 0


async def test_no_signal_is_said_as_such_after_a_bounded_wait(db) -> None:
    tablet = Tablet(uploads=False)
    upload, naps = await apply(tablet, PROFILE, db)
    assert upload["profile_upload"] == "unconfirmed"
    assert "Do not start the shot" in upload["note"]
    assert sum(naps) == pytest.approx(6.0), "WATCH_SECONDS, then it answers"


async def test_the_same_profile_again_is_not_watched(db) -> None:
    """Decaid uploads only when the profile object changes."""
    tablet = Tablet()
    upload, naps = await apply(tablet, dict(RUNNING), db)
    assert upload is None and naps == [] and tablet.log_reads == 0


async def test_a_new_title_alone_is_an_upload_too(db) -> None:
    """WorkflowDeviceSync compares the whole profile, so a rename re-uploads."""
    upload, _ = await apply(Tablet(), {**RUNNING, "title": "D-Flow (2)"}, db)
    assert upload["profile_upload"] == "confirmed"


async def test_a_tablet_without_a_log_answers_unconfirmed_at_once(db) -> None:
    class NoLog(Tablet):
        async def log_tail(self, kb: int = 32):
            raise ShotNotFound("Not found: /api/v1/logs")

    upload, naps = await apply(NoLog(), PROFILE, db)
    assert upload["profile_upload"] == "unconfirmed" and naps == []
