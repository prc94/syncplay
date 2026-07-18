# Yap Timer & Pause Warning

Two optional, server-side features for measuring and managing time spent paused
("yapping") in a room. Both are **off by default**, are enabled by the server
operator, and work with **unmodified Syncplay clients** (1.5.0 or newer) — no
client update is required. Clients running an updated build with an mpv-family
player additionally get a nicer live on-screen display.

---

## Yap Timer

Tracks how much time a room spends paused and shows it to everyone.

* **Current pause** — how long the room has been paused right now.
* **Total for the current file** — the sum of all pauses since this file
  started playing. The total **resets automatically when the file changes**
  (new playlist entry or a new file opened).

### Enabling

```bash
syncplay-server --yap-timer
```

That's all — no client configuration exists or is needed.

### What users see

**Updated clients using mpv, mpv.net, IINA or Memento** get a live overlay in
the player (cyan text) that updates every second while paused:

```
Yap timer: 00:42 (total 12:52)
```

On resume, the overlay briefly shows the final tally and then hides:

```
Total yapped this file: 12:52
```

**Everyone else** (VLC, MPC-HC/BE, mplayer, console users, and clients older
than this feature) sees the same information as chat messages, which Syncplay
already displays in the player OSD / chat area:

```
<Alice> paused - yap timer running
<Alice> still paused - 01:00 this pause (03:10 total)      (every 60 s)
<Alice> unpaused - yapped for 02:34 (05:44 total this file)
```

Messages are attributed to the user who paused. Clients older than 1.5.0
(pre-chat protocol) receive nothing and are otherwise unaffected.

---

## Pause Warning

Warns the whole room when a **single pause** runs longer than a limit set by
the server operator — a nudge to get playback going again.

### Enabling

```bash
syncplay-server --pause-warning-after 300
```

| Option | Meaning | Default |
|---|---|---|
| `--pause-warning-after <seconds>` | Warn once a single pause exceeds this many seconds. Unset/0 = feature off. | off |
| `--pause-warning-interval <seconds>` | How often to repeat the chat warning while still paused. | same as `--pause-warning-after` |
| `--pause-warning-message <text>` | Custom warning text. An optional `{}` is replaced with the current pause duration (live). Text without `{}` is shown verbatim. | `Paused for {} - please resume when ready` |

Examples:

```bash
# Warn after 5 minutes, then nag every minute
syncplay-server --pause-warning-after 300 --pause-warning-interval 60

# Custom message with the live duration filled in
syncplay-server --pause-warning-after 120 \
    --pause-warning-message "We have been paused for {} - resume when ready!"

# Fixed text, no duration
syncplay-server --pause-warning-after 600 --pause-warning-message "RESUME NOW"
```

### What users see

**Updated clients using mpv, mpv.net, IINA or Memento** get a **blinking red
warning** in the player that stays on screen (blinking about once per second)
until the room resumes:

```
Paused for 05:12 - please resume when ready      (blinking)
```

**Everyone else** gets the warning as a chat line when the threshold is
crossed, repeated every `--pause-warning-interval` seconds until resume.

### Behavior details

* The timer is **per pause**: it re-arms every time the room pauses, and a
  pause shorter than the threshold produces no warning at all.
* The warning stops immediately when anyone resumes playback.
* Works independently of the yap timer — enable either or both.

---

## The one-hour give-up

A pause that lasts a whole hour is not a discussion — the session is stale.
When a **single pause exceeds 1 hour**, both features give up:

* The yap timer **resets** (the per-file total is wiped and the runaway pause
  is discarded) and **turns off** — the overlay disappears, chat updates stop,
  and no "unpaused - yapped for ..." summary is sent on the eventual resume.
* The pause warning **stops** — no more blinking, no more chat reminders.

Everything stays quiet until the **next pause begins**, which starts fresh
(counting from 00:00 with a total of 00:00).

Note: because of this cap, a `--pause-warning-after` threshold of 3600 seconds
or more will never fire.

---

## Notes for server operators

* Both features are **per room**; nothing leaks between rooms (including with
  `--isolate-rooms`).
* Both work in controlled (managed) rooms; only pause/unpause actions by room
  operators drive the timers there, matching who is allowed to pause.
* The chat fallback is delivered even when user chat is disabled with
  `--disable-chat` — enabling the feature is the operator's explicit opt-in to
  these messages.
* In controlled rooms and on public servers, remember these produce extra chat
  lines for non-updated clients; tune `--pause-warning-interval` (and the yap
  timer's built-in 60 s "still paused" cadence) to taste before enabling on a
  busy server.

## Compatibility summary

| Client | Yap timer | Pause warning |
|---|---|---|
| Updated build + mpv / mpv.net / IINA / Memento | Live overlay (auto-updating) | Blinking red OSD |
| Any Syncplay ≥ 1.5.0 (no update needed) | Chat messages | Repeated chat messages |
| Syncplay < 1.5.0 | Nothing (unaffected) | Nothing (unaffected) |

How it works: updated clients advertise `yapTimer` / `pauseWarning` in their
feature list; the server sends those clients live data on the existing State
tick and skips them when broadcasting the chat fallback, so nobody sees the
same information twice.
