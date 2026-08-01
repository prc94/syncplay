# Flaky-link sync hardening

Always on, no flag. Server- and client-side.

## The report

One watcher saw their player rewound, jumped forward, and repeatedly speed-shifted, while the
other two watchers in the room stayed in sync with each other. That watcher was known to have an
unstable connection to the server. The connection was the trigger, but the behaviour was a bug:
their player was never actually out of sync — Syncplay's own latency estimate was moving the room
out from under them.

## What was wrong

Every `State` message carries a ping handshake. Each side estimates the *forward delay* — how long
the message it just received spent in flight — and both sides then compensate with

```python
position += messageAge          # client.py:updateGlobalState, server.py:Watcher._updatePositionByAge
```

Because this estimate is *added to a position*, an estimate that overshoots does not merely fail to
compensate; it actively teleports the listener's idea of where the room is. The old estimator
(`PingService.receiveMessage`) had three defects that made overshoot routine on a jittery link:

1. **The asymmetry term was raw and unclamped.**
   ```python
   if senderRtt < self._rtt:
       self._fd = self._avrRtt / 2 + (self._rtt - senderRtt)
   ```
   `self._rtt` is the *instantaneous* round trip, not the smoothed average, and nothing bounded the
   result. A single 6 s stall on a 50 ms link produced `messageAge = 6.42 s` — larger than the
   stall itself.

2. **`senderRtt` is the peer's round trip measured at a different moment.** On a jittery link the
   two samples are uncorrelated, so their difference is mostly noise rather than genuine path
   asymmetry, and the branch fired constantly.

3. **The estimate was sticky.** It is only recomputed when a `State` carrying
   `clientLatencyCalculation` arrives, so one poisoned sample corrupted several later updates.

Cold start was wrong for the same reason: `senderRtt` is `0` until the peer has a sample of its
own, so `senderRtt < rtt` always held and the first estimates were inflated by a full round trip.

Downstream, nothing sanity-checked the result, and the reactions fired on a *single* sample — so
one bad estimate was enough to seek the player or change its speed.

Note that a *symmetric* stall was harmless: both sides' round trips inflate together and the
difference stays small. It is specifically **asymmetric or uncorrelated jitter** that broke this,
which is exactly what an unstable link produces.

## The fix

**Estimator** (`PingService`, `protocols.py`)

- Round trips far above the running average (`PING_OUTLIER_FACTOR`, `PING_OUTLIER_MARGIN`) are
  treated as congestion spikes: they still register, but at `PING_OUTLIER_AVERAGE_WEIGHT` they
  barely move the average and they are excluded from the asymmetry term entirely.
- The asymmetry correction is clamped per sample (`PING_MAX_ASYMMETRY_CORRECTION`), then smoothed
  into its own moving average, and decays to zero once the evidence stops. A genuinely lopsided
  route still raises the estimate; one-off jitter no longer can.
- The correction is skipped while `senderRtt == 0`, fixing cold start.
- The final estimate is clamped to `[0, PING_MAX_FORWARD_DELAY]`.

**Application points** — `messageAge` is capped at `MAX_MESSAGE_AGE` where it is added to a
position, on both the client and the server. This is defence in depth: it also means a peer that
echoes hostile timestamps cannot move anyone (pre-fix, an hour-old echo yielded a 3870 s estimate).

**Reactions** (`client.py`) — the rewind and slowdown paths now require the desync to hold
continuously for `REWIND_SUSTAIN_DURATION` / `SLOWDOWN_SUSTAIN_DURATION` before acting, via
`_desyncSustainedFor`. This mirrors the sustained-evidence pattern the fast-forward path has always
used through `behindFirstDetected`. A single `State` can no longer move the player.

Also fixed: `_handleStatePing` raised `UnboundLocalError` when a `State`'s `ping` block omitted
`latencyCalculation`. Unreachable against a stock server, which always sends it.

## Measured effect

From `tests/suite_lag.py`, which runs both endpoints' real `PingService` over a synthetic link and
drives the real client sync logic. The client's player is in sync throughout; every event below is
spurious.

| link profile | before | after |
|---|---|---|
| stable (control) | 0 seeks, 0 speed changes | unchanged |
| moderate jitter | 0 seeks, 10–14 speed changes | 0 seeks, 0 speed changes |
| multi-second stalls | 7–12 seeks, 18–52 speed changes, drift up to −3.7 s | 0 seeks, 0 speed changes, drift < 0.01 s |

Worst injected position error on the jittery profile fell from **+3.88 s to +0.09 s**. The stable
link got slightly *more* accurate as well (max error +0.058 s → +0.003 s).

A genuinely desynced client is still corrected: the suite asserts that sustained desyncs are still
rewound, fast-forwarded and slowed down, and that a client 30 s adrift on the harsh profile still
converges (to within 0.25 s).

## Trade-off

The estimator now under-compensates rather than over-compensates when it cannot verify a delay
(mean error on the jittery profile is −0.16 s). On a link with genuinely large, *sustained*
asymmetry the correction still builds up, but it is capped at `PING_MAX_FORWARD_DELAY`. Corrections
for real desyncs are also delayed by up to ~1.5 s while the sustain window fills. Both are cheap
next to spurious multi-second seeks.

## Interop

No wire-format change. The forward delay is estimated locally by each endpoint from fields that
already exist, so a patched client talks to a stock server (and vice versa) exactly as before —
each side simply makes a better estimate for itself. The `MAX_MESSAGE_AGE` cap on the server
protects the room's position references from any client, patched or not.

## Not covered by this change

If a stall exceeds `PROTOCOL_TIMEOUT` (12.5 s) the connection is dropped by both
`SyncplayClient.checkIfConnected` and `Watcher.sendState`, and the client reconnects. On
reconnect `retry(0)` calls `onDisconnect()`, which pauses the local player **if `pauseOnLeave` is
enabled** (it defaults to off). Repeated drops on a bad link therefore still show up as repeated
pauses and rejoins for users who turned that setting on. That is existing, intended behaviour for a
dropped connection rather than part of this bug, so it is left alone here.
