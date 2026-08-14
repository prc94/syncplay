# Buffer hold

On by default. Server flag `--no-buffer-pause`; client setting **Pause the room while my player is
buffering** (`pauseOnBuffer`, or `--no-pause-on-buffer`).

## The report

Playing a file from a source with load latency — an HTTP stream, a slow NAS, a cold spinning disk —
made the room judder for *everybody else*. Whoever was streaming saw their player stall now and
then, which is expected; what was not expected was that everyone else got rewound several seconds
at a time and had their playback speed shifted up and down, repeatedly, with no explanation
anywhere. From the room's point of view the streaming user looked like they were seeking at random.

## What was wrong

Nothing knew what buffering was. A player filling its cache stalls *while still reporting itself as
unpaused*: its position simply stops advancing. Syncplay fed that straight into the machinery for
ordinary time differences, which then did precisely the wrong thing at both ends:

- **The room follows its slowest watcher.** `Room.getPosition` takes `min()` over the reference
  watchers, so a frozen watcher drags the authoritative room position backwards a second per
  second. Every other client measures a growing difference against a position that is going the
  wrong way and, once `REWIND_SUSTAIN_DURATION` has passed, seeks back — again and again for as
  long as the stall lasts.
- **The stalled client fights its own player.** It measures itself behind the room, so
  `_slowDownToCoverTimeDifference` and the fast-forward path both engage against a player that
  cannot move at all.
- **Nobody is told anything.** There is no message for it, so the only visible evidence is
  playback misbehaving.

The sustained-evidence requirement added by [flaky-link sync hardening](flaky-link-sync.md) does not
help here, and could not: that fix distinguishes a *measurement* artefact from a real difference,
and a cache stall is a real difference. The player genuinely is behind. What has to change is the
reaction, not the measurement.

## The fix

A stall is recognised as buffering, the room is paused and told why, and it resumes by itself once
the cache has filled.

```
buffering client                server                          every other client
  detect stall  ──State{buffering:{active,cache}}──▶  buffer hold on the room
                                                       ├─ paused (server authority)
                                                       ├─ forced State broadcast  ──▶ players pause
                                                       ├─ chat to the room         ──▶ scrollback
                                                       └─ State{bufferHold:{…}}    ──▶ live OSD
  cache filled  ──State{buffering:{active:false}}──▶  release → back to playing + chat
```

### Detecting it (client)

Two sources, one answer, in `SyncplayClient._bufferingEvidence`:

- **mpv says so.** `syncplayintf.lua`'s status poll — which already runs every ~0.1 s to report
  pause state and position — also reports `paused-for-cache` and `cache-buffering-state`. That is
  the authoritative signal: it says playback wants to run and cannot, and it comes with a
  percentage worth showing. Inherited by mpv.net, IINA and Memento.
- **Everyone else gets a heuristic.** VLC, MPC-HC/BE and mplayer2 cannot answer, so the client
  watches its own reported position: no real progress (`BUFFER_STALL_TOLERANCE`) for
  `BUFFER_STALL_DETECT` while the room is playing and the player calls itself unpaused.

Either way the room is never asked to wait on less than `BUFFER_STALL_DETECT` of evidence, and
recovery needs `BUFFER_RECOVER_HOLD` of sustained progress — one moving poll is not a recovery.

A frozen position is *normal* in several situations, all excluded up front. `_pauseChangeIsPlayerNoise()`
covers the windows just after a seek, a connection or a file load, and a pending playlist switch;
any seek *we* commanded resets the stall baseline (only `openFile`'s rewind sets `lastRewindTime`,
so that helper does not cover them); and the last few seconds of a file are excluded outright,
because a player sitting on its final frame with `paused` still false is indistinguishable from a
stalled one — long enough to pause the whole room exactly as the file ends. mpv is exempt from that
last exclusion: its own `paused-for-cache` flag answers the question properly. The cost is that a
genuine stall inside the final `PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD` seconds of a file
is not held for on those players — a deliberate trade, since a false hold is worse than a missed one.

