# Yap Timer, Pause Warning & OSD Messages

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
  (new playlist entry or a new file opened), and also when the file is
  **rewound to the start** (a controller seeks back to the very beginning) —
  replaying from scratch starts the tally fresh. The total is broken down into
  **active** time (paused while *nobody* was [AFK](#afk-state)) versus **AFK**
  time (paused while at least one person was away) — so you can tell real
  discussion apart from waiting on someone who stepped away.
* **Drag ratio** — the per-file total expressed as a percentage of the current
  file's runtime (`N% drag`), so you can see how much the film is being stretched
  by pauses (`25% drag` = a quarter of the runtime spent paused). Shown whenever
  the room knows the file's length; omitted if no one reports a duration.

### Enabling

```bash
syncplay-server --yap-timer
```

That's all — no client configuration exists or is needed.

### What users see

**Updated clients using mpv, mpv.net, IINA or Memento** get a live overlay in
the player (cyan text) that updates every second while paused. The current
pause is the main line, with the per-file total broken out on the row beneath:

```
Yap timer: 00:42
(total 12:52 - 09:00 active / 03:52 AFK) — 21% drag
```

On resume, the overlay briefly shows the final tally and then hides:

```
Total yapped this file: 12:52 (09:00 active / 03:52 AFK) — 21% drag
```

The `active` and `AFK` figures always add up to the total; when nobody has been
AFK on the current file, the AFK figure is simply `00:00`.

**Everyone else** (VLC, MPC-HC/BE, mplayer, console users, and clients older
than this feature) sees the same information as chat messages, which Syncplay
already displays in the player OSD / chat area:

```
<Alice> paused - yap timer running
<Alice> still paused - 01:00 this pause (03:10 total - 02:10 active / 01:00 AFK) — 5% drag   (every 60 s)
<Alice> unpaused - yapped for 02:34 (05:44 total this file - 04:44 active / 01:00 AFK) — 10% drag
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

## AFK state

A third per-user state alongside *ready* and *not ready*. Marking yourself
**AFK** ("away from keyboard") tells the room you have stepped away. While
**anyone** in a room is AFK:

* The room's **pause warning is suppressed** — no blinking OSD, no chat
  reminders. The warning is only *silenced*, not disarmed: it resumes
  automatically (within about a second) once the last AFK person returns or
  leaves the room, if the pause is still over the threshold.
* The **yap timer keeps counting** exactly as normal — the pause is still time
  the room spent paused, so it is still measured and displayed. That time is
  attributed to the timer's **AFK** portion (rather than **active**) for as long
  as anyone in the room is AFK, so the split reflects who the room was waiting
  on. Toggling AFK mid-pause moves subsequent seconds between the two portions.

An AFK user is also shown as **not ready** (so autoplay and "everyone ready"
correctly wait for them), marked with a clock icon in the user list, and
announced to the room ("*name* is now AFK"). In the same persistent OSD warning
that lists who is *not ready*, AFK users are pulled onto their own **`AFK:`
line** rather than lumped in with the plain not-ready names — so anyone can see
at a glance who has stepped away as long as the readiness warning is on screen.

### Toggling AFK

Any of these toggle your own AFK state:

* Press **`Ctrl+A`** in mpv. Going AFK this way **pauses the room first** (so you
  don't leave it playing to an empty seat) and then marks you AFK; if the room is
  already paused it just marks you AFK. Pressing it again clears AFK (and leaves
  the pause state as-is). The binding is a normal mpv key binding, so you can
  remap it in your `input.conf` with `script-binding syncplay_toggle_afk`.
* Type **`/afk`** in the chat box, the mpv chat overlay, or the console client.
* Click the **AFK** button next to the *Ready* button (GUI), or use the
  **right-click menu** on your own name in the user list.

There is no server flag — AFK is always available (it needs a server that
advertises the `afk` feature; see compatibility below).

### Automatic clear

AFK is meant to be transient, so it clears itself the moment you show activity:

* unpausing or seeking (but **not** pausing — stepping away often means pausing),
* changing your ready state,
* sending a chat message,
* switching rooms.

Returning from AFK does **not** restore your previous ready state — you re-ready
yourself when you are actually back.

### Compatibility

* On a **modded server**, updated clients get the full experience (icon,
  button, live suppression). **Stock/older clients** in the same room still see
  the AFK user as *not ready* and receive a plain chat line ("*name* is now
  AFK"); they can even toggle their own AFK by typing `/afk` (the server
  understands the command). Nobody is required to update.
* On a **stock server** (no `afk` feature), the AFK button is disabled and
  `/afk` reports that the server does not support it — nothing is sent, so
  there is no risk to interoperability.

---

## OSD messages (`/osd`)

A generic channel for putting **styled announcements** on everyone's screen.
Operators (controllers) of **managed rooms** type a `/osd` command into the
Syncplay chat; the server intercepts it and broadcasts an OSD message to the
room instead of a chat line. Always available — no server flag needed.

```
/osd [ass=1] [dur=secs] [colour=#RRGGBB] [pos=POSITION] [size=N] message text
```

| Option | Meaning | Default / limits |
|---|---|---|
| `dur=N` | Display time in seconds | 5 (0.5–60) |
| `colour=#RRGGBB` | Text colour | `#FFFF00` |
| `pos=...` | One of `top-left`, `top-center`/`top`, `top-right`, `middle-left`, `center`, `middle-right`, `bottom-left`, `bottom-center`/`bottom`, `bottom-right` | `top-center` |
| `size=N` | Font size (ASS units on a 1920×1080 canvas) | 50 (10–150) |
| `ass=1` | Treat the message as **raw ASS markup** (see below) | off |

Examples:

```
/osd Movie starts in 2 minutes!
/osd dur=10 colour=#FF4444 pos=bottom size=70 Last call for snacks
/osd ass=1 dur=15 pos=top {\b1\1c&H0000FF&}INTERMISSION{\b0}\N{\fs30}back in {\i1}10 minutes{\i0}
```

### Full ASS enrichment (`ass=1`)

With `ass=1` the message text is passed to the player's renderer **verbatim**,
so every libass override tag works: bold/italic (`\b1`, `\i1`), inline colours
(`\1c&HBBGGRR&` — note BGR order), borders and shadows (`\bord`, `\shad`),
fonts (`\fn`), per-part sizes (`\fs`), rotation (`\frz`), animation (`\t`),
line breaks (`\N`), even vector drawings (`\p1`). The `colour`/`pos`/`size`
options still apply as the starting style; inline tags override from there.
Without `ass=1`, braces and backslashes are escaped and display literally.

### Who sees what

* **Updated mpv / mpv.net / IINA / Memento clients:** the styled message at the
  chosen position, up to 5 concurrent messages, each with its own timer. The
  plain-text version also appears in their chat log.
* **Everyone else (≥ 1.5.0):** the message as a chat line with ASS tags
  stripped (`INTERMISSION back in 10 minutes`).

### Notes

* `/osd` requires being an **authenticated controller of a managed room**
  (rooms named `+name:code`) or a [server admin](server-admins.md) (who can
  use it in any room); anyone else gets a private error message.
* Sent through chat, so `--disable-chat` disables the command (the server-side
  Python API `SyncFactory.sendOSDMessage(...)` still works for custom mods).
* Long ASS payloads may hit the chat length limit senders adopt from the
  server — raise `--max-chat-message-length` if needed. The OSD text itself is
  capped at 1000 characters.
* **Trust note:** `ass=1` lets a room operator draw arbitrary overlays (up to
  60 s per message) on viewers' players. Only share operator passwords with
  people you trust.

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
