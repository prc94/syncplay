"""Unit suite for the file-switch half of the join position guard (docs/join-position-guard.md).

The reported bug: finishing a file and advancing to the next one sometimes rewound the new file to
the *previous* file's end position (obvious when both files are the same length, mid-file when the
new one is longer). Cause: the server's "this report comes from a freshly loaded file" flag was
consumed by whichever State happened to arrive next, and a playlist advance reliably produces
States that are not the new file's first position report - playstate-less pings from a client that
is ignoring on the fly, and status polls issued while the new file is still loading. The genuine
00:00 report that followed then looked like a backwards teleport, unestablished the watcher and got
it force-seeked back to where the old file ended.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import time
from syncplay import constants
from syncplay.server import Room, Watcher

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] FILESWITCH :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


class FakeConnector:
    def __init__(self):
        self.logged = True
        self.outstandingForced = False
        self.sentStates = []
        self.features = {}

    def setWatcher(self, watcher):
        self.watcher = watcher

    def isLogged(self):
        return self.logged

    def hasOutstandingForcedUpdate(self):
        return self.outstandingForced

    def sendState(self, position, paused, doSeek, setBy, forced=False):
        self.sentStates.append({"position": position, "paused": paused, "doSeek": doSeek,
                                "setBy": setBy.getName() if setBy else None, "forced": forced})

    def getFeatures(self):
        return self.features

    def getVersion(self):
        return "1.7.0"

    def meetsMinVersion(self, version):
        return True

    def sendMessage(self, message):
        pass

    def drop(self):
        pass


class FakeServer:
    def __init__(self):
        self.pulls = []

    def sendFileUpdate(self, watcher):
        # Mirrors SyncFactory.sendFileUpdate's pull attempt, which is the other route by which a
        # file announcement can trigger a catch-up seek.
        if watcher.getFile() and watcher.getRoom() is not None and not watcher.isPositionEstablished():
            self.pulls.append((watcher.getName(), watcher.getRoom().estimatePosition()))

    def sendState(self, watcher, doSeek=False, forcedUpdate=False):
        pass

    def setAfk(self, watcher, isAfk):
        pass

    def updateYapTimer(self, room, paused, watcher):
        pass

    def updatePauseWarning(self, room, paused, watcher):
        pass

    def forcePositionUpdate(self, watcher, doSeek, watcherPauseState):
        pass

    def pullWatcherIntoSync(self, watcher, position, requireFile=True):
        self.pulls.append((watcher.getName(), position))

    def pullWatcherIntoSyncIfNeeded(self, watcher, requireFile=True):
        if not watcher.isPositionEstablished():
            self.pulls.append((watcher.getName(), watcher.getRoom().estimatePosition()))

    def removeWatcher(self, watcher):
        pass


EPISODE_LENGTH = 1400.0


def makeWatcher(server, name, room, file_="ep01.mkv"):
    watcher = Watcher(server, FakeConnector(), name)
    room.addWatcher(watcher)
    if file_:
        watcher.setFile({"name": file_, "duration": EPISODE_LENGTH, "size": 12345})
    watcher._connector.sentStates = []  # drop the join-time State Watcher.setRoom always sends
    return watcher


def report(watcher, position, paused=False, doSeek=False, messageAge=0):
    watcher.updateState(position, paused, doSeek, messageAge)


def ping(watcher):
    """A State carrying no playstate - what a client sends while it is ignoring on the fly."""
    watcher.updateState(None, None, None, 0)


def pausedRoom(name="room"):
    room = Room(name, None)
    room._playState = Room.STATE_PAUSED  # a file that just ended leaves the room paused
    return room


def settle(room, watcher, position):
    room._position = position
    room._lastUpdate = time.time()
    report(watcher, position, paused=True)
    room._lastUpdate = time.time() - 2   # force the recompute, so the room position counts as meaningful
    room.getPosition()
    return watcher


def switchFile(watcher, name="ep02.mkv"):
    watcher.setFile({"name": name, "duration": EPISODE_LENGTH, "size": 999})


def endOfFile(server=None):
    """A room paused at the end of ep01 with one established watcher sitting there."""
    server = server or FakeServer()
    room = pausedRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, EPISODE_LENGTH - 5.0)
    del server.pulls[:]
    return server, room, alice


# ---------- the reported bug ----------

def test_clean_advance_keeps_the_watcher_as_the_reference():
    """Baseline: when the 00:00 report is the very next State, the switch was always handled."""
    server, room, alice = endOfFile()
    switchFile(alice)
    report(alice, 0.3, paused=True)
    check("a clean advance keeps reference status", alice.isPositionEstablished())
    check("a clean advance is not pulled back", not server.pulls, "pulls={}".format(server.pulls))


def test_ping_only_state_does_not_eat_the_file_change():
    """The reported bug: an end-of-file pause makes the client ignore on the fly, so the State
    that lands between Set:file and the first new-file position carries no playstate at all."""
    server, room, alice = endOfFile()
    switchFile(alice)
    ping(alice)
    report(alice, 0.3, paused=True)
    check("a ping-only State does not consume the file change", alice.isPositionEstablished())
    check("the new file is not seeked back to the old file's end", not server.pulls,
          "pulls={}".format(server.pulls))


def test_several_ping_only_states_do_not_eat_the_file_change():
    server, room, alice = endOfFile()
    switchFile(alice)
    for _ in range(4):
        ping(alice)
    report(alice, 0.3, paused=True)
    check("a run of ping-only States does not consume the file change", alice.isPositionEstablished())
    check("still no pull after several pings", not server.pulls, "pulls={}".format(server.pulls))


def test_stale_position_report_does_not_eat_the_file_change():
    """A status poll issued while the new file is still loading reports the old file's position."""
    server, room, alice = endOfFile()
    switchFile(alice)
    report(alice, EPISODE_LENGTH - 4.5, paused=True)   # still the old file, in flight
    report(alice, 0.3, paused=True)
    check("a stale old-file report does not consume the file change", alice.isPositionEstablished())
    check("no pull after a stale report", not server.pulls, "pulls={}".format(server.pulls))


