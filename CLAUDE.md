# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Syncplay synchronizes media playback (position + play/pause state) across multiple media players
over the internet: everyone in the same server "room" watches the same thing at the same time. It is
a Python 3 / Twisted application with a Qt (PySide2/PySide6) GUI, split into a **client** (drives a
local media player) and a **server** (relays authoritative room state). Supported players: mpv,
mpv.net, VLC, MPC-HC, MPC-BE, mplayer2, IINA, Memento.

**This repo is a fork** (`prc94/syncplay`, remote via SSH with a repo-local `core.sshCommand`) that
adds server-power features on top of upstream. `master` tracks upstream; `feature/yap-timer` adds
the pause-tracking/OSD feature set; `feature/mgmt-overhaul` (branched from it) adds server admins
and everything admin-driven. **The hard invariant of every fork feature: full interoperability with
stock clients and servers** — new behavior is opt-in, feature-flagged, and always degrades to chat.

### Fork features (docs in `docs/*.md`)
- **Yap timer** (`--yap-timer`): tracks room pause time (current + per-file total), live mpv overlay.
- **Pause warning** (`--pause-warning-after/-interval/-message`): blinking OSD after a long pause.
- Both give up after a 1-hour pause (`YAP_TIMER_MAX_PAUSE`, sticky `_yapExpired` flag).
- **Generic OSD channel** (`/osd`, `SyncFactory.sendOSDMessage`): styled/full-ASS one-shot messages.
- **Server admins** (`--admin-password`/`SYNCPLAY_ADMIN_PASSWORD`): `/admin <pw>` in chat or
  auto-auth from modded clients (`adminPassword` client config, GUI field, `Set:adminAuth`);
  controller authority everywhere, `/lock`//`/unlock` on plain rooms.
- **Track proposals**: admin publishes recommended audio/sub tracks (Ctrl+T in mpv or `/tracks`),
  applied by layout-signature match, per-watcher chat reminders for legacy clients.
- **Join position guard** (always on, no flag): a watcher only defines the room position while it
  is demonstrably at it (`Watcher._positionEstablished`, `Room.getPositionReferences`); anyone
  else is seeked to the room (`SyncFactory.pullWatcherIntoSync`) instead of dragging it to 00:00.
  Fixes joins/rejoins/player restarts rewinding the room. Server-side, so stock clients get it too.

## Running & building

Upstream has **no test suite and no linter config**; CI (`.github/workflows/build.yml`) only builds
installers. This fork adds its own suites in `tests/` (see "Testing the fork" below).

```bash
python3 syncplayClient.py           # GUI client (--no-gui for console)
python3 syncplayServer.py           # server (default port 8999); --help for all flags
pip install -r requirements.txt -r requirements_gui.txt
python3 buildPy2exe.py / buildPy2app.py py2app / ci/deb-*.sh   # installers
make -f GNUmakefile install-client|install-server              # Linux install (NOT the stub Makefile)
docker build -t syncplay-server .                              # server container (Dockerfile in repo)
```

The version lives in `syncplay/__init__.py` (`version`, `milestone`, `release_number`); build
scripts and the update check text-parse it — keep the format stable.

## Architecture

### Entry flow & layers
`syncplayClient.py` → `ep_client.py` → `SyncplayClientManager.run()` (lazy-imports `SyncplayClient`
so the right reactor is installed first). `syncplayServer.py` → `ep_server.py` → `SyncFactory`.
Three client layers, wired at startup: **UI** (`ui/gui.py` MainWindow or `ui/consoleUI.py`, always
accessed through `UiManager` in `client.py`) — **core** (`client.py:SyncplayClient` + `UiManager`,
`SyncplayUserlist`, `SyncplayPlaylist`) — **player controller** (`players/*.py`, all subclassing
`basePlayer.py:BasePlayer`; registry in `players/__init__.py`; `mpv.py` is the reference impl and
the base class of mpv.net/IINA/Memento — they inherit its capabilities).

### Networking (`protocols.py`)
Newline-delimited JSON over TCP/TLS. `JSONCommandProtocol` dispatches top-level keys `Hello`,
`Set`, `List`, `State`, `Error`, `Chat`, `TLS` to `SyncClientProtocol` / `SyncServerProtocol`.
Server: `SyncFactory` → `RoomManager`/`PublicRoomManager` → `Room`/`ControlledRoom` → `Watcher`.
- **`State`** is a 1 s per-watcher heartbeat (`Watcher._scheduleSendState`) — piggyback *continuous*
  data on it (yap timer, pause warning fields).
- **`Set`** is for one-shot commands both directions — use it for events (osdMessage,
  trackProposal, adminAuth). **Both sides silently ignore unknown `Set`/`State` keys** (if/elif
  chains, no else) — this is what makes cross-version compat free; it is load-bearing, keep it.

