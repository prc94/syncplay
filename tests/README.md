# Fork test suites

Test suites for the fork's features (yap timer, pause warning, 1-hour cap, OSD message channel,
server admins, room locking, track proposals, join position guard, join-time state propagation).
Upstream Syncplay has no test suite; these are self-contained scripts, not pytest — each prints one
line per check and a summary, and exits non-zero on failure.

```bash
python3 tests/run_all.py              # everything (~4-5 min; needs free ports 19001-19198)
python3 tests/run_all.py --unit-only  # fast path (~15 s)
python3 tests/suite_admin.py          # any suite runs standalone
```

| Suite | What it covers |
|---|---|
| `suite_unit.py` | Yap timer + pause warning: Room pause clock, config, chat/State gating matrices, authority guard, players, i18n |
| `suite_cap.py` | The 1-hour give-up cap: trip/stick/rearm lifecycle, suppression, timer stops |
| `suite_osd.py` | `/osd` channel: command parser, validation/clamps, ASS passthrough, routing, tag-strip fallback |
| `suite_admin.py` | Server admins: canControl matrix, locking, dispatcher, auth (chat + `Set:adminAuth`), client auto-auth, **live offscreen Qt dialog** |
| `suite_tracks.py` | Track proposals: validation, routing, per-watcher reminders, deferred delivery, mpv back-channel parse |
| `suite_joinguard.py` | Join position guard: reference-set filtering, catch-up/teleport/give-up rules, locked + controlled rooms, pull rate limiting, client seek-on-file-load |
| `suite_fileswitch.py` | Advancing to the next file: the file-change latch vs. playstate-less/stale States, latch expiry, teleport guard and join pulls still intact |
| `suite_joinprop.py` | Join-time propagation of room state: Hello-vs-Set ordering, domain-overlay reset ownership, track proposals queued until the player is up, player-less chat, cache eviction |
| `suite_permrooms.py` | Room startup: permanent rooms created before the port opens, `loadRooms` merging into live rooms instead of replacing them, name/key consistency |
| `suite_lag.py` | Flaky-link sync hardening: `PingService` outlier/asymmetry/clamp behaviour, `messageAge` caps, sustained-desync gating, the **stale-echo guard** (a lagging watcher's heartbeat is not a keypress) and the **held keypress** (a change that could not be sent is re-sent, once, when the ack lands), plus a bidirectional link simulation asserting an in-sync client is never seeked or speed-shifted while a real desync still converges |
| `suite_buffer.py` | Buffer hold: stall detection from mpv's cache properties and from a frozen position (plus every window that must not trip it — seek, load, connect, end of file, a seek we issued), the server hold's apply/release/timeout/override/cleanup paths including locked rooms and rooms that empty, hostile `buffering`/`bufferHold` payloads, the verdict while the room is paused *for us* (the hold must not read as its own recovery), and a regression check that a stalled peer no longer causes any seek or speed change |
| `suite_lua.py` | `syncplayintf.lua`: **parse gate against Lua 5.1 + 5.2**, luacheck scope gate, static checks (declaration order, block balance, render order) + Python-ported simulations (blink timing, layout signatures) |
| `suite_lint.py` | **ruff (pyflakes rules)** over the Python tree — undefined names, silent redefinitions, dead assignments in branches the runtime suites never reach |
| `suite_flaky_e2e.py` | A **real client** on a **bad link**: real `SyncplayClient` + `SyncClientProtocol` behind a latency proxy in front of a real server, with keypresses on its player. Named link profiles (LAN/HSPA/EDGE, plus blackouts either side of `PROTOCOL_TIMEOUT`); asserts that well-spaced actions always land, that the last press of a burst wins, that the client is never left wedged or mute, that outages cost nothing (short) or self-heal (long), and that a starved player never ping-pongs the room's pause state |
| `suite_e2e*.py` | Live-server scenarios: real `syncplayServer.py` subprocesses driven by protocol-faithful socket clients |

## Lint and parse gates

Both gates need external tools and **skip themselves cleanly when those are absent**, so the suites
still run on a bare machine — but then they are not checking anything. To enable them:

```bash
sudo apt install lua5.1 lua5.2 lua-check   # parse + scope gates for syncplayintf.lua
pipx install ruff                          # Python static analysis
```

**Why 5.1 *and* 5.2:** mpv embeds LuaJIT/5.1 on Windows and several distros, 5.2 elsewhere. A newer
host `luac` accepts syntax (`goto`, `//`, bitwise operators) that some mpv builds reject, and a lua
syntax error does not degrade — mpv fails to load the script and chat, the yap timer, the pause
warning and track proposals all disappear together, looking like "the feature didn't show up".
Checking against only whatever `luac` happens to be installed is worse than not checking.

**Baselines.** Every finding that existed when these gates were added is in upstream code (38 ruff,
32 luacheck). Fixing them would conflict on every upstream merge, so they are recorded in
`lint_baseline_ruff.txt` / `lint_baseline_luacheck.txt` and the gates fail only on findings *not*
in the baseline. Fingerprints (`path|code|symbol`) carry no line numbers, so entries survive edits
above them, and they are compared as a multiset — a second copy of a baselined finding is still
reported. `tests/` and `ci/` are fork-authored and enforced at zero; `suite_lint.py` fails if
anything under them ever lands in the baseline.

After merging upstream, refresh and **review the diff** — new entries under fork-authored code are
a real signal, not noise:

```bash
python3 tests/suite_lint.py --update-baseline   # ruff
python3 tests/suite_lua.py  --update-baseline   # luacheck
```

Known upstream findings worth being aware of (left unfixed, deliberately): a duplicate
`clientConnectionLost` in `players/vlc.py` that shadows the debug-logging one, a `ctrl+l` binding in
`syncplayintf.lua` pointing at an undefined `clear_log_buffer`, and a duplicate `mpv-failed-advice`
key in `messages_eo.py`.

## Harness notes (`e2e_harness.py`)

- `MiniClient` speaks the real protocol: Hello, periodic State pings, and — critically — it
  **echoes `ignoringOnTheFly.server`**; without that echo the server drops your playstate reports.
  `role="leader"` follows a scripted timeline; `role="follower"` adopts server-forced pause state.
- `ServerBoot` refuses to start if the port is already occupied — a crashed earlier run leaves a
  zombie server and later runs would silently test stale code.
- Assertions are made on captured, timestamped Chat/Set/State events; every scenario ends with a
  server-stdout traceback scan.
- Always run via `run_all.py` or directly from anywhere — suites put the repo root on `sys.path`
  themselves, so a system-installed syncplay never shadows the working tree.

## Flaky-link harness (`flaky_harness.py`)

Kept separate from `e2e_harness.py` so nothing above is affected by it. `MiniClient` is the right
tool for asserting what the *server* sends; this one exists to show what a *client* does about it,
because on a bad link almost everything that goes wrong is a client-side decision.

- **`FlakyClient` is the real client.** A real `SyncplayClient` (built `__new__` + attributes, the
  same recipe as `suite_lag.py`/`suite_buffer.py`) driving a real `SyncClientProtocol` over a real
  socket, polled at `PLAYER_ASK_DELAY` by a driver that stands in for `askPlayer`. User actions are
  performed on the **player** — `press_pause()` flips the player's own flag — exactly as a keypress
  in mpv is, so the client decides for itself what to put on the wire. `starve()` freezes the
  position while the player still calls itself unpaused: a cache that will not fill.
- **`LaggyLink`** is a TCP proxy adding one-way delay and jitter (seeded, so failures reproduce).
  `blackout(secs)` *holds* traffic rather than dropping it — TCP would have retransmitted it — so
  the only difference between the two blackout profiles is duration, either side of the 12.5 s
  `PROTOCOL_TIMEOUT`.
- **`PROFILES`** — `lan`, `hspa`, `edge` (0.4 s each way: mobile data), `edge_blackout` (8 s
  outage), `edge_outage` (15 s). Add one here rather than hard-coding delays in a scenario.
- **Attribution matters when judging an action.** `FlakyClient.landed()` requires the room change to
  come back with `setBy` equal to us: while the room is ping-ponging (below) *some* later State says
  `paused=True` no matter what you pressed.

### `[KNOWN]` / `[FIXED]`: recording a defect you are not fixing yet

`expect_defect()` prints `[KNOWN]` while a defect still reproduces and `[FIXED]` when it stops, and
the summary shouts about every `[FIXED]` line so it can be promoted to an ordinary `check()` and the
`expect_defect` call deleted — without ever failing the suite in the meantime. **A `[FIXED]` line is
not a pass, it is a to-do.**

The four defects `suite_flaky_e2e.py` was written to measure this way (room ping-pong, swallowed
keypress, overwritten seek, the same ping-pong via the client-side buffer fallback) have since been
fixed, and the checks that replaced them are the regression guard; each was verified to fail against
the old behaviour before being promoted. The mechanism stays for the next one.

Not covered (needs a real mpv): the lua runtime paths — hotkey publish, OSD rendering, track
application against a genuine `track-list`. Those are validated structurally/by simulation here;
smoke-test with two real mpv clients when touching them.
