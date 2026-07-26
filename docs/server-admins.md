# Server Admins

Server admins are users who authenticate against a server-operator password and gain
**controller-level authority over every room** — managed rooms (no room password needed) and
plain rooms alike — plus the ability to **lock** any plain room so that only admins control it.
The feature is entirely server-side: it works with completely unmodified Syncplay clients.

## Enabling

```bash
syncplay-server --admin-password S3cret
# or:
SYNCPLAY_ADMIN_PASSWORD=S3cret syncplay-server
```

No flag → the feature is off and `/admin` reports that it is not enabled.

## Becoming an admin

**Any client (no update needed):** type into Syncplay chat:

```
/admin S3cret
```

The command is intercepted by the server — it never appears in room chat. You get a private
confirmation, and the operator icon appears next to your name for everyone (including in plain
rooms). Admin status lasts until you disconnect.

**Modded clients — automatic login:** set the admin password once and the client authenticates
itself on every connect:

* **GUI:** the *Admin password* field in the connection settings (below the server password);
  leave empty to disable.
* **Config file:** `adminPassword` under `[server_data]` in `.syncplay` / `syncplay.ini`.
* **Command line:** `syncplay --admin-password S3cret`.

Auto-login is sent as a dedicated protocol message (never as chat), so connecting to an old or
unmodified server is a silent no-op — the password cannot leak into room chat.

## What admins can do

* **Control playback everywhere** — pause, seek, and edit the playlist in any room, including
  managed (`+name:code`) rooms, without knowing the room's operator password. Admins also serve
  as the position reference in managed rooms when no operator is present.
* **Set other users ready/not-ready** in any room (this follows from control authority).
* **`/osd` in any room** — send [styled OSD announcements](yap-timer-and-pause-warning.md)
  without being a room operator.
* **`/lock`** — lock the room you are currently in (plain rooms only; managed rooms are already
  restricted). In a locked room only server admins can pause, seek, or change the playlist; the
  server reverts anyone else's attempts, exactly like a managed room does for non-operators.
  Everyone in the room is notified via chat.
* **`/unlock`** — release the lock; the room behaves like a normal plain room again.
* **`/togglelock`** — flip the lock on or off, whichever the room currently is. In mpv this is
  bound to **Ctrl+L** (rebindable, and available as the `toggle-room-lock` script-message), so an
  admin can lock/unlock without leaving the player. Non-admins pressing it just get the usual
  "you are not an admin" reply.

Locked state is **runtime-only**: it does not survive a server restart, and it ends when the room
empties. If the only admin disconnects, the room stays locked (position extrapolates) until an
admin returns and `/unlock`s it.

## Recommended tracks

An admin can publish their **current audio + subtitle selection** as the room's recommended
default. Publication is always explicit — switching tracks never publishes anything by itself.

**Publishing** (admin on an mpv-family player): press **Ctrl+T** in mpv (rebindable via the
`syncplay_publish_tracks` script binding in `input.conf`) or type **`/tracks`** into the Syncplay
chat. The server confirms privately.

**What others get:**

