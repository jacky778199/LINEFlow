# Group synchronization health

Each subscribed group has persistent state in `group_sync_status`. Scans record
attempt/finish times, last successful database synchronization, last complete
scan (including return to Chats), outcome, stage, reason, duration and inserted
count. Zero new messages is a successful scan. Cancellation is not a failure.
If writing succeeds but cleanup fails, `last_success_at` advances while
`last_complete_at` does not. Timestamps are Unix seconds (UTC).

An independent asyncio task checks every five seconds, without taking the UI
lock. Starting from the last complete scan (or initial subscription observation):

- At 180 seconds: `warning`.
- At 600 seconds: `critical`.
- After two consecutive complete successful scans: one recovery event.

Notifications occur only on transitions, not every poll. Alert levels survive
restarts. Removing a subscription removes its monitoring state at reconciliation;
new subscriptions receive a fresh grace period. This monitor can detect a blocked
UI worker, but cannot notify while the server process/event loop is down.

## Authenticated WebSocket

After the normal auth handshake, request current state (also on reconnect):

```json
{"action":"get_sync_status","request_id":"health-1"}
```

Response: `type: sync_status`, `instance_id`, `request_id`, `groups`, and
`thresholds: {warning_seconds: 180, critical_seconds: 600}`.
Group states include `alert_level` (last emitted level), `effective_level`
(current health including pending threshold transitions), `stale_seconds`,
`in_progress`, `consecutive_failures`, `consecutive_successes`, and scan fields.

Unsolicited events use `type: sync_alert` or `type: sync_recovered`, with a `data`
object containing the group state and instance ID. Connected clients for that
instance receive events. They are not an offline notification queue; disconnected
clients must request the snapshot. No external notification provider is configured.

## Health endpoint

`GET /health` returns HTTP 200 / `status: ok` when devices, polling and monitors
are alive and no group is warning/critical. Otherwise HTTP 503 / `degraded`.
Existing `alive` and `serial` fields remain, with `healthy`, `monitor_alive`,
`poll_alive`, `group_count`, `warning_groups`, `critical_groups`, `pending_groups`.
Group names and error details are exposed only over authenticated WebSocket.
An external uptime monitor should check this endpoint to catch process failure.

## Latest-position verification

The scanner targets the actual message list with Android accessibility
`scrollForward`. It requires two consecutive checks with a false scroll result,
an unchanged message-list subtree, no “Go to newest message” button, and the
expected conversation title. Full-screen XML stability alone is no longer enough.
No readable text yields a failed observation, not a fresh successful checkpoint.
This is bounded verification of the emulator UI, not proof of upstream LINE
network delivery. LINE UI changes can still require selector updates.