### Core sync — "ignoring on the fly" (read before touching sync code)
Client polls the player and sends `State`; server merges to authoritative room state and pushes
back; the client seeks/pauses/nudges speed to converge (thresholds in `constants.py`). To stop a
server-directed change from echoing back as a user action, both sides keep
`clientIgnoringOnTheFly`/`serverIgnoringOnTheFly` counters exchanged inside `State`
(`handleState`/`sendState` in both protocol classes). Any test client must echo the server counter
or its reports are dropped.

### Authority model (fork)
All control authority funnels through exactly three checkpoints — override behavior only there:
`Room.canControl` (free-for-all unless `_locked`), `ControlledRoom.canControl` (controllers dict),
`Watcher.isController()` (managed-room operator or `_isAdmin`). Plain `Room` setters
(`setPaused/setPosition/setPlaylist*`) are gated on `canControl(setBy)`; rejected changes are
auto-reverted by `forcePositionUpdate`'s non-controller branch. `getPosition` picks the
position-reference watcher — admins are included/preferred where relevant. Admin status is
per-connection (`Watcher._isAdmin`), granted by `SyncFactory.authAdmin`, displayed on stock
clients by reusing the `sendControlledRoomAuthStatus` broadcast.

### Chat command dispatchers (two of them — don't confuse)
- **Server-side** (`SyncFactory.sendChat`): exact first-token match (`/osd`, `/admin`, `/lock`,
  `/unlock`, `/tracks`, `/afk`) *before* chat truncation; an unknown `/foo` is **rejected** — the
  sender gets a private `unknown-command-chat-message` warning (echoing only the command token,
  never the args, so a mistyped `/admin` can't leak) and it is **not** broadcast to the room.
  Errors/acks go as private chat to the sender. (Consequence: literal chat starting with `/`
  no longer reaches the room — the old `//`-escape produces `/foo` on the wire, which is warned.)
- **Client-side** (`consoleUI.executeCommand`, driven by `constants.COMMANDS_*`): the single
  dispatch point for slash-commands typed in the GUI chat box, mpv chat overlay, and console —
  mpv chat starting with `/` becomes `executeCommand(...)`, it never reaches `client.sendChat`.
  A command it does **not** recognize is forwarded to the server as `"/" + normalized` chat, so
  server-side commands (`/lock`, `/admin`, `/osd`, `/unlock`) work from every input surface;
  known local commands (`/afk`, `/list`, `/pause`, `/help`, …) are still handled on the client.

### mpv ↔ lua plumbing (`players/mpv.py` + `resources/syncplayintf.lua`)
- Client→lua: `sendLine(["script-message-to", "syncplayintf", "<msg>", jsonArg])`.
- lua→client: `mp.commandv('print-text', '<Marker>payload</Marker>')` parsed in
  `mpv.py:_handleUnknownLine` → hand results to the reactor with `reactor.callFromThread`
  (listener runs on its own thread). Existing markers: `<chat>`, `<paused=…>`, `<eof>`,
  `<SyncplayUpdateFile>`, `<SyncplayTrackProposal>`.
- **`_sanitizeText` escapes `{`/`}`** — it protects the ASS layer but destroys ASS markup; bypass
  it for JSON payloads (the JSON IPC transport is binary-safe) and escape lua-side instead.
- **Lua gotchas:** file-scope `local utils = require 'mp.utils'` is declared *late* in the file —
  closures defined above it must `require 'mp.utils'` in-function or they capture a nil global.
  Keep declaration-before-use for all file-scope locals. `mp.set_osd_ass` treats real `\n` as
  event separators (one line per independently-positioned `{\anN}` element); use `ass_escape()`
  for untrusted text. OSD elements are globals + a `process_*` function called from `chat_update`;
  new fork elements render *below* all original entries (order: alert → notification → chat →
  yap → pause warning; generic osd messages are separate self-positioned lines).

## The fork feature pattern (follow this recipe for new features)

Every fork feature ships the same way; the touchpoint checklist:
1. **`constants.py`** — tunables, command tokens, limits, defaults.
2. **`server.py`** — `Room` runtime state (cleared in `removeWatcher`'s `isEmpty()` block; NOT
   persisted to the rooms DB), `SyncFactory` handler with validation/clamps + private-chat
   errors/acks, `Watcher.sendX` → `sendSet({...})`.
3. **`protocols.py`** — server `handleSet` branch; client `handleSet` branch → `ui.X`; client
   `sendX` method.
4. **`client.py`** — capability flag from the *player class* (`getattr(playerClass, "xSupported",
   False)` in `__init__` — the player instance starts async, never read it at feature time) +
   `features["x"]` in `getFeatures()`; `UiManager` method (log via `showMessage`, forward to
   player with no-player guard, re-clamp everything off the wire — don't trust the server).
5. **`basePlayer.py`** — `xSupported = False` + docstring'd no-op; **`mpv.py`** — flag True + impl.
6. **`syncplayintf.lua`** — element/handler, pcall-guarded JSON, per-field sanity defaults.
7. **Routing/degradation** — capable clients get `Set`/`State` data; everyone else gets chat via
   `sendChatMessage` (auto-gated ≥1.5.0), using `skipIfSupportsFeature="x"` or explicit
   `supportsFeature()` branching. Old clients ignore the new keys — zero risk.
8. **`messages_en.py`** — all user-facing strings as i18n keys (suffix conventions:
   `-notification/-error/-argument/-tooltip/-message`; GUI tooltips are looked up by *lowercased*
   widget objectName + `-tooltip`). English only; other languages auto-fallback.
9. **`docs/*.md`** — user/operator docs; **tests** (below).

Server flags: argparse in `server.py:ConfigurationGetter` (+ env default where sensible) →
`ep_server.py` → `SyncFactory.__init__` (positional — append at the end). Client settings:
defaults dict + `_iniStructure` + argparse (+ `_overrideConfigWithArgs` key mapping) in
`ui/ConfigurationGetter.py`; GUI fields mirror the `password` field's manual-binding pattern
(`LOAD_SAVE_MANUALLY_MARKER` objectName + explicit save in `GuiConfiguration.py`).

**Hard-won design lessons:** `_getRoomFileKey`/`getSetBy()` can point at a watcher with no file —
don't hang room-wide file-change logic on it; per-watcher tracking (see
`_remindTrackProposalOnFileChange`) is more robust. Match files by **track-layout signature,
never filename** (users watch different releases). Timers armed with `callLater`/`LoopingCall`
must be cancelled in the room-empty cleanup and guarded against firing on emptied rooms.

## Testing the fork

Suites live in `tests/` — `python3 tests/run_all.py` (`--unit-only` for the ~15 s path); see
`tests/README.md`. **Every new fork feature or fix ships with its suite committed there**, following
these patterns (they found real bugs every time):
- **Unit style:** instantiate `Room`/`SyncFactory`/protocol classes directly (`__new__` + set the
  few attrs needed); fake watchers implementing
  `getName/isAdmin/supportsFeature/sendChatMessage/getPosition/isPositionEstablished` (the last two
  are required by `Room.getPosition`'s reference filter — return `True` for a settled watcher);
  backdate `_yapPauseStartedAt`-style clocks instead of sleeping.
- **E2E style:** boot the real `syncplayServer.py` as a subprocess and drive it with a
  protocol-faithful socket client (Hello → State pings; **echo `ignoringOnTheFly.server`** or your
  playstate reports are dropped; followers adopt server-forced pause state). Assert on captured
  Chat/Set/State events with timestamps; scan server stdout for tracebacks.
  **Guard the port before booting** — a crashed run leaves a zombie server and later runs silently
  test stale code.
- **Lua:** no interpreter available — validate structurally (declaration order, block balance,
  handler registration, render order) and port decision logic (blink duty cycle, layout-signature
  matching) to Python for simulation.
- **GUI:** PySide6 IS importable here; `QT_QPA_PLATFORM=offscreen` lets you construct the real
  `ConfigDialog` and assert on live widgets.

### Environment quirks (this machine)
- `python3` only (no `python`, no pip); **system-installed syncplay shadows the repo** — always run
  tests with `PYTHONPATH=/path/to/repo` *from the repo* (a bare `PYTHONPATH=.` from elsewhere
  imports the system copy and fails confusingly).
- No docker, no lua interpreter. `pkill -f "syncplayServer.py"` matches your own shell's command
  line — use `pkill -f "syncplayServer[.]py"`.

## Conventions to follow

- **All user-facing strings are i18n keys** in `messages_en.py` via `getMessage()` — never literals.
- **Wire-protocol changes must be version-gated or unknown-key-safe.** Upstream gates on
  `*_MIN_VERSION` constants + `utils.meetsMinVersion`; fork features gate on the featureList
  handshake. Clients and servers of different versions are expected to interoperate.
- **`constants.py` is the home for every tunable and magic number** (use `getValueForOS` for
  OS-specific values).
- **The reactor gotcha:** in GUI mode `qt5reactor.install()` runs from `ConfigurationGetter` —
  nothing importing `twisted.internet.reactor` may load before the right reactor is installed
  (that's why `clientManager.py` imports lazily). `ConsoleUI` runs in a daemon thread and must
  hand work to the reactor thread.
- **`syncplay/vendor/` is bundled third-party code** — avoid editing.

## Layout quick reference

- `syncplay/client.py`, `server.py`, `protocols.py` — the three core modules.
- `syncplay/players/` — controllers (`mpv.py` = reference + base of the mpv family).
- `syncplay/ui/` — `gui.py`, `consoleUI.py` (command dispatch), `ConfigurationGetter.py` (client
  config: defaults/ini/CLI), `GuiConfiguration.py` (Qt settings dialog).
- `syncplay/resources/syncplayintf.lua` — the mpv-side half of every mpv feature.
- `syncplay/messages*.py` — i18n; `syncplay/constants.py` — all constants.
- `docs/yap-timer-and-pause-warning.md`, `docs/server-admins.md`, `docs/join-position-guard.md`
  — fork feature docs.
- `Dockerfile`/`.dockerignore` — server container; `ci/`, `buildPy2exe.py`, `buildPy2app.py`,
  `GNUmakefile` — packaging.