* **Updated mpv-family clients:** the recommendation is applied as the **default** and a
  status-aware notice appears at the lower middle of the screen — "Applied audio #2 eng,
  subtitles off - recommended by X" when it took effect, or "X recommends … (applies when a
  matching file loads)" when their current file doesn't match yet. Application happens
  immediately if their current file has the **same track layout**, and again on every
  file they load that matches the layout (e.g. the next episode). Users can freely switch tracks
  afterwards — a manual choice sticks for the current file, exactly like normal mpv track cycling —
  and can return to the recommendation at any time with **Alt+T** (rebindable via the
  `syncplay_apply_tracks` script binding; shows why nothing happened if no recommendation was
  received or the layout doesn't match).
* **Other players / legacy clients:** the recommendation as a chat line, re-posted whenever the
  room's file changes.

Matching is **by track layout, never by filename** — different releases of the same content with
identical audio/sub layouts match; unrelated files are left untouched. Users with a different
file loaded (slow loads, browsing) are handled gracefully: the proposal is stored and applied
when a matching file finally loads.

**Per-room layout cache.** The server remembers a recommendation for **each distinct track
layout** it has seen in the room — not just the most recent one. Publish tracks for a show, watch
something else, then come back to that show (or any file with a matching layout) and the earlier
recommendation **re-applies automatically, with no need to publish again**. Late joiners and
room-switchers receive the whole cache, so they too auto-apply on any remembered layout. Details:

* Re-publishing for a layout that is already cached **replaces** its entry (last write wins).
* The cache holds up to **32 layouts per room** (oldest evicted first) and is keyed only on the
  layout signature, so two unrelated releases that happen to share an identical audio/sub layout
  are treated as the same — the newer recommendation wins.
* It **survives the room emptying** (unlike before), but is held in memory only: a **server
  restart** clears it. Legacy (non-mpv) clients still only receive the latest recommendation as
  chat, since the server can't match layouts on their behalf.
* A recommendation published **without** a layout signature is not cacheable, but is still
  delivered to joiners alongside the cached layouts as the room's latest.
* If your player is still starting up when you join, the recommendation is **held and applied once
  the player is ready** rather than lost.

## Trusted domains

Syncplay only auto-switches the room to a streamed **URL** when its domain is in each viewer's
**trusted domains** list (and "only switch to trusted domains" is enabled). An admin can share the
trusted-domains list **from their own client** so the room can follow along to the operator's
streaming host.

**Publishing** (an admin or a room controller, any updated client): press **Ctrl+D** in mpv
(rebindable via the `syncplay_publish_domains` script binding) or type **`/domains`** into the
Syncplay chat. This posts *your client's* current trusted-domains list to the room; the server
confirms privately. Room controllers are treated the same as admins here — an admin is simply a
controller in every room.

**Auto-sharing on edit (GUI):** the *Set trusted domains* dialog (File → Advanced → Set trusted
domains) has a **"Share these trusted domains with everyone in the room"** checkbox, enabled only when
you have admin or controller authority. Ticking it publishes the list immediately on OK and
re-publishes automatically whenever you change your list for the rest of the session (this also
covers the *"Add … as trusted domain"* context-menu action). The preference is **session-only** — it
resets to off on the next launch. Newly joining users still receive the last-published list
automatically, so there is no separate "share with new users" toggle.

**What others get:**

* **Updated clients:** the shared domains are **added** to their own trusted list **for the current
  session only** (never written to their config), with a brief notification. They can opt out — see
  below.
* **Legacy clients:** an informational chat line listing the domains (they can add them manually).

Shared domains are **merged, never replacing** a user's own list, and are **dropped on disconnect**
(on each client, they are session-only and never written to config). This only affects clients that
have "only switch to trusted domains" enabled.

Server-side the published list is remembered **per room**, on the same terms as the track cache: it
**survives the room emptying**, so the next session in that room still gets the domains without an
admin re-publishing, and it is held in memory only — a **server restart** clears it. Re-publishing
replaces the room's entry (last write wins).

**Delivery to joiners:** the server hands the room's remembered domains (and any remembered track
layouts) to a joining client **after** its handshake reply, never before — a client resets its
session-only copy of that state while processing the handshake, so anything delivered ahead of it
would be silently discarded. Room switches are unaffected, since no handshake is involved.

**Opting out:** each client has an *"Accept trusted domains shared by server admins"* setting (in
the Trusted domains section of the settings dialog, on by default). Unticking it makes that client
ignore shared domains entirely; re-ticking it applies the current room list again without
reconnecting.

## Security notes

* The password travels **in plain text** inside the chat command / auth message — run the server
  with `--tls` if admins connect over untrusted networks.
* The client stores `adminPassword` in plain text in its config file.
* A typo in the command (e.g. `/admn S3cret`) is **not intercepted** and will be posted to the
  room as ordinary chat — retype carefully. (The exact `/admin` token is always intercepted,
  correct password or not.)
* There is no rate limiting on `/admin` attempts — use a strong password.
* Only share the admin password with people you trust with every room on the server.
