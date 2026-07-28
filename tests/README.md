# Fork test suites

Test suites for the fork's features (yap timer, pause warning, 1-hour cap, OSD message channel,
server admins, room locking, track proposals, join position guard, join-time state propagation).
Upstream Syncplay has no test suite; these are self-contained scripts, not pytest — each prints one
line per check and a summary, and exits non-zero on failure.

```bash
python3 tests/run_all.py              # everything (~2-3 min; needs free ports 19001-19073)
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
| `suite_joinprop.py` | Join-time propagation of room state: Hello-vs-Set ordering, domain-overlay reset ownership, track proposals queued until the player is up, player-less chat, cache eviction |
| `suite_permrooms.py` | Room startup: permanent rooms created before the port opens, `loadRooms` merging into live rooms instead of replacing them, name/key consistency |
| `suite_lua.py` | `syncplayintf.lua` static checks (declaration order, block balance, render order) + Python-ported simulations (blink timing, layout signatures) |
| `suite_e2e*.py` | Live-server scenarios: real `syncplayServer.py` subprocesses driven by protocol-faithful socket clients |

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

Not covered (needs a real mpv): the lua runtime paths — hotkey publish, OSD rendering, track
application against a genuine `track-list`. Those are validated structurally/by simulation here;
smoke-test with two real mpv clients when touching them.
