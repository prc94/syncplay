"""Unit suite for permanent/persistent room construction at server startup.

Regression cover for the startup race: the rooms DB is loaded through an async adbapi deferred,
but ep_server opens the listening port straight away. That left two distinct bugs:
  1. permanent rooms did not exist yet, so a client joining in that window was put in an ordinary
     Room, which _deleteRoomIfEmpty discarded the moment it emptied (taking the room's published
     trusted domains with it);
  2. when the deferred finally fired, loadRooms *replaced* the dict entry with a brand new Room
     object - orphaning any watcher already holding a reference to the old one.

The fix creates the permanent rooms synchronously in RoomManager.__init__ (before the port can be
opened) and makes loadRooms merge into whatever is already there.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy

from syncplay import constants
from syncplay.server import RoomManager, Room, ControlledRoom
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] PermRooms :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


class FakeWatcher:
    """The bare minimum Room.addWatcher/removeWatcher and the broadcast helpers touch."""
    def __init__(self, name="w"):
        self._name = name
        self._room = None
        self._file = None
    def getName(self): return self._name
    def getRoom(self): return self._room
    def setRoom(self, room): self._room = room
    def getFile(self): return self._file
    def getPosition(self): return 0
    def isPositionEstablished(self): return True
    def isAdmin(self): return False
    def supportsFeature(self, f): return False
    def sendChatMessage(self, *a, **k): pass
    def sendSetting(self, *a, **k): pass
    def sendState(self, *a, **k): pass
    def setPlaylistIndex(self, *a, **k): pass
    def setPlaylist(self, *a, **k): pass
    def setPosition(self, *a, **k): pass


def makeManager(permanent, dbfile=None):
    """A RoomManager whose DB deferred has NOT fired yet - i.e. the state the port opens in."""
    return RoomManager(dbfile, list(permanent))


# ---------------- 1. permanent rooms exist before the DB load fires ----------------
# No rooms db file at all: connect() is never called, so nothing but __init__ can have run.
mgr = makeManager(["perm", "movies"])
rooms = mgr.exportRooms()
check("permanent rooms are created during __init__, before any DB callback",
      "perm" in rooms and "movies" in rooms, repr(sorted(rooms)))
check("they are flagged permanent straight away",
      all(n in rooms and rooms[n].isPermanent() for n in ("perm", "movies")))
check("--permanent-rooms-file no longer needs a rooms DB to work",
      mgr._roomsDbHandle is None and "perm" in rooms and rooms["perm"].isPermanent())

# The permanent-rooms file is read with splitlines(), so a blank line reaches us as "" - that must
# not become a nameless room (_deleteRoomIfEmpty skips falsy names, so it would never be cleaned up).
blankMgr = makeManager(["perm", "", "  "])
check("blank lines in the permanent-rooms file are ignored",
      "" not in blankMgr.exportRooms() and "  " not in blankMgr.exportRooms(),
      repr(sorted(blankMgr.exportRooms())))

# A room that exists but is not listed stays ordinary.
other = mgr._getRoom("random")
check("unlisted rooms are not marked permanent", not other.isPermanent())

# ---------------- 2. a joiner in the startup window gets the permanent room ----------------
mgr = makeManager(["perm"])
w = FakeWatcher("early")
mgr.moveWatcher(w, "perm")
check("a client joining immediately lands in the permanent room instance",
      w.getRoom() is mgr.exportRooms().get("perm"))
check("...and that room is permanent, so it survives emptying", w.getRoom().isPermanent())
mgr.removeWatcher(w)
check("the room is still there once the last watcher leaves",
      "perm" in mgr.exportRooms(), repr(sorted(mgr.exportRooms())))
mgr.moveWatcher(w, "gone")
mgr.removeWatcher(w)
check("an ordinary room in the same manager is still discarded when empty",
      "gone" not in mgr.exportRooms(), repr(sorted(mgr.exportRooms())))

# ---------------- 3. loadRooms merges instead of replacing ----------------
# The racing client is already in "saved" when the DB deferred finally delivers that room.
mgr = makeManager([])
w = FakeWatcher("racer")
mgr.moveWatcher(w, "saved")
racedRoom = mgr.exportRooms()["saved"]
mgr.loadRooms([("saved", "http://a.mkv\nhttp://b.mkv", 1, 42.0, 1234)])
check("loadRooms keeps the existing room object (no orphaned watchers)",
      mgr.exportRooms()["saved"] is racedRoom)
check("the racing watcher is still in the room the manager broadcasts to",
      racedRoom.getWatchers() and list(racedRoom.getWatchers())[0] is w)
check("the saved playlist is applied to the room the racer is already in",
      racedRoom._playlist == ["http://a.mkv", "http://b.mkv"], repr(racedRoom._playlist))
check("the saved position is restored too", racedRoom._position == 42.0)

# Live state wins: a room somebody already put a playlist in is not overwritten by the DB row.
mgr = makeManager([])
live = mgr._getRoom("busy")
live._playlist = ["http://live.mkv"]
mgr.loadRooms([("busy", "http://stale.mkv", 0, 99.0, 1)])
check("a room with a live playlist is not clobbered by the stale DB row",
      live._playlist == ["http://live.mkv"] and live._position != 99.0, repr(live._playlist))

# Rooms that only exist in the DB are still created normally.
mgr = makeManager([])
mgr.loadRooms([("fresh", "http://c.mkv", 0, 7.0, 1)])
check("DB-only rooms are still loaded", "fresh" in mgr.exportRooms())
check("...with their playlist", mgr.exportRooms()["fresh"]._playlist == ["http://c.mkv"])

# A permanent room that also has a DB row gets both the flag and the saved playlist.
mgr = makeManager(["perm"])
mgr.loadRooms([("perm", "http://d.mkv", 0, 3.0, 1)])
permRoom = mgr.exportRooms()["perm"]
check("a permanent room with a DB row keeps its permanent flag", permRoom.isPermanent())
check("...and picks up its saved playlist", permRoom._playlist == ["http://d.mkv"])
check("loadRooms did not duplicate the permanent room",
      len([n for n in mgr.exportRooms() if n == "perm"]) == 1)

# ---------------- 4. name/key consistency ----------------
longName = "x" * (constants.MAX_ROOM_NAME_LENGTH + 20)
truncated = longName[:constants.MAX_ROOM_NAME_LENGTH]
mgr = makeManager([])
mgr.loadRooms([(longName, "", 0, 0, 0)])
loaded = mgr.exportRooms()[truncated]
check("an over-long DB room name is filed under its truncated form", loaded is not None)
check("...and the room reports that same name, so _deleteRoomIfEmpty can find it",
      loaded.getName() == truncated, repr(loaded.getName()))
w = FakeWatcher("t")
mgr.moveWatcher(w, longName)
try:
    mgr.removeWatcher(w)
    removeError = None
except Exception as e:
    removeError = repr(e)
check("removing the last watcher from it does not raise (name vs key mismatch)",
      removeError is None, removeError or "")

# A permanent room whose name is a controlled-room name gets a ControlledRoom, matching what a
# client joining it in the startup window used to be given by _getRoom.
CONTROLLED = "+controlled:abcdef123456"  # CONTROLLED_ROOM_REGEX wants exactly 12 word chars
mgr = makeManager([CONTROLLED])
ctl = mgr.exportRooms().get(CONTROLLED)
check("a permanent controlled-room name yields a ControlledRoom",
      isinstance(ctl, ControlledRoom), type(ctl).__name__)
check("...and it is still permanent", ctl is not None and ctl.isPermanent())

# ---------------- 5. source-level guards ----------------
serverSrc = open(os.path.join(REPO_ROOT, "syncplay", "server.py")).read()
initBody = serverSrc[serverSrc.index("class RoomManager(object):"):serverSrc.index("    def loadRooms(self, rooms):")]
check("permanent rooms are created before the DB connection is opened",
      initBody.index("self._createPermanentRooms()") < initBody.index("self._roomsDbHandle.connect()"),
      "connect() must not be able to win the race")
loadBody = serverSrc[serverSrc.index("    def loadRooms(self, rooms):"):serverSrc.index("    def broadcastRoom(")]
check("loadRooms no longer assigns into self._rooms directly",
      "self._rooms[roomName] = room" not in loadBody)
check("loadRooms goes through _getRoom", "self._getRoom(roomName)" in loadBody)

fails = [x for x in RESULTS if not x[1]]
print("\n===== PERMROOMS SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
