# Join Position Guard

Stops a user who joins (or rejoins) a room from rewinding everybody else to the start of the file.
The fix is **server-side and needs no client support**: completely unmodified Syncplay clients get
the corrected behaviour just by connecting. A small client-side half is included as well, so this
fork's clients also protect themselves when connecting to a stock server.

There is nothing to enable and nothing to configure — it is always on.

## The problem

Syncplay defines a room's position as the position of its **least advanced** watcher, so that the
room waits for whoever is lagging. Somebody who has just joined is, briefly, the least advanced
watcher in the room: their player is sitting at `00:00` because the file has only just been opened.

The room would therefore adopt `00:00` as its position and push it to everybody. Each of those
clients then sees a huge backwards difference and rewinds itself (`rewindOnDesync`). One person
joining a room half an hour into a film sent everyone in it back to the opening titles.

Two separate defects combined to produce it:

* **Nothing pulled the newcomer forward.** `Watcher.setRoom` does ask for a forced seek to the room
  position, but on a fresh connection that message is discarded — `Watcher.sendState` is gated on
  `isLogged()`, and `SyncServerProtocol.handleHello` only sets `_logged` *after* `addWatcher` has
  returned. Nothing else seeks a joiner forward either: `fastforwardOnDesync` deliberately skips
  clients that are entitled to control the room, which in an ordinary room is everyone.
* **Nothing stopped the newcomer pulling everyone back.** The only guard on the
  `min()`-over-watchers in `Room.getPosition` was "a watcher with no file cannot be the minimum" —
  which stops applying the moment the joiner announces its file, while its player is still at
  `00:00`.

## The rule

> A watcher may only define the room position while it demonstrably **sits at** that position.
> Anything else is seeked to the room instead. Explicit seeks always win.

A watcher that has not proved this is *unestablished*: it is excluded from the room's position
reference set, so `min()` cannot pick it, and `forcePositionUpdate` broadcasts the room's position
rather than the watcher's when it pauses. Meanwhile the server seeks it towards the room.

A watcher becomes established when any of these holds:

| Condition | Why |
| --- | --- |
| It reports a position within `JOIN_SYNC_TOLERANCE` of the room | It has caught up — the normal path |
| It explicitly seeks (`doSeek`), and may control the room | A seek is deliberate intent and must be honoured, including a seek to `00:00` |
| The room has no established watcher and no meaningful position | Nobody is watching anything yet, so this watcher defines the room |
| The room has no established watcher and the stored position stays out of reach for `JOIN_PULL_GRACE` | The position is a ghost (everyone left) or unreachable (persistent room restored against a shorter file) — stop fighting the only person actually present |

A watcher stops being established when it **teleports backwards** — a jump of more than
`POSITION_TELEPORT_GUARD` on an *unchanged* file with no seek. That is a player restart, not
playback, and it used to rewind the room in exactly the same way as a join. Gradual desync (a
buffering or stuttering player, however far behind it drifts) is *not* a teleport and keeps
upstream's semantics: the room still waits for the slowest watcher.

### Changing file is not a teleport

Restarting at `00:00` because a *different* file was loaded — the playlist advancing at the end of
an episode, or anyone opening something else — is a legitimate jump backwards, so the first report
from a newly announced file always keeps its reference status.

The catch is *which* report that is. It is emphatically not "the next State to arrive":

* the end-of-file pause is a state change, so the client starts ignoring on the fly and its next
  States carry **no playstate at all** (`SyncClientProtocol.sendState` omits it) — the server still
  processes them;
* a status poll issued while the new file is loading reports the **old file's** position.

Both used to consume a one-shot "file changed" flag, after which the genuine `00:00` report looked
like a backwards teleport, unestablished the watcher and got it force-seeked to the room position —
which was still the end of the file it had just finished. With two same-length episodes that lands
exactly at the end of the new one; with a longer next file it lands at the old file's end timestamp
mid-episode; with a shorter one it clamps to EOF and can advance the playlist again.

So the flag is a **latch**, not a one-shot: armed by `Watcher.setFile`, held across playstate-less
States and across reports the old file's position can still account for (within
`JOIN_SYNC_TOLERANCE` of where that file would be now), and released by the first report the old
file cannot explain — or by `FILE_CHANGE_REPORT_GRACE` expiring, so a file that resumes exactly
where the last one stopped cannot leave the teleport guard disabled forever.

"Meaningful position" distinguishes a room that is genuinely somewhere from one whose position is
just an unused default zero. It becomes true when an established watcher defines the position, when
somebody entitled to control the room seeks it, or when the room is restored from the rooms
database; it is cleared when a non-persistent room empties.

## Pulling a watcher into sync

An unestablished watcher is sent a forced seek to the room position:

* immediately after the `Hello` handshake, for anyone whose player was already open on the file;
* **the moment it announces a file** — the first point at which a client can actually act on a
  seek, and the point at which it would otherwise start reporting `00:00` at the rest of the room;
* on subsequent state reports while it is still adrift.

Pulls are rate limited to one per `JOIN_PULL_INTERVAL`, are never sent while a previous forced
update is still unacknowledged (each one bumps the `ignoringOnTheFly` counter, and stacking them
would leave the server ignoring that client's reports), and are never sent towards a room that has
no meaningful position — joining an empty room must not yank you to `00:00`.

## Client side

`SyncplayClient._syncNewlyLoadedFileToRoom` seeks a newly loaded file to the room position, so this
fork's clients recover by themselves on servers that do not have the guard. It only applies inside
the *join window* — from connecting until the player has genuinely been in sync once — so
deliberately switching to another file mid-session is left alone, as is any file opened with
`resetPosition` (a playlist switch is meant to start at the beginning).

## Tunables

All in `syncplay/constants.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `JOIN_SYNC_TOLERANCE` | `5.0` | How close a report must land to count as in sync |
| `JOIN_PULL_INTERVAL` | `2.0` | Minimum gap between catch-up seeks to one watcher |
| `JOIN_PULL_GRACE` | `15.0` | How long an unreachable stored position is defended |
| `POSITION_TELEPORT_GUARD` | `30.0` | Backwards jump that counts as a player restart |
| `FILE_CHANGE_REPORT_GRACE` | `10.0` | How long a file change waits for the new file's first position report |
| `CLIENT_SYNC_ON_FILE_LOAD_THRESHOLD` | `5.0` | How far ahead the room must be for a client to seek a freshly loaded file to it |

## Interoperability

Purely a change in *which* positions the server chooses to believe and when it sends an ordinary
forced seek. No new protocol messages, no new fields, no version gating needed — stock clients and
stock servers are unaffected, and a stock client on a guarded server simply stops being rewound.