def test_room_lands_on_the_new_file_not_the_old_ending():
    server, room, alice = endOfFile()
    switchFile(alice)
    ping(alice)
    report(alice, 0.3, paused=True)
    room._lastUpdate = time.time() - 2  # force the min()-over-watchers recompute
    check("the room follows the watcher into the new file", room.getPosition() < 10.0,
          "position={}".format(room.getPosition()))


def test_second_watcher_advancing_is_not_pulled_back():
    """Both watchers finish together; whoever loads second must not be yanked to the old end."""
    server = FakeServer()
    room = pausedRoom()
    alice = makeWatcher(server, "alice", room)
    bob = makeWatcher(server, "bob", room)
    settle(room, alice, EPISODE_LENGTH - 5.0)
    settle(room, bob, EPISODE_LENGTH - 5.0)
    del server.pulls[:]
    switchFile(alice)
    ping(alice)
    report(alice, 0.3, paused=True)
    switchFile(bob)
    ping(bob)
    report(bob, 0.2, paused=True)
    check("both advancing watchers keep reference status",
          alice.isPositionEstablished() and bob.isPositionEstablished())
    check("neither is pulled back to the old file's end", not server.pulls,
          "pulls={}".format(server.pulls))


def test_longer_next_file_is_not_seeked_to_the_old_ending():
    """Same defect, visible mid-file: the pull target is the old room position, whatever the
    new file's length is."""
    server, room, alice = endOfFile()
    alice.setFile({"name": "ep02.mkv", "duration": EPISODE_LENGTH * 2, "size": 999})
    ping(alice)
    report(alice, 0.3, paused=True)
    check("a longer next file is not seeked to the previous end", not server.pulls,
          "pulls={}".format(server.pulls))


# ---------- the guard still guards ----------

def test_player_restart_on_an_unchanged_file_is_still_a_teleport():
    server, room, alice = endOfFile()
    report(alice, 0.0, paused=True)   # same file, no seek: the player was restarted
    check("a restart on an unchanged file loses reference status", not alice.isPositionEstablished())
    check("a restart is pulled back to the room", any(n == "alice" for n, _ in server.pulls),
          "pulls={}".format(server.pulls))


def test_the_latch_expires():
    """If the new file never reports a position the old file cannot account for, stop waiting -
    the flag must not stay armed forever and disable the teleport guard."""
    server, room, alice = endOfFile()
    switchFile(alice)
    alice._fileChangedAt = time.time() - (constants.FILE_CHANGE_REPORT_GRACE + 1)
    report(alice, EPISODE_LENGTH - 4.0, paused=True)   # indistinguishable from the old file
    check("the latch is dropped once the grace expires", alice._fileChangedAt is None)
    report(alice, 0.0, paused=True)
    check("the teleport guard is armed again afterwards", not alice.isPositionEstablished())


def test_a_newcomer_is_still_pulled_when_it_announces_a_file():
    """A file announcement must not hand reference status to somebody who never had it."""
    server = FakeServer()
    room = pausedRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, EPISODE_LENGTH - 5.0)
    bob = makeWatcher(server, "bob", room, file_=None)
    del server.pulls[:]
    bob.setFile({"name": "ep01.mkv", "duration": EPISODE_LENGTH, "size": 1})
    check("announcing a file pulls an unestablished joiner", any(n == "bob" for n, _ in server.pulls),
          "pulls={}".format(server.pulls))
    ping(bob)
    report(bob, 0.0, paused=True)
    check("the joiner is still not a reference", not bob.isPositionEstablished())
    room._lastUpdate = time.time() - 2
    check("the room is not dragged to the joiner's 00:00", room.getPosition() > EPISODE_LENGTH - 20,
          "position={}".format(room.getPosition()))


def test_reannouncing_the_same_file_does_not_arm_the_latch():
    """PublicRoomManager re-sets the identical file on room moves; only real changes count."""
    server, room, alice = endOfFile()
    alice.setFile({"name": "ep01.mkv", "duration": EPISODE_LENGTH, "size": 12345})
    check("an identical file does not arm the latch", alice._fileChangedAt is None)
    report(alice, 0.0, paused=True)
    check("a teleport after a no-op file re-set is still caught", not alice.isPositionEstablished())


def test_explicit_seek_after_a_switch_still_wins():
    server, room, alice = endOfFile()
    switchFile(alice)
    report(alice, 0.3, paused=True)
    report(alice, 600.0, paused=True, doSeek=True)
    check("a seek in the new file keeps reference status", alice.isPositionEstablished())
    check("a seek is not pulled back", not server.pulls, "pulls={}".format(server.pulls))


# ---------- constants ----------

def test_constants():
    check("the file-change grace outlasts a few state ticks",
          constants.FILE_CHANGE_REPORT_GRACE >= constants.SERVER_STATE_INTERVAL * 5)
    check("the file-change grace is shorter than the protocol timeout",
          constants.FILE_CHANGE_REPORT_GRACE < constants.PROTOCOL_TIMEOUT)


for _name, _test in sorted(globals().items()):
    if _name.startswith("test_"):
        try:
            _test()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(_name, False, "raised {}: {}".format(type(e).__name__, e))

fails = [x for x in RESULTS if not x[1]]
print("\n===== FILE SWITCH SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