A player that reports nothing at all falls back to the heuristic rather than reading as "not
buffering" — absence of evidence must not silently disable the feature.

**While a hold has the room paused, the ordinary rules cannot answer** (`_stallStandsWhileHeld`).
Detection is built on a position that should be advancing and is not — but nothing advances during a
hold, because the hold stopped it. Reading that as recovery is what made a hold cancel itself about
a second after it started, over and over: measured against a link that could not fill the cache,
one hold/release cycle every 1.2 s, on exactly the connection the feature exists for. So while
`_bufferHoldActive` (anyone's hold — it stops our playback just the same) or our own
`_bufferFallbackPaused` is set:

- a player that tracks its own cache is simply asked, so mpv holds for as long as it is really
  stalled — up to the server's `BUFFER_HOLD_MAX`, which is now reachable — and releases the moment
  the cache is full;
- anything else keeps its verdict for `BUFFER_HOLD_SETTLE`, then lets the room try again. Long
  enough to be worth having paused for, short enough not to sit on a cache that did fill.

The stall baseline and the sustained-evidence clock are both dropped while held, so the poll right
after a release cannot declare a fresh stall on evidence gathered before it.

### Holding the room (server)

The report rides the 1 s `State` heartbeat (`{"buffering": {"active": …, "cache": …}}`), because it
is continuous data — the `State`-vs-`Set` rule from `CLAUDE.md`. Two details are load-bearing:

- **It is handled outside the ignoring-on-the-fly gate.** Forcing the hold's pause on the buffering
  client makes it ignore on the fly, and while it does it sends playstate-less `State`s. Gating the
  buffering key with the rest of `updateState` would drop the very report that ends the hold.
- **The hold does not go through `Room.setPaused`.** That is gated on `canControl(setBy)`, and this
  is the server holding the room, not a user pausing it — the same authority
  `pullWatcherIntoSync` exercises. `Room.applyBufferHold` / `releaseBufferHold` set the play state
  directly, which is why the hold works in locked and managed rooms, where the buffering user
  controls nothing and being stuck behind an unfillable cache is worst. The three authority
  checkpoints are untouched.

The hold is derived from the room on every state tick, never from a single report: it stands while
*any* watcher is buffering and lifts when the last one recovers. Because it is re-derived rather
than edge-triggered, a hold that should exist gets armed even if the edge that should have started
it was missed.

**A paused room is never held.** Recording a hold nobody can see looks harmless, but it turns the
next person to press play into "somebody resumed the room by hand", which gives up on the very
watcher being waited for — and that is the common case, not an exotic one: everyone opens the
stream, the room is still paused, and the first play is exactly when a cache is least likely to be
full. Nothing is lost by waiting, because the hold arms within a tick of the room actually playing.

It ends in four ways.

| Ending | What happens |
| --- | --- |
| The last buffering watcher recovers | The room resumes; chat says so |
| A watcher stops reporting (crash, freeze, disconnect) | `Watcher.isBuffering` expires after `BUFFER_REPORT_STALE`; the hold lifts on the next tick |
| The hold outlasts `BUFFER_HOLD_MAX` | Given up on — for **everyone** still stalled, not just the watcher the hold was named after, or the room would be given up on one user at a time. It **stays paused**, since resuming would only stall again, and nobody who is still stalled can re-hold it until they recover |
| Somebody entitled to control resumes the room | Their choice outranks the hold, which is dropped without restoring anything; everyone still stalled is marked given-up so the next tick cannot immediately put it back |

Expiry is checked from the per-watcher state tick rather than a timer of its own: a `LoopingCall`
would need arming, cancelling on the room emptying, and guarding against firing on an emptied room.

Hold state is cleared in `Room.removeWatcher` when the room empties — not in `SyncFactory`'s
room-empty block, because a room *switch* never reaches it (`setWatcherRoom` calls
`RoomManager.moveWatcher` directly). A permanent or persistent room survives being empty, so a
hold left behind there would be inherited by whoever joined next and released on their first tick,
spontaneously resuming them and announcing that an absent stranger had finished buffering.

Everything arriving from a peer is re-validated where it is stored, not only where it is displayed:
a `buffering` block that is not an object is ignored rather than raising out of `lineReceived`
(which would cost that client its connection), `active` must be an actual JSON `true`
(`bool("false")` is `True`), and `cache` is clamped to a plain `0`–`100` integer or dropped —
otherwise one client could make the server repeat an arbitrary value to the whole room every second
for the life of a hold.

A buffer hold deliberately does **not** start the [yap timer](yap-timer-and-pause-warning.md) or the
pause warning. A network stall is not the room talking, and nobody should be nagged for it.

### Not reacting to it (client)

This is the half that fixes the reported symptom. While this client is buffering, or while a hold
is in force for the room, `_changePlayerStateAccordingToGlobalState` skips the rewind, fast-forward
and slowdown branches entirely, clears the sustained-desync latches so a stall cannot bank evidence
for a seek the moment it clears, and puts playback speed back to 1.00 immediately rather than
waiting for a difference that cannot shrink while the player is not playing.

### Telling everybody

- **Chat**, to the whole room, on the hold and on the release. This is what stock clients get, and
  it is the only record that survives in scrollback. Rate-limited per user by
  `BUFFER_CHAT_MIN_INTERVAL` so a flapping link cannot spam a room.
- **A live overlay** on players that advertise the capability: `Waiting for alice to buffer… 0:06
  (34%)`, refreshed from the `bufferHold` `State` field each tick and auto-hiding once the field
  stops arriving. Rendered by `process_bufferhold_osd` in `syncplayintf.lua`, below the yap-timer
  and pause-warning rows.

## Interoperability

| Combination | Behaviour |
| --- | --- |
| Fork client + fork server | Everything above |
| Fork client + stock/older server | The client suppresses its own desync reactions, sends a chat notice and — only where it is entitled to control the room — pauses itself, undoing that pause on recovery unless somebody else has changed the state meanwhile. In a locked or managed room it does not attempt the pause: the server would convert the rejected pause into a readiness toggle |
| Stock client + fork server | Held and resumed like everyone else, and told in chat. It never reports its own stalls, so the room cannot wait for it |
| Either side with the feature off | Nothing is reported or held; unknown `State` keys are ignored as always |

## Tunables (`constants.py`)

| Constant | Default | Meaning |
| --- | --- | --- |
| `BUFFER_STALL_DETECT` | 0.8 s | Frozen for this long before it counts as a stall |
| `BUFFER_STALL_TOLERANCE` | 0.15 s | Progress within that window that still counts as frozen |
| `BUFFER_RECOVER_HOLD` | 1.0 s | Sustained progress before declaring recovery |
| `BUFFER_HOLD_SETTLE` | 4.0 s | How long a stall stands while the room is paused *for it*, on players that cannot report their own cache |
| `BUFFER_REPORT_STALE` | 3.0 s | After this without a report, a watcher stops holding the room |
| `BUFFER_HOLD_MAX` | 120 s | Give up on a hold that has lasted this long |
| `BUFFER_CHAT_MIN_INTERVAL` | 20 s | Minimum gap between a user's own buffering chat notices |
| `BUFFERHOLD_OSD_TIMEOUT` | 3.0 s | Overlay auto-hide, mirrored in `syncplayintf.lua` |

## Tests

`tests/suite_buffer.py` (unit) and `tests/suite_buffer_e2e.py` (live server). The centrepiece is a
regression check that drives the real client sync logic against a stalled peer: against the
pre-feature behaviour it produces seeks and speed changes, and it must now produce none — while a
genuinely desynced client is still corrected once the stall clears.

The live suite also *measures* where everyone ends up, rather than only asserting that a pause and
a resume happened — a hold that quietly loses several seconds would satisfy the latter. It reports
advancing positions for both clients, freezes the staller's, and checks the room waits at the stall
point and sends nobody further back than the stall lasted.
