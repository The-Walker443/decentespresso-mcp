# A workflow PUT gives a client no way to know the profile reached the machine

**Project:** decentespresso/decaid · **Version:** 0.8.6+2801, and unchanged on main

## What happens

`PUT /api/v1/workflow` with a new `profile` answers 200 as soon as the workflow
is stored. The profile is uploaded to the DE1 afterwards, by
`WorkflowDeviceSync` (`lib/src/controllers/workflow_device_sync.dart`):
asynchronously, over Bluetooth, queued behind other writes, retried after
3/10/30 s on failure - and skipped without a word while the DE1 is not
connected (`DeviceNotConnectedException` is logged at FINE and the push
waits for the next connect).

Nothing in the API says when, or whether, the upload finished:

- the PUT response and a following `GET /api/v1/workflow` show the new
  profile immediately, uploaded or not;
- `profileUploadFailed` reaches only `connectionStatus.error` on
  `/ws/v1/devices`, a single slot that another error (here an older
  `scaleDisconnected`) can hold;
- success has no signal at all.

## Why it matters

A client that sets a profile and tells the user "done" cannot know the
machine will pull it. A shot started before the upload ends runs the previous
profile - and its stored record names the new one, because
`_persistShotIfNeeded` takes `currentWorkflow` at the end of the shot.

Measured with the DE1 connected and asleep: the upload takes 0.8-0.9 s for a
six-step profile, so the window is short but real, and unbounded while the DE1
is disconnected.

The only observable trace today is the log: `_sendProfile` ends with an MMR
write of the tank temperature, which `GET /api/v1/logs` shows as
`DE1 - mmr write: tankTemp`. Reading a log line to learn a machine state is
not an interface a client should rely on.

## Suggestion

Any one of:

- a field on `GET /api/v1/workflow` (or `/api/v1/machine/state`) saying
  whether the workflow's profile is the one on the machine - e.g.
  `profileSync: "synced" | "pending" | "failed" | "noMachine"`;
- a WebSocket event when an upload completes or fails;
- or document that a PUT does not imply the profile is on the machine, and
  how a client should find out.
