<!---
# Copyright (C) 2019 Syncplay
# This file is licensed under the MIT license - http://opensource.org/licenses/MIT

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
-->

# Syncplay
![GitHub Actions build status](https://github.com/Syncplay/syncplay/workflows/Build/badge.svg)

Solution to synchronize video playback across multiple instances of mpv, VLC, MPC-HC, MPC-BE and mplayer2 over the Internet.

> **This is a fork of [Syncplay/syncplay](https://github.com/Syncplay/syncplay).** It adds
> features for people who run a room rather than just join one: pause metering, server admins,
> room locks, and shared track and trusted-domain recommendations. Run this fork's client against
> this fork's server — that pairing is what the features are designed for. Stock clients and
> servers stay compatible, but get a reduced version of it. Upstream's README continues below,
> unchanged.

## What this fork adds

**Use this fork's client build.** It is the intended setup and where the features actually land:
live on-screen displays that update as you watch, the mpv hotkeys, the GUI controls, and the two
publishing actions — recommended tracks and trusted domains — which exist only here, because they
come out of your own player and config. A room where everyone runs it gets the whole thing.

**Stock clients still work, and always will.** That is a deliberate backwards-compatibility
guarantee rather than the target configuration. An unmodified Syncplay 1.5.0 or newer joins a fork
server and behaves normally: the server enforces the rules for everyone regardless of what they
run, and the information reaches those users as ordinary chat messages. What they don't get is the
live displays, the hotkeys, or the ability to publish anything — functional, but noticeably less
than the real experience. Clients older than 1.5.0 see none of it and are unaffected.

| Feature | Enable / use | What it does |
|---|---|---|
| **Yap timer** | `--yap-timer` | Measures how long a room spends paused. Shows the **current pause**, the **per-file total** split into **active** vs **AFK** time (real discussion vs. waiting on someone), and a **drag ratio** — the total as a percentage of the file's runtime (`21% drag`). The per-file tally resets when the file changes or is rewound to the start. Live cyan overlay on mpv-family players; chat for everyone else. |
| **Pause warning** | `--pause-warning-after` / `-interval` / `-message` | A blinking red on-screen warning once a *single* pause passes the threshold, repeating until someone resumes. Custom text can embed `{}` for the live duration. Re-arms on every pause. |
| **AFK state** | `/afk`, **Ctrl+A** in mpv, the **AFK** button in the GUI, or right-click your own name in the user list | A third state alongside ready and not-ready. While anyone is AFK the pause warning is **suppressed** — the yap timer keeps counting, attributing that time to its AFK portion. Ctrl+A pauses the room before marking you away. Clears itself on unpause, seek, chat, ready-change or room switch. |
| **Server admins** | `--admin-password` / `SYNCPLAY_ADMIN_PASSWORD`, then `/admin <password>` | Control playback and playlists in **any** room, including managed ones, without the operator password; set other people ready; use `/osd` anywhere. Modded clients authenticate from their config instead of typing the password. |
| **Room lock** | `/lock`, `/unlock`, or **Ctrl+L** in mpv (`/togglelock`) | Turns a plain room into an admins-only room: the server reverts everyone else's pauses and seeks, exactly as a managed room does. Runtime-only — the lock is gone after a restart or once the room empties. |
| **Recommended tracks** | **Ctrl+T** in mpv or `/tracks` to publish; viewers press **Alt+T** to re-apply | An admin publishes their current audio and subtitle selection as the room default. Clients match it by **track-layout signature, never filename**, so different releases of the same episode still work; a proposal that doesn't match is cached and applied when a matching file loads. Publishing is always explicit — switching tracks never publishes by itself. |
| **Trusted domains** | **Ctrl+D** in mpv or `/domains` (admins and room controllers), or the share checkbox under File → Advanced → Set trusted domains | Shares your trusted-domains list with the room so everyone can follow you to a streaming host. Recipients get them **for that session only** — never written to their config — and can opt out. People who join later receive the last published list. |
| **Room info** | `/info`, `/info full` | Privately reports the room's live state: lock status, recommended tracks, published domains, controllers and admins. `full` adds the server's configuration and limits. |
| **OSD messages** | `/osd` — server admins anywhere, managed-room operators in their own room | A generic channel for one-shot styled or full-ASS announcements on everyone's screen. |
| **Join position guard** | always on, no flag | A joining or rejoining watcher can no longer drag the room back to 00:00. It defines the room position only while it is demonstrably at it, and is otherwise seeked to the room instead. This one lives entirely in the server, so **stock clients benefit too**. |

The mpv hotkeys are script bindings and can be rebound in `input.conf` — for example
`K script-binding syncplay_publish_tracks`. The names are `syncplay_publish_tracks`,
`syncplay_apply_tracks`, `syncplay_publish_domains`, `syncplay_toggle_afk` and
`syncplay_toggle_room_lock`. Admin-gated actions are resolved on the server, so pressing one
without the authority for it is a harmless no-op.

**Documentation:** [Yap timer, pause warning & OSD](docs/yap-timer-and-pause-warning.md) ·
[Server admins](docs/server-admins.md) · [Join position guard](docs/join-position-guard.md)

**Branches:** `master` tracks upstream. `feature/yap-timer` adds the timer, AFK and OSD set.
`feature/mgmt-overhaul` branches from it and adds server admins and everything admin-driven — that
is the branch to use.

**Tests:** upstream ships none; this fork adds its own suites under `tests/`, run with
`python3 tests/run_all.py`.

## Disclaimer: this fork was vibecoded

Essentially all of the fork-specific code here was written by an LLM (Claude Code) from prompts,
with human review that was directional rather than line-by-line. What that means in practice:

- **No warranty, rather more so than usual.** It works in the author's own use and every feature
  ships with a test suite, but no human has read every line, and nobody holds a complete mental
  model of its edge cases.
- **Treat the security-relevant parts with suspicion.** The admin password path, the chat command
  dispatcher and anything parsing data off the wire are where a subtle mistake actually costs you
  something. Don't put a server with admin features on an untrusted network and assume it is
  hardened.
- **None of this goes upstream.** It isn't written to upstream's standards or review process, and
  upstream never asked for it. Bugs in anything listed above belong here, not on the Syncplay
  issue tracker.
- **Bugs are yours to keep.** Issues and pull requests are welcome — but expect the fixes to be
  vibecoded too.

## Official website
https://syncplay.pl

## Download
https://syncplay.pl/download/

## What does it do

Syncplay synchronises the position and play state of multiple media players so that the viewers can watch the same thing at the same time.
This means that when one person pauses/unpauses playback or seeks (jumps position) within their media player then this will be replicated across all media players connected to the same server and in the same 'room' (viewing session).
When a new person joins they will also be synchronised. Syncplay also includes text-based chat so you can discuss a video as you watch it (or you could use third-party Voice over IP software to talk over a video).

## What it doesn't do

Syncplay is not a file sharing service.

## License

This project, the Syncplay released binaries, and all the files included in this repository unless stated otherwise in the header of the file, are licensed under the [Apache License, version 2.0](https://www.apache.org/licenses/LICENSE-2.0.html). A copy of this license is included in the LICENSE file of this repository. Licenses and attribution notices for third-party media are set out in [third-party-notices.txt](syncplay/resources/third-party-notices.txt).

## Authors
* *Initial concept and core internals developer* - Uriziel.
* *GUI design and current lead developer* - Et0h.
* *Original SyncPlay code* - Tomasz Kowalczyk (Fluxid), who developed SyncPlay at https://github.com/fluxid/syncplay
* *Other contributors* - See http://syncplay.pl/about/development/
* *Fork features* - prc94, with Claude Code.
