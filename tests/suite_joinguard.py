"""Unit suite for the join position guard (docs/join-position-guard.md).

Covers the server-side rule - a watcher only defines the room position while it demonstrably sits
at it - plus the teleport guard, the "nothing to sync to" fallbacks, the pull rate limiting, and
the client-side seek-to-room-on-file-load half.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import time
from syncplay import constants
from syncplay.server import ControlledRoom, Room, SyncFactory, Watcher

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] JOINGUARD :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


class FakeConnector:
    """Connector stand-in recording the States the server pushes at this watcher."""
    def __init__(self, logged=True):
        self.logged = logged
        self.outstandingForced = False
        self.sentStates = []
        self.watcher = None
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
    """SyncFactory stand-in for the cases where only the callbacks matter."""
    def __init__(self):
        self.pulls = []
        self.forcedUpdates = []

    def sendFileUpdate(self, watcher):
        pass

    def sendState(self, watcher, doSeek=False, forcedUpdate=False):
        room = watcher.getRoom()
        if room:
            watcher.sendState(room.getPosition(), room.isPaused(), doSeek, room.getSetBy(), forcedUpdate)

    def setAfk(self, watcher, isAfk):
        pass

    def updateYapTimer(self, room, paused, watcher):
        pass

    def updatePauseWarning(self, room, paused, watcher):
        pass

    def forcePositionUpdate(self, watcher, doSeek, watcherPauseState):
        self.forcedUpdates.append((watcher.getName(), doSeek, watcherPauseState))

    def pullWatcherIntoSync(self, watcher, position, requireFile=True):
        self.pulls.append((watcher.getName(), position))

    def pullWatcherIntoSyncIfNeeded(self, watcher, requireFile=True):
        if not watcher.isPositionEstablished():
            self.pulls.append((watcher.getName(), watcher.getRoom().estimatePosition()))

    def removeWatcher(self, watcher):
        pass


class FakeRoomManager:
    def broadcastRoom(self, sender, whatLambda):
        for receiver in sender.getRoom().getWatchers():
            whatLambda(receiver)


def makeWatcher(server, name, room=None, file_=True):
    watcher = Watcher(server, FakeConnector(), name)
    if room is not None:
        room.addWatcher(watcher)
    if file_:
        watcher.setFile({"name": "ep01.mkv", "duration": 3600, "size": 12345})
    watcher._connector.sentStates = []  # drop the join-time State Watcher.setRoom always sends
    return watcher


def report(watcher, position, paused=False, doSeek=False, messageAge=0):
    watcher.updateState(position, paused, doSeek, messageAge)


def playingRoom(name="room", roomsdb=None):
    room = Room(name, roomsdb)
    room._playState = Room.STATE_PLAYING
    return room


def settle(room, watcher, position):
    """Bring a watcher to an established, in-sync state at `position`."""
    room._position = position
    room._lastUpdate = time.time()
    report(watcher, position)
    return watcher


def makeFactory():
    factory = SyncFactory.__new__(SyncFactory)
    factory._roomManager = FakeRoomManager()
    return factory


# ---------- the reported bug: a joiner at 00:00 must not rewind the room ----------

def test_joiner_at_zero_does_not_rewind_the_room():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    check("alice is a position reference", alice.isPositionEstablished())

    bob = makeWatcher(server, "bob", room)
    report(bob, 0.0)
    check("joiner at 00:00 is not a position reference", not bob.isPositionEstablished())
    check("joiner is pulled towards the room", any(n == "bob" for n, _ in server.pulls),
          "pulls={}".format(server.pulls))

    room._lastUpdate = time.time() - 2  # force the min()-over-watchers recompute
    position = room.getPosition()
    check("room position survives the joiner", position > 1700, "position={}".format(position))
    check("room position reference is still alice", room.getSetBy() is alice)


def test_rejoin_at_zero_does_not_rewind_the_room():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    bob = makeWatcher(server, "bob", room)
    settle(room, alice, 1800.0)
    settle(room, bob, 1800.0)
    check("both watchers are references", alice.isPositionEstablished() and bob.isPositionEstablished())

    room.removeWatcher(bob)      # drops out and comes back with a freshly opened player
    room.addWatcher(bob)
    check("rejoining clears reference status", not bob.isPositionEstablished())
    report(bob, 0.0)
    room._lastUpdate = time.time() - 2
    check("room position survives the rejoin", room.getPosition() > 1700,
          "position={}".format(room.getPosition()))


def test_catching_up_restores_reference_status():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    bob = makeWatcher(server, "bob", room)
    report(bob, 0.0)
    check("still adrift before the seek lands", not bob.isPositionEstablished())
    report(bob, room.estimatePosition() - 1.0)
    check("an in-sync watcher becomes a reference", bob.isPositionEstablished())


def test_watcher_without_a_file_is_never_a_reference():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    bob = makeWatcher(server, "bob", room, file_=False)
    report(bob, 0.0)
    check("a fileless watcher cannot establish", not bob.isPositionEstablished())
    check("a fileless watcher is not in the reference set", "bob" not in room.getPositionReferences())
    bob.setFile({"name": "ep01.mkv", "duration": 3600, "size": 1})
    report(bob, 0.0)
    check("loading a file late does not make it a reference at 00:00", not bob.isPositionEstablished())
    room._lastUpdate = time.time() - 2
    check("room survives a late file load", room.getPosition() > 1700)


# ---------- deliberate actions still win ----------

def test_deliberate_seek_to_zero_is_honoured():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    bob = makeWatcher(server, "bob", room)
    report(bob, 0.0, doSeek=True)
    check("an explicit seek establishes the seeker", bob.isPositionEstablished())
    room._lastUpdate = time.time() - 2
    check("the room follows a deliberate seek to 00:00", room.getPosition() < 5,
          "position={}".format(room.getPosition()))


def test_gradual_lag_keeps_upstream_semantics():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    bob = makeWatcher(server, "bob", room)
    settle(room, alice, 1800.0)
    settle(room, bob, 1800.0)
    # Bob's machine struggles: he loses a couple of seconds per report until he is minutes behind.
    # That is ordinary desync - upstream semantics say the room waits for him.
    lag = 0.0
    for _ in range(60):
        lag += 2.0
        room._position, room._lastUpdate = 1800.0, time.time()
        report(bob, 1800.0 - lag)
        if not bob.isPositionEstablished():
            break
    check("2 minutes of accumulated lag keeps bob a reference ({}s in)".format(lag),
          bob.isPositionEstablished())
    room._lastUpdate = time.time() - 2
    check("the room still waits for the slowest watcher", room.getPosition() < 1800.0 - 100,
          "position={}".format(room.getPosition()))


def test_frozen_player_keeps_upstream_semantics():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    bob = makeWatcher(server, "bob", room)
    settle(room, alice, 1800.0)
    settle(room, bob, 1800.0)
    for _ in range(20):  # player stalls outright: same position reported while the room plays on
        room._position, room._lastUpdate = 1800.0, time.time()
        report(bob, 1800.0)
    check("a stalled player is not mistaken for a restart", bob.isPositionEstablished())


def test_file_change_is_not_treated_as_a_teleport():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    alice.setFile({"name": "ep02.mkv", "duration": 3600, "size": 999})
    report(alice, 0.0)
    check("a deliberate file switch keeps reference status", alice.isPositionEstablished())
    room._lastUpdate = time.time() - 2
    check("the room follows a file switch back to the start", room.getPosition() < 5,
          "position={}".format(room.getPosition()))


# ---------- teleport guard ----------

def test_player_restart_does_not_rewind_the_room():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    bob = makeWatcher(server, "bob", room)
    settle(room, alice, 1800.0)
    settle(room, bob, 1800.0)
    room._position, room._lastUpdate = 1800.0, time.time()
    report(bob, 0.0)  # same file, no seek: the player restarted
    check("a backwards teleport drops reference status", not bob.isPositionEstablished())
    check("the restarted player is pulled back", any(n == "bob" for n, _ in server.pulls))
    room._lastUpdate = time.time() - 2
    check("the room survives a player restart", room.getPosition() > 1700,
          "position={}".format(room.getPosition()))


# ---------- nothing to sync to ----------

def test_first_watcher_in_a_fresh_room_defines_it():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    report(alice, 1234.0)
    check("a lone watcher in a default-zero room establishes at once", alice.isPositionEstablished())
    check("no pointless pull is sent", not server.pulls, "pulls={}".format(server.pulls))
    room._lastUpdate = time.time() - 2
    check("the room adopts the lone watcher's position", abs(room.getPosition() - 1234.0) < 2)


def test_unreachable_stored_position_is_given_up_on():
    server = FakeServer()
    room = playingRoom()
    room._position, room._positionIsMeaningful = 1800.0, True  # e.g. restored from the rooms DB
    room._lastUpdate = time.time()
    alice = makeWatcher(server, "alice", room)
    report(alice, 0.0)
    check("a stored position is defended at first", not alice.isPositionEstablished())
    check("and the watcher is pulled to it", any(n == "alice" for n, _ in server.pulls))
    alice._unsyncedSince = time.time() - (constants.JOIN_PULL_GRACE + 1)
    report(alice, 0.0)
    check("an unreachable stored position is eventually given up on", alice.isPositionEstablished())


def test_ghost_room_does_not_hold_the_survivor_hostage():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    bob = makeWatcher(server, "bob", room)
    report(bob, 0.0)
    room.removeWatcher(alice)  # the only in-sync watcher leaves
    check("bob is still adrift", not bob.isPositionEstablished())
    bob._unsyncedSince = time.time() - (constants.JOIN_PULL_GRACE + 1)
    report(bob, 0.0)
    check("the survivor takes over the ghost room", bob.isPositionEstablished())


def test_room_reset_when_it_empties():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    room._lastUpdate = time.time() - 2
    room.getPosition()  # the room learns where it is from an in-sync watcher (the 1s state tick)
    check("a watched room has a meaningful position", room.positionIsMeaningful())
    room.removeWatcher(alice)
    check("an emptied non-persistent room is back to a default zero",
          not room.positionIsMeaningful() and room._position == 0)


# ---------- locked / controlled rooms ----------

def test_locked_room_reference_set():
    server = FakeServer()
    room = playingRoom()
    admin = makeWatcher(server, "admin", room)
    admin.setAdmin(True)
    settle(room, admin, 1800.0)
    plebe = makeWatcher(server, "plebe", room)
    settle(room, plebe, 1800.0)
    room.setLocked(True)
    check("a locked room keeps only admins as references",
          list(room.getPositionReferences()) == ["admin"], str(list(room.getPositionReferences())))
    room.setLocked(False)
    joiner = makeWatcher(server, "joiner", room)
    joiner.setAdmin(True)
    report(joiner, 0.0)
    room.setLocked(True)
    check("an admin who just joined is not a reference either",
          "joiner" not in room.getPositionReferences())
    room._lastUpdate = time.time() - 2
    check("a locked room survives an admin joining at 00:00", room.getPosition() > 1700,
          "position={}".format(room.getPosition()))


def test_controlled_room_reference_set():
    server = FakeServer()
    room = ControlledRoom("+test:hash", None)
    room._playState = Room.STATE_PLAYING
    controller = makeWatcher(server, "controller", room)
    room.addController(controller)
    settle(room, controller, 1800.0)
    check("the controller is a reference", "controller" in room.getPositionReferences())
    newController = makeWatcher(server, "newController", room)
    room.addController(newController)
    report(newController, 0.0)
    check("a controller who just joined is not a reference",
          "newController" not in room.getPositionReferences())
    room._lastUpdate = time.time() - 2
    check("a controlled room survives a controller joining at 00:00", room.getPosition() > 1700,
          "position={}".format(room.getPosition()))


# ---------- pause propagation and pull mechanics ----------

def test_unestablished_pause_propagates_without_the_position():
    factory = makeFactory()
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    bob = makeWatcher(server, "bob", room)
    bob._server = factory
    report(bob, 0.0)
    alice._connector.sentStates = []

    room._position, room._lastUpdate = 1800.0, time.time()
    factory.forcePositionUpdate(bob, False, True)  # bob's readiness toggle pauses the room
    sent = alice._connector.sentStates
    check("the pause still reaches the room", len(sent) == 1, "sent={}".format(sent))
    if sent:
        check("but never the newcomer's position", sent[0]["position"] > 1700,
              "position={}".format(sent[0]["position"]))
        check("and it is not sent as a seek", not sent[0]["doSeek"])

    alice._connector.sentStates = []
    factory.forcePositionUpdate(alice, False, True)
    check("an established watcher still drives the room",
          alice._connector.sentStates and alice._connector.sentStates[0]["position"] > 1700)


def test_pulls_do_not_stack_forced_updates():
    factory = makeFactory()
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    bob = makeWatcher(server, "bob", room)

    factory.pullWatcherIntoSync(bob, 1800.0)
    check("the first pull is sent", len(bob._connector.sentStates) == 1)
    check("and it is a forced seek",
          bob._connector.sentStates and bob._connector.sentStates[0]["doSeek"]
          and bob._connector.sentStates[0]["forced"])
    factory.pullWatcherIntoSync(bob, 1800.0)
    check("an immediate second pull is rate limited", len(bob._connector.sentStates) == 1)

    bob._lastPositionPull = time.time() - (constants.JOIN_PULL_INTERVAL + 1)
    bob._connector.outstandingForced = True
    factory.pullWatcherIntoSync(bob, 1800.0)
    check("no pull while a forced update is unacknowledged", len(bob._connector.sentStates) == 1)

    bob._connector.outstandingForced = False
    factory.pullWatcherIntoSync(bob, 1800.0)
    check("pulls resume once acknowledged", len(bob._connector.sentStates) == 2)

    fileless = makeWatcher(server, "fileless", room, file_=False)
    factory.pullWatcherIntoSync(fileless, 1800.0)
    check("no pull to a client with nothing loaded", not fileless._connector.sentStates)
    factory.pullWatcherIntoSync(fileless, 1800.0, requireFile=False)
    check("unless explicitly allowed (the join handshake)", len(fileless._connector.sentStates) == 1)


def test_no_pull_towards_a_default_zero_room():
    factory = makeFactory()
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    factory.pullWatcherIntoSyncIfNeeded(alice)
    check("a fresh room does not yank a joiner to 00:00", not alice._connector.sentStates,
          "sent={}".format(alice._connector.sentStates))


def test_yap_timer_untouched_by_a_join():
    server = FakeServer()
    room = playingRoom()
    alice = makeWatcher(server, "alice", room)
    settle(room, alice, 1800.0)
    room.yapStartPause("alice")
    room._yapTotalThisFile = 42.0
    bob = makeWatcher(server, "bob", room)
    report(bob, 0.0)
    check("a joiner does not reset the room's yap total", room._yapTotalThisFile == 42.0)
    check("nor the running pause clock", room._yapPauseStartedAt is not None)


# ---------- client side: seek a newly loaded file to the room ----------

class FakePlayer:
    def __init__(self):
        self.seeks = []

    def setPosition(self, position):
        self.seeks.append(position)


class FakeUi:
    def showDebugMessage(self, message):
        pass


class FakeUserlist:
    class _User:
        def __init__(self, hasFile):
            self.file = {"name": "ep01.mkv", "duration": 3600} if hasFile else None

    def __init__(self, hasFile=True):
        self.currentUser = self._User(hasFile)


def makeClient(globalPosition=1800.0, playerPosition=0.0, hasFile=True, alreadySynced=False,
               suppress=False, hasGlobalUpdate=True):
    from syncplay.client import SyncplayClient
    client = SyncplayClient.__new__(SyncplayClient)
    client._player = FakePlayer()
    client.ui = FakeUi()
    client.userlist = FakeUserlist(hasFile)
    client._lastGlobalUpdate = time.time() if hasGlobalUpdate else None
    client._globalPosition = globalPosition
    client._globalPaused = True
    client._lastPlayerUpdate = time.time()
    client._playerPosition = playerPosition
    client._playerPaused = True
    client._userOffset = 0.0
    client.lastRewindTime = None
    client._syncedWithRoomSinceConnect = alreadySynced
    client._suppressSyncOnNextFileLoad = suppress
    return client


def test_client_seeks_a_file_loaded_during_the_join_window():
    client = makeClient()
    client._syncNewlyLoadedFileToRoom()
    check("client: a file loaded on join is seeked to the room position",
          client._player.seeks and abs(client._player.seeks[0] - 1800.0) < 1,
          "seeks={}".format(client._player.seeks))


def test_client_leaves_playlist_switches_at_the_start():
    client = makeClient(suppress=True)
    client._syncNewlyLoadedFileToRoom()
    check("client: a file opened with resetPosition stays at 00:00", not client._player.seeks,
          "seeks={}".format(client._player.seeks))
    check("client: the suppression is one-shot", client._suppressSyncOnNextFileLoad is False)


def test_client_leaves_mid_session_file_changes_alone():
    client = makeClient(alreadySynced=True)
    client._syncNewlyLoadedFileToRoom()
    check("client: switching file after being in sync is not overridden", not client._player.seeks,
          "seeks={}".format(client._player.seeks))


def test_client_seek_preconditions():
    close = makeClient(globalPosition=1800.0, playerPosition=1799.0)
    close._syncNewlyLoadedFileToRoom()
    check("client: no pointless seek when already at the room position", not close._player.seeks)

    ahead = makeClient(globalPosition=10.0, playerPosition=1800.0)
    ahead._syncNewlyLoadedFileToRoom()
    check("client: a client ahead of the room is left to the normal sync machinery",
          not ahead._player.seeks)

    unknown = makeClient(hasGlobalUpdate=False)
    unknown._syncNewlyLoadedFileToRoom()
    check("client: nothing happens before the room position is known", not unknown._player.seeks)

    below = makeClient(globalPosition=constants.CLIENT_SYNC_ON_FILE_LOAD_THRESHOLD - 0.5)
    below._syncNewlyLoadedFileToRoom()
    check("client: gaps under the threshold are ignored", not below._player.seeks)

    above = makeClient(globalPosition=constants.CLIENT_SYNC_ON_FILE_LOAD_THRESHOLD + 5.0)
    above._syncNewlyLoadedFileToRoom()
    check("client: gaps over the threshold are corrected", above._player.seeks)


# ---------- constants ----------

def test_constants():
    check("JOIN_SYNC_TOLERANCE is a sane catch-up window", 1.0 <= constants.JOIN_SYNC_TOLERANCE <= 15.0)
    check("teleport guard is well clear of the sync tolerance",
          constants.POSITION_TELEPORT_GUARD > constants.JOIN_SYNC_TOLERANCE * 3)
    check("pull interval leaves room for the 1s state tick",
          constants.JOIN_PULL_INTERVAL >= constants.SERVER_STATE_INTERVAL)
    check("the give-up grace allows several pulls first",
          constants.JOIN_PULL_GRACE >= constants.JOIN_PULL_INTERVAL * 4)


for _name, _test in sorted(globals().items()):
    if _name.startswith("test_"):
        try:
            _test()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(_name, False, "raised {}: {}".format(type(e).__name__, e))

fails = [x for x in RESULTS if not x[1]]
print("\n===== JOIN GUARD SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
