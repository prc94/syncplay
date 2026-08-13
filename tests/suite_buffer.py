"""Unit suite for the buffer hold (docs/buffer-pause.md).

A player caching a stream stalls while still calling itself unpaused. Before this feature that
looked exactly like a desync: the frozen watcher became the room's slowest position reference and
everyone else was rewound and speed-shifted around it, repeatedly, with no explanation.

Three things are covered here, in the order the feature works:
  1. detection - the client noticing its own stall, from mpv's cache properties or from its
     position not advancing, without firing on the seeks and file loads that also freeze it;
  2. the reaction it replaces - the real client sync logic, driven against a stalled peer, must
     produce no seeks and no speed changes (and must still correct a genuine desync);
  3. the hold - the server pausing, announcing, timing out and releasing, including in rooms where
     the buffering user has no control of their own.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import time, types
from syncplay import constants
from syncplay.client import SyncplayClient
from syncplay.server import Room, SyncFactory, Watcher
from syncplay.players.mpv import MpvPlayer
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] BUF :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


# --------------------------------------------------------------------------- virtual clock
# Detection windows are 0.8s and 1.0s and the hold times out after 120s; a virtual clock keeps the
# suite instant and deterministic instead of sleeping through any of it (same trick as suite_lag).
VIRTUAL = [1000.0]
_real_time = time.time

def _install_clock():
    time.time = lambda: VIRTUAL[0]

def _restore_clock():
    time.time = _real_time

def advance(seconds):
    VIRTUAL[0] += seconds


# --------------------------------------------------------------------------- client-side fakes
class FakePlayer:
    speedSupported = True
    bufferStateSupported = False
    bufferHoldOSDSupported = True

    def __init__(self):
        self.events = []
        self.pos = 0.0
        self.bufferState = None
    def setPosition(self, p): self.events.append(("seek", p)); self.pos = p
    def setSpeed(self, s): self.events.append(("speed", s))
    def setPaused(self, p): self.events.append(("pause", p))
    def getBufferState(self): return self.bufferState
    def updateBufferHoldOSD(self, text): self.events.append(("osd", text))


class FakeUI:
    def __init__(self): self.messages = []
    def showMessage(self, m, *a, **k): self.messages.append(m)
    def showDebugMessage(self, m, *a, **k): pass
    def showErrorMessage(self, m, *a, **k): self.messages.append(m)


def make_client(nativeBufferState=False, pauseOnBuffer=True, canControl=True, serverBufferPause=True):
    """A real SyncplayClient with only the attributes the sync path reads (see tests/README.md)."""
    c = SyncplayClient.__new__(SyncplayClient)
    c._player = FakePlayer()
    c._player.bufferStateSupported = nativeBufferState
    c.ui = FakeUI()
    c._config = {
        "rewindThreshold": constants.DEFAULT_REWIND_THRESHOLD,
        "fastforwardThreshold": constants.DEFAULT_FASTFORWARD_THRESHOLD,
        "slowdownThreshold": constants.DEFAULT_SLOWDOWN_KICKIN_THRESHOLD,
        "rewindOnDesync": True, "fastforwardOnDesync": True, "slowOnDesync": True,
        "dontSlowDownWithMe": False, "pauseOnBuffer": pauseOnBuffer,
    }
    c.userlist = types.SimpleNamespace(currentUser=types.SimpleNamespace(
        file={"name": "a.mkv", "duration": 7200, "path": "/a.mkv"},
        username="me", canControl=lambda: canControl))
    c.serverFeatures = {"bufferPause": serverBufferPause, "chat": True}
    c._speedChanged = False
    c.behindFirstDetected = None
    c._desyncSince = {}
    c._buffering = False
    c._bufferingSince = None
    c._bufferCachePercent = None
    c._stallReference = None
    c._bufferHoldActive = False
    c._bufferFallbackPaused = False
    c._lastBufferChatTime = None
    c._userOffset = 0
    c.lastRewindTime = None
    c.lastUpdatedFileTime = None
    c.lastAdvanceTime = None
    c.lastConnectTime = VIRTUAL[0] - 600  # long since connected: not in the join grace window
    c.lastLeftTime = 0
    c.lastLeftUser = "x"
    c.playerPositionBeforeLastSeek = 0
    c.waitingToLoadNewfile = False
    c._afkKeybindPausePending = False
    c._username = "me"
    c._protocol = None
    start = 100.0
    c._player.pos = start
    c._playerPosition = start
    c._playerPaused = False
    c._lastPlayerUpdate = VIRTUAL[0]
    c._globalPosition = start
    c._globalPaused = False
    c._lastGlobalUpdate = VIRTUAL[0]
    c.playlist = types.SimpleNamespace(advancePlaylistCheck=lambda: None,
                                       notJustChangedPlaylist=lambda: True,
                                       canSwitchToNextPlaylistIndex=lambda: False)
    c._warnings = types.SimpleNamespace(checkWarnings=lambda: None, checkReadyStates=lambda: None)
    return c


def poll(c, position, paused=False, seconds=0.1):
    """One player status poll, `seconds` after the previous one."""
    advance(seconds)
    c._playerPosition = position
    c._player.pos = position
    c._updateBufferingState(paused, position)


_install_clock()
try:
    # ======================================================================= 1. detection
    # -------- generic stall heuristic (every player that cannot answer for itself) --------
    c = make_client()
    poll(c, 100.0)
    check("stall: not declared on the first frozen poll", not c.isBuffering())
    for _ in range(5):
        poll(c, 100.0)  # 0.5s frozen - still inside BUFFER_STALL_DETECT
    check("stall: not declared before BUFFER_STALL_DETECT", not c.isBuffering(),
          "frozen for 0.5s of {}s".format(constants.BUFFER_STALL_DETECT))
    for _ in range(5):
        poll(c, 100.0)  # past 0.8s
    check("stall: declared once the position has been frozen for the window", c.isBuffering())

    # Recovery needs sustained progress, not one moving poll.
    pos = 100.0
    pos += 0.1; poll(c, pos)
    check("stall: one advancing poll is not recovery", c.isBuffering())
    for _ in range(12):
        pos += 0.1; poll(c, pos)
    check("stall: recovered after BUFFER_RECOVER_HOLD of progress", not c.isBuffering())

    # Ordinary playback must never read as a stall, however long it runs.
    c = make_client()
    pos = 100.0
    for _ in range(200):  # 20s of normal playback
        pos += 0.1; poll(c, pos)
    check("stall: 20s of normal playback never trips detection", not c.isBuffering())

    # -------- the windows where a frozen position is normal --------
    for label, setup in (
        ("just rewound", lambda cl: setattr(cl, "lastRewindTime", VIRTUAL[0])),
        ("just connected", lambda cl: setattr(cl, "lastConnectTime", VIRTUAL[0])),
        ("file just loaded", lambda cl: setattr(cl, "lastUpdatedFileTime", VIRTUAL[0])),
        ("waiting for a new file", lambda cl: setattr(cl, "waitingToLoadNewfile", True)),
    ):
        c = make_client()
        setup(c)
        for _ in range(20):
            poll(c, 100.0)  # 2s frozen, well past the window
        check("stall: not declared while {}".format(label), not c.isBuffering())

    # A paused player is not a stalled one, and neither is one in a paused room.
    c = make_client()
    for _ in range(20):
        poll(c, 100.0, paused=True)
    check("stall: a paused player is never buffering", not c.isBuffering())
    c = make_client()
    c._globalPaused = True
    for _ in range(20):
        poll(c, 100.0)
    check("stall: a paused room is never buffering", not c.isBuffering())

    # -------- the end of a file looks exactly like a stall to the heuristic --------
    # Several players sit on the last frame with paused still False, long enough to clear
    # BUFFER_STALL_DETECT - which would pause the whole room as the file ends.
    c = make_client()
    c.userlist.currentUser.file = {"name": "a.mkv", "duration": 600.0, "path": "/a.mkv"}
    c._playerPosition = 598.0
    for _ in range(20):
        poll(c, 599.5)
    check("stall: the end of a file is not a stall", not c.isBuffering(),
          "frozen at 599.5 of 600.0 for 2s")

    # ...but a stall in the middle of that same file still is.
    c = make_client()
    c.userlist.currentUser.file = {"name": "a.mkv", "duration": 600.0, "path": "/a.mkv"}
    for _ in range(20):
        poll(c, 300.0)
    check("stall: a stall away from the end still detects", c.isBuffering())

    # A live stream reports no duration; nothing to be at the end of, so detection must still run.
    c = make_client()
    c.userlist.currentUser.file = {"name": "live", "duration": 0, "path": "http://x/live"}
    for _ in range(20):
        poll(c, 100.0)
    check("stall: a stream with no duration is still covered", c.isBuffering())

    # -------- a seek we issued ourselves --------
    # A player takes a moment to land on a commanded position and reports the old one meanwhile.
    # Only openFile's rewind sets lastRewindTime, so _pauseChangeIsPlayerNoise does not cover this.
    c = make_client()
    for _ in range(6):
        poll(c, 100.0)          # 0.6s frozen, not yet a stall
    c.setPosition(400.0)        # the sync logic seeks us
    for _ in range(4):
        poll(c, 100.0)          # the player is still reporting the old position
    check("stall: a seek we commanded resets the baseline", not c.isBuffering(),
          "otherwise the pre-seek freeze counts toward the window")

    # Opting out disables detection outright.
    c = make_client(pauseOnBuffer=False)
    for _ in range(20):
        poll(c, 100.0)
    check("stall: pauseOnBuffer=False never detects", not c.isBuffering())

    # -------- mpv's own answer, when the player has one --------
    c = make_client(nativeBufferState=True)
    c._player.bufferState = (True, 12)
    poll(c, 100.0)
    check("native: one stalled poll is not enough", not c.isBuffering())
    for _ in range(10):
        poll(c, 100.0)
    check("native: paused-for-cache held for the window is buffering", c.isBuffering())
    check("native: cache percentage is carried through", c.getBufferCachePercent() == 12,
          repr(c.getBufferCachePercent()))
    # The position advancing is irrelevant while mpv says the cache is the problem...
    c._player.bufferState = (False, 100)
    pos = 100.0
    for _ in range(12):
        pos += 0.1; poll(c, pos)
    check("native: recovery follows the player's own flag", not c.isBuffering())

    # A player that reports nothing must fall back to the heuristic rather than read as "fine".
    c = make_client(nativeBufferState=True)
    c._player.bufferState = None
    for _ in range(20):
        poll(c, 100.0)
    check("native: an unanswering player falls back to the stall heuristic", c.isBuffering())

    # -------- the mpv report parser --------
    parsed = MpvPlayer._parseStateReport("<paused=no, pos=612.3, cache=yes, cachepct=12>")
    check("mpv parse: all four fields",
          parsed == {"paused": "no", "pos": "612.3", "cache": "yes", "cachepct": "12"}, repr(parsed))
    short = MpvPlayer._parseStateReport("<paused=nil, pos=nil>")
    check("mpv parse: an older script's short report still parses",
          short == {"paused": "nil", "pos": "nil"}, repr(short))
    p = MpvPlayer.__new__(MpvPlayer)
    p._storeBufferState(short)
    check("mpv parse: no cache key means 'cannot answer', not 'not buffering'", p.getBufferState() is None)
    p._storeBufferState({"cache": "true", "cachepct": "34"})
    check("mpv parse: boolean-style cache value", p.getBufferState() == (True, 34), repr(p.getBufferState()))
    p._storeBufferState({"cache": "false", "cachepct": "nil"})
    check("mpv parse: unknown percentage does not break the flag",
          p.getBufferState() == (False, None), repr(p.getBufferState()))

    # ======================================================================= 2. the reaction it replaces
    def run_against_stalled_peer(buffering):
        """Drive the real sync logic for 20s while the room position sits frozen behind us.

        This is what a room does today when somebody's stream stalls: the room's position comes
        from the slowest reference watcher, so it stops advancing while ours keeps going, and the
        difference grows without bound.
        """
        c = make_client()
        c._buffering = buffering
        c._bufferHoldActive = buffering
        roomPosition = 100.0
        for _ in range(200):
            advance(0.1)
            c._playerPosition += 0.1
            c._player.pos = c._playerPosition
            c._changePlayerStateAccordingToGlobalState(roomPosition, False, False, "stalleduser")
        return c

    noisy = run_against_stalled_peer(buffering=False)
    seeks = [e for e in noisy._player.events if e[0] == "seek"]
    speeds = [e for e in noisy._player.events if e[0] == "speed"]
    check("regression: without the feature a stalled peer causes seeks", len(seeks) > 0,
          "{} seeks".format(len(seeks)))
    check("regression: without the feature a stalled peer causes speed changes", len(speeds) > 0,
          "{} speed changes".format(len(speeds)))

    quiet = run_against_stalled_peer(buffering=True)
    seeks = [e for e in quiet._player.events if e[0] == "seek"]
    speeds = [e for e in quiet._player.events if e[0] == "speed"]
    check("suppressed: a buffering client is never seeked", not seeks, repr(seeks))
    check("suppressed: a buffering client's speed is never changed", not speeds, repr(speeds))

    # Suppression must not outlive the stall: a real desync still gets corrected afterwards.
    c = make_client()
    c._buffering = True
    for _ in range(20):
        advance(0.1)
        c._changePlayerStateAccordingToGlobalState(c._playerPosition - 8.0, False, False, "someone")
    check("suppressed: no correction while buffering",
          not [e for e in c._player.events if e[0] == "seek"])
    c._buffering = False
    for _ in range(40):
        advance(0.1)
        c._changePlayerStateAccordingToGlobalState(c._playerPosition - 8.0, False, False, "someone")
    check("suppressed: a genuine desync is still corrected once the stall clears",
          [e for e in c._player.events if e[0] == "seek"])

    # A speed change already in force is reverted immediately rather than left applied.
    c = make_client()
    c._speedChanged = True
    c._buffering = True
    advance(0.1)
    c._changePlayerStateAccordingToGlobalState(c._playerPosition - 3.0, False, False, "someone")
    check("suppressed: an active slowdown is reverted at once",
          ("speed", 1.00) in c._player.events and not c._speedChanged, repr(c._player.events))

    # ======================================================================= 3. the server-side hold
    class FW:
        """Watcher stand-in for the room-level plumbing (see suite_afk.py for the pattern)."""
        def __init__(self, name, features=None, admin=False, position=100.0):
            self._name = name
            self._features = features if features is not None else {"bufferPause": True}
            self._admin = admin
            self._position = position
            self._room = None
            self._buffering = False
            self._gaveUp = False
            self.chats = []
            self.states = []
        def getName(self): return self._name
        def getRoom(self): return self._room
        def isAdmin(self): return self._admin
        def isController(self): return self._admin
        def supportsFeature(self, ft): return self._features.get(ft, False)
        def getFile(self): return {"name": "f.mkv"}
        def getPosition(self): return self._position
        def isPositionEstablished(self): return True
        def setPosition(self, p): self._position = p
        def isBuffering(self): return self._buffering
        def holdsBufferPause(self): return self._buffering and not self._gaveUp
        def bufferGiveUp(self): self._gaveUp = True
        def bufferCachePercent(self): return 42 if self._buffering else None
        def sendChatMessage(self, m, skipIfSupportsFeature=None):
            if skipIfSupportsFeature and self.supportsFeature(skipIfSupportsFeature):
                return
            self.chats.append(m["message"])
        def sendState(self, position, paused, doSeek, setBy, forced):
            self.states.append((position, paused, doSeek, forced))
        def __lt__(self, other):  # Room.getPosition min()s over the reference watchers
            return self._position < other._position

    def make_factory(bufferPause=True):
        f = SyncFactory.__new__(SyncFactory)
        f.bufferPause = bufferPause
        f.maxChatMessageLength = 150
        return f

    def make_room(watchers, playing=True, locked=False):
        room = Room("d", None)
        room._watchers = {w.getName(): w for w in watchers}
        for w in watchers:
            w._room = room
        room._playState = Room.STATE_PLAYING if playing else Room.STATE_PAUSED
        room._locked = locked
        room._position = 100.0
        room._lastUpdate = VIRTUAL[0]
        return room

    def chatsOf(watchers):
        return [m for w in watchers for m in w.chats]

    # -------- applying and releasing --------
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob])
    alice._buffering = True
    f.setBuffering(alice)
    check("hold: a playing room is paused", room.isPaused())
    check("hold: attributed to the buffering watcher", room.bufferHoldBy() == "alice")
    check("hold: everyone is told in chat",
          any("buffering" in m for m in chatsOf([alice, bob])), repr(chatsOf([alice, bob])))
    check("hold: a forced state goes to the whole room",
          all(w.states and w.states[-1][1] is True and w.states[-1][3] is True for w in (alice, bob)))

    advance(9.0)
    alice._buffering = False
    f.setBuffering(alice)
    check("release: the room goes back to playing", room.isPlaying())
    check("release: the hold is gone", not room.bufferHoldIsActive())
    check("release: the room is told it is resuming",
          any("resuming" in m for m in chatsOf([alice, bob])), repr(chatsOf([alice, bob])))

    # -------- a room that was already paused --------
    # The common case, and the one that used to break: everybody opens the stream, nobody has
    # pressed play yet, and the streamer's player starts caching straight away. Recording a hold
    # here looks harmless but turns the first play into "somebody resumed the room by hand", which
    # gives up on the very watcher being waited for.
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob], playing=False)
    alice._buffering = True
    f.setBuffering(alice)
    check("paused room: nothing is held - there is nothing to stop", not room.bufferHoldIsActive())
    check("paused room: nothing is announced", not chatsOf([alice, bob]), repr(chatsOf([alice, bob])))

    room._playState = Room.STATE_PLAYING  # somebody presses play, alice still caching
    f._reviewBufferHold(room)
    check("paused room: the hold arms as soon as the room actually plays",
          room.bufferHoldIsActive() and room.isPaused())
    check("paused room: and the first play is not treated as a manual override",
          not alice._gaveUp, "alice must still be worth waiting for")

    # -------- several people buffering at once --------
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob])
    alice._buffering = True
    f.setBuffering(alice)
    bob._buffering = True
    f.setBuffering(bob)
    check("multi: still one hold", room.bufferHoldBy() == "alice")
    alice._buffering = False
    f.setBuffering(alice)
    check("multi: the room waits for the last one too", room.isPaused() and room.bufferHoldIsActive())
    bob._buffering = False
    f.setBuffering(bob)
    check("multi: released once nobody is buffering", room.isPlaying() and not room.bufferHoldIsActive())

    # -------- a locked room, where the buffering user controls nothing --------
    f = make_factory()
    admin, alice = FW("admin", admin=True), FW("alice")
    room = make_room([admin, alice], locked=True)
    alice._buffering = True
    f.setBuffering(alice)
    check("locked room: the hold still pauses it", room.isPaused(),
          "server authority, not the watcher's")
    alice._buffering = False
    f.setBuffering(alice)
    check("locked room: and still resumes it", room.isPlaying())

    # -------- the server flag --------
    f = make_factory(bufferPause=False)
    alice = FW("alice")
    room = make_room([alice])
    alice._buffering = True
    f.setBuffering(alice)
    check("disabled: --no-buffer-pause holds nothing",
          room.isPlaying() and not room.bufferHoldIsActive())

    # -------- a watcher that stops reporting --------
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob])
    alice._buffering = True
    f.setBuffering(alice)
    alice._buffering = False  # stand-in for isBuffering() expiring after BUFFER_REPORT_STALE
    f._reviewBufferHold(room)
    check("stale: a silent reporter stops holding the room",
          room.isPlaying() and not room.bufferHoldIsActive())

    # The real staleness rule lives on Watcher, so exercise that too.
    w = Watcher.__new__(Watcher)
    w.clearBuffering()
    w._server = None
    w._buffering = True
    w._lastBufferReport = VIRTUAL[0]
    check("stale: a fresh report is believed", w.isBuffering())
    advance(constants.BUFFER_REPORT_STALE + 1)
    check("stale: an old one is not", not w.isBuffering(),
          "older than {}s".format(constants.BUFFER_REPORT_STALE))

    # -------- timing out --------
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob])
    alice._buffering = True
    f.setBuffering(alice)
    advance(constants.BUFFER_HOLD_MAX + 1)
    f._reviewBufferHold(room)
    check("timeout: the hold is dropped", not room.bufferHoldIsActive())
    check("timeout: the room stays paused", room.isPaused(),
          "resuming would only stall again")
    check("timeout: the room is told", any("stay paused" in m for m in chatsOf([alice, bob])),
          repr(chatsOf([alice, bob])))
    f._reviewBufferHold(room)
    check("timeout: not re-applied for the same still-stalled watcher", not room.bufferHoldIsActive())

    # Everyone still stalled is given up on, not only the watcher the hold was named after -
    # otherwise the next tick hands a fresh full-length hold to the next one, and the room is
    # given up on one user at a time.
    f2 = make_factory()
    a2, b2 = FW("a2"), FW("b2")
    room2 = make_room([a2, b2])
    a2._buffering = True; b2._buffering = True
    f2.setBuffering(a2)
    advance(constants.BUFFER_HOLD_MAX + 1)
    f2._reviewBufferHold(room2)   # expires
    f2._reviewBufferHold(room2)   # next tick must not re-arm for the second stalled watcher
    check("timeout: a second still-stalled watcher does not inherit a fresh hold",
          not room2.bufferHoldIsActive(), "holder now {}".format(room2.bufferHoldBy()))
    # Giving up leaves the room paused, so somebody has to resume it before there is anything to
    # hold again. Once they have, a watcher that recovered and then stalled afresh still counts.
    alice._buffering = False
    alice._gaveUp = False          # what Watcher.updateBuffering does on a not-buffering report
    f.setBuffering(alice)
    room._playState = Room.STATE_PLAYING
    alice._buffering = True
    f.setBuffering(alice)
    check("timeout: a later stall by the same watcher holds again", room.bufferHoldIsActive(),
          "gaveUp={}".format(alice._gaveUp))

    # -------- somebody resuming by hand --------
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob])
    alice._buffering = True
    f.setBuffering(alice)
    f.cancelBufferHold(room, bob)
    check("override: the hold is dropped", not room.bufferHoldIsActive())
    check("override: the recorded prior state is not restored", room.isPaused(),
          "the resumer's own unpause propagates instead")
    check("override: the room is told who did it",
          any("bob" in m for m in chatsOf([alice, bob])), repr(chatsOf([alice, bob])))
    room._playState = Room.STATE_PLAYING  # bob's unpause, arriving by the ordinary path
    f._reviewBufferHold(room)
    check("override: the hold is not put straight back by the next tick",
          not room.bufferHoldIsActive() and room.isPlaying(),
          "alice is still stalled, but the room has stopped waiting for her")

    # -------- self-healing: a hold that was never applied on the edge --------
    f = make_factory()
    alice, bob = FW("alice"), FW("bob")
    room = make_room([alice, bob])
    alice._buffering = True  # reported, but no edge reached setBuffering
    f._reviewBufferHold(room)
    check("review: a stall with no hold gets one on the next tick",
          room.bufferHoldIsActive() and room.isPaused())

    # -------- a stall report arriving after the previous one went stale --------
    w = Watcher.__new__(Watcher)
    w.clearBuffering()
    calls = []
    w._server = types.SimpleNamespace(setBuffering=lambda watcher: calls.append("edge"))
    w.updateBuffering(True, 10)
    check("edges: the first report is an edge", calls == ["edge"], repr(calls))
    w.updateBuffering(True, 20)
    check("edges: a repeat is not", calls == ["edge"], repr(calls))
    advance(constants.BUFFER_REPORT_STALE + 1)
    w.updateBuffering(True, 30)
    check("edges: a report after the last one went stale is an edge again",
          calls == ["edge", "edge"], repr(calls))

    # -------- the yap timer and pause warning stay out of it --------
    f = make_factory()
    alice = FW("alice")
    room = make_room([alice])
    alice._buffering = True
    f.setBuffering(alice)
    check("yap: a buffer hold does not start the yap timer", room._yapPauseStartedAt is None,
          "a network stall is not the room yapping")
    check("yap: nor the pause warning", not room._pauseWarningActive)

    # -------- the room emptying --------
    # Room.removeWatcher, not SyncFactory's isEmpty() branch: a room *switch* goes straight through
    # RoomManager.moveWatcher and never reaches the factory. A permanent or persistent room
    # survives being empty, so a hold left behind here is inherited by whoever joins next.
    class RealishWatcher(FW):
        def setRoom(self, r): self._room = r

    f = make_factory()
    alice = RealishWatcher("alice")
    room = make_room([alice])
    room.setPermanent(True)
    alice._buffering = True
    f.setBuffering(alice)
    check("cleanup: the hold is in force before the room empties", room.bufferHoldIsActive())
    room.removeWatcher(alice)  # the one path every removal goes through
    check("cleanup: an emptied room keeps no hold state",
          not room.bufferHoldIsActive() and room.bufferHoldBy() is None)

    carol = RealishWatcher("carol")
    room._watchers = {"carol": carol}
    carol._room = room
    f._reviewBufferHold(room)
    check("cleanup: the next person to join is not spontaneously resumed",
          not carol.chats and room.isPaused(),
          "chats={} playing={}".format(carol.chats, room.isPlaying()))

    # ======================================================================= 4. hostile input
    # All of this arrives from a peer. The client half of the feature re-validated what it
    # received from the start; the server half did not, and one malformed line was enough to
    # raise out of lineReceived and cost that client its connection.
    from syncplay.protocols import SyncServerProtocol, SyncClientProtocol

    class RecordingWatcher:
        def updateBuffering(self, active, cache): pass
        def updateState(self, *a): pass

    for payload in (None, "yes", [True, 20], 1, {"active": True, "cache": 20}):
        p = SyncServerProtocol.__new__(SyncServerProtocol)
        p._logged = True
        p.serverIgnoringOnTheFly = p.clientIgnoringOnTheFly = 0
        p._pingService = types.SimpleNamespace(getLastForwardDelay=lambda: 0, receiveMessage=lambda *a: None)
        p._watcher = RecordingWatcher()
        try:
            p.handleState({"buffering": payload})
            raised = None
        except Exception as exc:
            raised = "{}: {}".format(type(exc).__name__, exc)
        check("hostile: buffering={} does not raise".format(repr(payload)[:16]), raised is None, raised or "")

    w = Watcher.__new__(Watcher)
    w.clearBuffering()
    w._server = types.SimpleNamespace(setBuffering=lambda watcher: None)
    w.updateBuffering("false", None)
    check("hostile: only a real JSON true means buffering", not w.isBuffering(),
          'bool("false") is True, which is why this is not a bool() call')
    for value, expected in ((20, 20), (20.7, 20), (-5, 0), (250, 100), (True, None),
                            ("50", None), (float("nan"), None), (float("inf"), None)):
        check("hostile: cache {!r} sanitises to {!r}".format(value, expected),
              Watcher._sanitisedCachePercent(value) == expected)

    # The room position comes from an unvalidated reported playstate and formatTime raises on
    # non-finite input; the hold's chat line must not be what turns that into a server exception.
    for bad in (float("nan"), float("inf"), 10 ** 400, None, "x"):
        try:
            SyncFactory._safeFormatTime(bad)
            raised = None
        except Exception as exc:
            raised = "{}: {}".format(type(exc).__name__, exc)
        check("hostile: hold chat survives position {!r}".format(bad)[:60], raised is None, raised or "")

    holds = []
    cp = SyncClientProtocol.__new__(SyncClientProtocol)
    cp._pingService = types.SimpleNamespace(getLastForwardDelay=lambda: 0, receiveMessage=lambda *a: None,
                                            newTimestamp=lambda: 0, getRtt=lambda: 0)
    cp.hadFirstStateUpdate = True
    cp.clientIgnoringOnTheFly = cp.serverIgnoringOnTheFly = 0
    cp._sentBuffering = False
    cp.sendMessage = lambda m: None
    cp._client = types.SimpleNamespace(getLocalState=lambda: (None, None, None, False),
                                       updateGlobalState=lambda *a: None,
                                       isBuffering=lambda: False, getBufferCachePercent=lambda: None,
                                       setBufferHoldActive=lambda active: holds.append(active),
                                       ui=types.SimpleNamespace(updateBufferHold=lambda v: None))
    for payload in ("garbage", [1], 7):
        cp.handleState({"bufferHold": payload})
    check("hostile: a non-dict bufferHold never latches suppression on", not any(holds), repr(holds))
    cp.handleState({"bufferHold": {"user": "alice", "elapsed": 3}})
    check("hostile: a real bufferHold still does", holds[-1] is True)

    # ======================================================================= 5. the OSD payload
    holdRoom = make_room([FW("alice")])
    holdRoom.applyBufferHold(FW("alice"))
    advance(6.0)
    check("osd: elapsed counts up", 5.9 < holdRoom.bufferHoldElapsed() < 6.1,
          repr(holdRoom.bufferHoldElapsed()))

    c = make_client()
    from syncplay.client import UiManager  # the real overlay formatter, driven directly
    um = UiManager.__new__(UiManager)
    um._client = c
    um._lastBufferHoldText = ""
    um.updateBufferHold({"user": "alice", "elapsed": 6, "cache": 34})
    osd = [e for e in c._player.events if e[0] == "osd"]
    check("osd: names the user and the wait", osd and "alice" in osd[-1][1], repr(osd))
    check("osd: shows the cache percentage", osd and "34" in osd[-1][1], repr(osd))
    um.updateBufferHold({"user": "alice", "elapsed": 6, "cache": 999})
    check("osd: a nonsense percentage is dropped, not shown",
          "999" not in c._player.events[-1][1], repr(c._player.events[-1]))
    um.updateBufferHold(None)
    check("osd: no hold clears the overlay", c._player.events[-1][1] == "", repr(c._player.events[-1]))
    cleared = len(c._player.events)
    for _ in range(5):
        um.updateBufferHold(None)  # the state tick keeps arriving once a second, for ever
    check("osd: the clear is sent once, not on every tick", len(c._player.events) == cleared,
          "{} -> {}".format(cleared, len(c._player.events)))

finally:
    _restore_clock()

print("\n===== BUFFER SUMMARY: {} checks, {} failed =====".format(
    len(RESULTS), sum(1 for _, ok, _ in RESULTS if not ok)))
sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
