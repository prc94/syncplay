"""Unit suite for flaky-link desync hardening (docs/flaky-link-sync.md).

Covers the three things that let connection instability move a client that was actually in sync:
the forward-delay estimate (PingService), the cap on where that estimate is applied
(messageAge), and the sustained-evidence requirement on the rewind/slowdown reactions.

The centrepiece is a bidirectional link simulation: both endpoints run the real PingService over
a synthetic jittery link and drive the real SyncplayClient sync logic. Against the pre-fix code it
produced multi-second rewinds and a dozen-plus speed changes per five minutes on a client whose
player never actually drifted; it must now produce none - while a genuinely desynced client is
still corrected.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import random
import time
import types
from syncplay import constants
from syncplay.protocols import PingService, SyncClientProtocol
from syncplay.server import Room, Watcher

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] LAG :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


# --------------------------------------------------------------------------- virtual clock
# Every timing decision under test reads time.time(); a virtual clock keeps the suite fast and
# deterministic instead of sleeping through 1.5s sustain windows.
VIRTUAL = [1000.0]
_real_time = time.time

def _install_clock():
    time.time = lambda: VIRTUAL[0]

def _restore_clock():
    time.time = _real_time


# --------------------------------------------------------------------------- PingService
def feed(ps, rtt, senderRtt):
    """Deliver one message that took `rtt` to round-trip, with the peer reporting `senderRtt`."""
    sent = VIRTUAL[0]
    VIRTUAL[0] = sent + rtt
    ps.receiveMessage(sent, senderRtt)
    return ps.getLastForwardDelay()


def test_pingservice():
    # Steady link: the estimate should settle on half the round trip.
    ps = PingService()
    for _ in range(30):
        fd = feed(ps, 0.10, 0.10)
    check("steady link estimates half the round trip", abs(fd - 0.05) < 0.005, "fd=%.4f" % fd)

    # The regression that caused the report: one spike used to be added raw and unclamped, taking
    # the estimate above the spike itself (a 6s stall produced fd=6.42) and teleporting the room.
    ps = PingService()
    for _ in range(30):
        feed(ps, 0.05, 0.05)
    spike = feed(ps, 6.0, 0.05)
    check("single RTT spike cannot blow up the estimate",
          spike <= constants.PING_MAX_FORWARD_DELAY, "fd=%.3f cap=%.1f" % (spike, constants.PING_MAX_FORWARD_DELAY))
    check("single RTT spike stays near the steady-state estimate",
          spike < 0.5, "fd=%.3f (pre-fix: 6.421)" % spike)

    # ...and it must not poison the following updates either.
    after = [feed(ps, 0.05, 0.05) for _ in range(3)]
    check("estimate recovers immediately after a spike",
          all(f < 0.2 for f in after), "fd=%s" % ["%.3f" % f for f in after])

    # Cold start: the peer reports rtt=0 until it has a sample of its own. Treating that as
    # "the peer has a fast path, so all the delay is inbound" inflated the first estimates.
    ps = PingService()
    first = feed(ps, 0.40, 0.0)
    check("cold start does not inflate the estimate via senderRtt=0",
          abs(first - 0.20) < 0.01, "fd=%.4f (rtt/2=0.20)" % first)

    # The asymmetry correction still has to work: a genuinely lopsided route, held long enough,
    # should raise the estimate above rtt/2. Killing it outright would be a different bug.
    ps = PingService()
    for _ in range(60):
        fd = feed(ps, 0.40, 0.20)
    check("sustained genuine asymmetry still raises the estimate",
          fd > 0.25, "fd=%.4f vs rtt/2=0.20" % fd)
    check("sustained asymmetry correction stays bounded",
          fd <= constants.PING_MAX_FORWARD_DELAY, "fd=%.4f" % fd)

    # Alternating noise is not asymmetry: it must not accumulate into a standing offset.
    ps = PingService()
    rnd = random.Random(7)
    for _ in range(80):
        fd = feed(ps, 0.10 + rnd.choice([0.0, 0.30]), 0.10)
    check("alternating jitter does not accumulate a standing offset",
          fd < 0.45, "fd=%.4f" % fd)

    # A peer echoing nonsense timestamps must not be able to drive the estimate anywhere.
    ps = PingService()
    for _ in range(20):
        feed(ps, 0.05, 0.05)
    VIRTUAL[0] += 1.0
    ps.receiveMessage(VIRTUAL[0] + 500.0, 0.05)   # timestamp from the future -> negative rtt
    check("future-dated timestamp is ignored", ps.getLastForwardDelay() <= constants.PING_MAX_FORWARD_DELAY,
          "fd=%.3f" % ps.getLastForwardDelay())
    ps.receiveMessage(0, 0.05)                    # falsy timestamp -> no sample at all
    check("missing timestamp is ignored", ps.getLastForwardDelay() <= constants.PING_MAX_FORWARD_DELAY,
          "fd=%.3f" % ps.getLastForwardDelay())

    hostile = PingService()
    for _ in range(20):
        feed(hostile, 0.05, 0.05)
    fd = feed(hostile, 3600.0, 0.0)               # peer claims an hour-old echo
    check("hostile peer cannot teleport us via the estimate",
          fd <= constants.PING_MAX_FORWARD_DELAY, "fd=%.3f" % fd)


def test_handle_state_ping_without_latency_calculation():
    # A ping block without latencyCalculation used to raise UnboundLocalError out of the handler.
    p = SyncClientProtocol.__new__(SyncClientProtocol)
    p._pingService = PingService()
    try:
        messageAge, latencyCalculation = p._handleStatePing({"ping": {"clientLatencyCalculation": VIRTUAL[0], "serverRtt": 0.1}})
        ok, detail = True, "latencyCalculation=%r" % (latencyCalculation,)
    except Exception as e:
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    check("ping block without latencyCalculation does not raise", ok, detail)

    # Same class of bug one level up: handleState feeds latencyCalculation to sendState even when
    # the State carried no ping block at all.
    p = SyncClientProtocol.__new__(SyncClientProtocol)
    p._pingService = PingService()
    p.hadFirstStateUpdate = True
    p.clientIgnoringOnTheFly = 0
    p.serverIgnoringOnTheFly = 0
    p._sentBuffering = False
    p._pendingStateChange = False
    p._client = types.SimpleNamespace(
        getLocalState=lambda: (None, None, None, False),
        updateGlobalState=lambda *a: None,
        isBuffering=lambda: False,
        getBufferCachePercent=lambda: None,
        setBufferHoldActive=lambda active: None,
        ui=types.SimpleNamespace(updateBufferHold=lambda values: None))
    sent = []
    p.sendMessage = lambda m: sent.append(m)
    try:
        p.handleState({"playstate": {"position": 5.0, "paused": False}})
        ok, detail = True, "sent=%d message(s)" % len(sent)
    except Exception as e:
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    check("State without a ping block does not raise", ok, detail)


# --------------------------------------------------------------------------- messageAge caps
def test_message_age_caps():
    w = Watcher.__new__(Watcher)
    capped = w._updatePositionByAge(3600.0, False, 100.0)
    check("server caps messageAge applied to a reported position",
          capped <= 100.0 + constants.MAX_MESSAGE_AGE, "position=%.2f" % capped)
    normal = w._updatePositionByAge(0.25, False, 100.0)
    check("server still applies an ordinary messageAge", abs(normal - 100.25) < 1e-9, "position=%.4f" % normal)
    paused = w._updatePositionByAge(3600.0, True, 100.0)
    check("server applies no messageAge while paused", paused == 100.0, "position=%.2f" % paused)


# --------------------------------------------------------------------------- client harness
class FakePlayer:
    speedSupported = True
    def __init__(self, ev):
        self.ev = ev
        self.speed = 1.0
        self.pos = 0.0
        self.paused = False
    def setSpeed(self, s):
        self.speed = s
        self.ev.append((VIRTUAL[0], "setSpeed", round(s, 3)))
    def setPaused(self, p):
        self.paused = p
        self.ev.append((VIRTUAL[0], "setPaused", p))
    def setPosition(self, p):
        self.pos = p
        self.ev.append((VIRTUAL[0], "seek", round(p, 3)))


class FakeUI:
    def showMessage(self, m, hide=False): pass
    def showDebugMessage(self, m): pass
    def showErrorMessage(self, m, *a): pass


def build_client(ev, canControl=False):
    """A settled SyncplayClient: player and room agree, playing, nothing pending.

    Built with __new__ + the attributes the sync path touches, per the suite conventions in
    tests/README.md - __init__ wants a real player, UI and reactor.
    """
    from syncplay.client import SyncplayClient
    c = SyncplayClient.__new__(SyncplayClient)
    c._player = FakePlayer(ev)
    c.ui = FakeUI()
    c._config = {
        "rewindThreshold": constants.DEFAULT_REWIND_THRESHOLD,
        "fastforwardThreshold": constants.DEFAULT_FASTFORWARD_THRESHOLD,
        "slowdownThreshold": constants.DEFAULT_SLOWDOWN_KICKIN_THRESHOLD,
        "rewindOnDesync": True, "fastforwardOnDesync": True, "slowOnDesync": True,
        "dontSlowDownWithMe": False, "pauseOnBuffer": True,
    }
    c.userlist = types.SimpleNamespace(currentUser=types.SimpleNamespace(
        file={"name": "a.mkv", "duration": 7200, "path": "/a.mkv"},
        username="me", canControl=lambda: canControl))
    c._speedChanged = False
    c.behindFirstDetected = None
    c._desyncSince = {}
    c._buffering = False        # no cache stall here: this suite is about link jitter
    c._bufferHoldActive = False
    c._userOffset = 0
    c.lastRewindTime = None
    c.lastUpdatedFileTime = None
    c.lastAdvanceTime = None
    c.lastLeftTime = 0
    c.lastLeftUser = "x"
    c.playerPositionBeforeLastSeek = 0
    c._protocol = None
    c._username = "me"
    start = 100.0
    c._player.pos = start
    c._playerPosition = start
    c._playerPaused = False
    c._lastPlayerUpdate = VIRTUAL[0]
    c._globalPosition = start
    c._globalPaused = False
    c._lastGlobalUpdate = VIRTUAL[0]
    return c


def tick_room(c, roomPosition, seconds=1.0):
    """Advance one server State tick: wall clock moves, the player plays, the room reports in."""
    VIRTUAL[0] += seconds
    c._playerPosition = c._player.pos = c._player.pos + (seconds * c._player.speed if not c._player.paused else 0)
    c._lastPlayerUpdate = VIRTUAL[0]
    c._changePlayerStateAccordingToGlobalState(roomPosition, False, False, "someoneelse")


def test_sustained_desync_gating():
    # One bad sample - the shape a jittery latency estimate produces - must not move the player.
    ev = []
    c = build_client(ev)
    tick_room(c, c._player.pos - 8.0)   # room appears 8s behind us, once
    check("a single bad sample does not rewind", not [e for e in ev if e[1] == "seek"],
          "events=%s" % ev)

    # A real desync that persists must still be corrected, or we have merely traded one bug for
    # another. The room stays put while we play on, so the gap only widens.
    ev = []
    c = build_client(ev)
    stuckRoom = c._player.pos - 8.0
    for _ in range(4):
        tick_room(c, stuckRoom)
    seeks = [e for e in ev if e[1] == "seek"]
    check("a sustained desync is still rewound", len(seeks) >= 1, "seeks=%s" % seeks)

    # Same contract for the slowdown path.
    ev = []
    c = build_client(ev)
    tick_room(c, c._player.pos - 2.5)
    check("a single bad sample does not change speed", not [e for e in ev if e[1] == "setSpeed"],
          "events=%s" % ev)

    ev = []
    c = build_client(ev)
    for i in range(4):
        tick_room(c, c._player.pos - 2.5)
    speeds = [e for e in ev if e[1] == "setSpeed"]
    check("a sustained small desync still slows playback down", len(speeds) >= 1, "speeds=%s" % speeds)
    check("slowdown uses the configured rate",
          any(abs(e[2] - constants.SLOWDOWN_RATE) < 1e-9 for e in speeds), "speeds=%s" % speeds)

    # The fast-forward path had its own sustained-evidence guard already; keep it working.
    ev = []
    c = build_client(ev)
    for _ in range(12):
        tick_room(c, c._player.pos + 12.0)
    check("a sustained backward desync is still fast-forwarded",
          [e for e in ev if e[1] == "seek"], "events=%s" % [e for e in ev if e[1] == "seek"])


# --------------------------------------------------------------------------- link simulation
class Endpoint:
    def __init__(self):
        self.ps = PingService()
        self.peer_echo = 0.0
        self.peer_echo_arrival = 0.0


def simulate_link(delay_fn, steps=300, seed=1, room_drift=0.0):
    """Run the real client sync logic against the real ping handshake over a synthetic link.

    Mirrors SyncServerProtocol.sendState / SyncClientProtocol.sendState: the server stamps
    latencyCalculation and echoes back the client's last clientLatencyCalculation plus its own
    processing time, and each side feeds its PingService from the peer's reported RTT.

    room_drift shifts the room away from the client's true position, so the same harness covers
    "in sync but on a bad link" (0.0) and "genuinely desynced" (non-zero).
    """
    rnd = random.Random(seed)
    ev = []
    VIRTUAL[0] = 1000.0
    c = build_client(ev)
    player = c._player
    server, client = Endpoint(), Endpoint()
    inflight = []
    origin = VIRTUAL[0]
    start = player.pos
    last_advance = [origin]

    def room_position(now):
        return start + (now - origin) + room_drift

    def advance(now):
        dt = now - last_advance[0]
        if not player.paused:
            player.pos += dt * player.speed
        last_advance[0] = now
        c._playerPosition = player.pos
        c._lastPlayerUpdate = now
        c._playerPaused = player.paused

    def s2c(now):
        processing = (now - server.peer_echo_arrival) if server.peer_echo else 0.0
        inflight.append((now + delay_fn(rnd, now, "s2c"), "s2c", {
            "latencyCalculation": now,
            "serverRtt": server.ps.getRtt(),
            "clientLatencyCalculation": (server.peer_echo + processing) if server.peer_echo else None,
            "roompos": room_position(now)}))
        server.peer_echo = 0.0

    def c2s(now, echo):
        inflight.append((now + delay_fn(rnd, now, "c2s"), "c2s", {
            "latencyCalculation": echo,
            "clientLatencyCalculation": now,
            "clientRtt": client.ps.getRtt()}))

    next_tick = origin
    for _ in range(steps * 6):
        now = min([next_tick] + [p[0] for p in inflight])
        if now > origin + steps:
            break
        VIRTUAL[0] = now
        advance(now)
        if now == next_tick:
            s2c(now)
            next_tick += constants.SERVER_STATE_INTERVAL
        due = [p for p in inflight if p[0] == now]
        for p in due:
            inflight.remove(p)
        for _arrive, direction, pkt in due:
            if direction == "s2c":
                if pkt["clientLatencyCalculation"] is not None:
                    client.ps.receiveMessage(pkt["clientLatencyCalculation"], pkt["serverRtt"])
                messageAge = min(client.ps.getLastForwardDelay(), constants.MAX_MESSAGE_AGE)
                c._changePlayerStateAccordingToGlobalState(
                    pkt["roompos"] + messageAge, False, False, "someoneelse")
                c2s(now, pkt["latencyCalculation"])
            else:
                server.ps.receiveMessage(pkt["latencyCalculation"], pkt["clientRtt"])
                server.peer_echo = pkt["clientLatencyCalculation"]
                server.peer_echo_arrival = now
    return ev, player, room_position(VIRTUAL[0])


def stable_link(rnd, now, direction):
    return 0.025 + rnd.uniform(0, 0.005)


def jittery_link(rnd, now, direction):
    """Mostly fine, with delay bursts that hit each direction independently."""
    base = 0.025 + rnd.uniform(0, 0.01)
    if rnd.random() < 0.08:
        base += rnd.uniform(0.5, 4.0)
    return base


def harsh_link(rnd, now, direction):
    """Congested/mobile link: frequent multi-second stalls, still under PROTOCOL_TIMEOUT."""
    base = 0.04 + rnd.uniform(0, 0.03)
    r = rnd.random()
    if r < 0.10:
        base += rnd.uniform(1.0, 5.0)
    elif r < 0.14:
        base += rnd.uniform(5.0, 11.0)
    return base


def test_link_simulation():
    for label, link in (("stable", stable_link), ("jittery", jittery_link), ("harsh", harsh_link)):
        for seed in (1, 2, 3):
            ev, player, roompos = simulate_link(link, steps=300, seed=seed)
            seeks = [e for e in ev if e[1] == "seek"]
            speeds = [e for e in ev if e[1] == "setSpeed"]
            drift = player.pos - roompos
            check("%s link (seed %d): an in-sync client is never seeked" % (label, seed),
                  not seeks, "%d seeks: %s" % (len(seeks), seeks[:3]))
            check("%s link (seed %d): an in-sync client's speed is never changed" % (label, seed),
                  not speeds, "%d speed changes: %s" % (len(speeds), speeds[:3]))
            check("%s link (seed %d): stays in sync" % (label, seed),
                  abs(drift) < 1.0, "drift=%+.3fs" % drift)

    # The same harsh link, but the client really is far behind: correction must still happen.
    ev, player, roompos = simulate_link(harsh_link, steps=120, seed=1, room_drift=30.0)
    seeks = [e for e in ev if e[1] == "seek"]
    check("harsh link: a genuinely desynced client is still pulled into sync",
          len(seeks) >= 1, "%d seeks" % len(seeks))
    check("harsh link: correction actually converges",
          abs(player.pos - roompos) < 2.0, "drift=%+.3fs" % (player.pos - roompos))


# --------------------------------------------------------------------------- stale-echo guard
# Every watcher reports its play state once a second whether or not anything changed. On a slow
# link that report describes the room as it was a round trip ago, and the server used to read the
# contradiction as a keypress - which flipped the room, which produced the same contradiction from
# the other side one round trip later. Measured at 800ms RTT before the guard: 14 flips in 12s, all
# blamed on the lagging watcher. See docs/flaky-link-sync.md and tests/suite_flaky_e2e.py.
class EchoConnector:
    def __init__(self):
        self.states = []
    def setWatcher(self, watcher):
        pass
    def sendState(self, position, paused, doSeek, setBy, forced=False):
        self.states.append((paused, forced))
    def isLogged(self):
        return True
    def getFeatures(self):
        return {}
    def getVersion(self):
        return "1.7.0"
    def meetsMinVersion(self, version):
        return True
    def sendMessage(self, message):
        pass
    def drop(self):
        pass


class EchoServer:
    disableReady = False

    def __init__(self):
        self.forced = []
        self.readiness = []
    def setReady(self, watcher, isReady):
        self.readiness.append(isReady)
    def sendFileUpdate(self, watcher):
        pass
    def sendState(self, watcher, doSeek=False, forcedUpdate=False):
        pass
    def setAfk(self, watcher, isAfk):
        pass
    def updateYapTimer(self, room, paused, watcher):
        pass
    def updatePauseWarning(self, room, paused, watcher):
        pass
    def forcePositionUpdate(self, watcher, doSeek, watcherPauseState):
        self.forced.append((doSeek, watcherPauseState))
    def pullWatcherIntoSyncIfNeeded(self, watcher, requireFile=True):
        pass
    def removeWatcher(self, watcher):
        pass


def echoWatcher():
    """A settled watcher in a playing room, with the room's play state freshly changed."""
    server = EchoServer()
    room = Room("echoroom", None)
    watcher = Watcher(server, EchoConnector(), "laggy")
    room.addWatcher(watcher)
    watcher.setFile({"name": "a.mkv", "duration": 3600, "size": 1})
    watcher._fileChangedAt = None  # settled long ago: this is playback, not a file switch
    watcher.establishPosition()
    watcher.setPosition(100.0)
    room._setPlayState(Room.STATE_PLAYING)   # ...and now the room pauses, as a buffer hold would
    room._setPlayState(Room.STATE_PAUSED)
    return server, room, watcher


def test_stale_echo_guard():
    # The heartbeat that arrives one round trip after the room paused still says "playing".
    server, room, watcher = echoWatcher()
    watcher.updateState(100.0, False, False, 0.4, True)
    check("stale heartbeat contradicting a just-changed room is not a command",
          room.isPaused() and not server.forced, "paused=%s forced=%s" % (room.isPaused(), server.forced))

    # The same report once the window has passed is a user who has caught up and pressed play.
    server, room, watcher = echoWatcher()
    VIRTUAL[0] += 10.0
    watcher.updateState(100.0, False, False, 0.4, True)
    check("the same report outside the echo window is obeyed",
          not room.isPaused(), "paused=%s" % room.isPaused())

    # A keypress announces itself by bumping ignoringOnTheFly.client in the message that carries it;
    # the protocol passes echoCandidate=False for those, and they are never guarded.
    server, room, watcher = echoWatcher()
    watcher.updateState(100.0, False, False, 0.4, False)
    check("a report flagged as a user action is obeyed inside the window",
          not room.isPaused(), "paused=%s" % room.isPaused())

    # The window has to scale with the link, or it either misses the echo or deafens the server.
    server, room, watcher = echoWatcher()
    VIRTUAL[0] += 0.5                       # past a LAN client's window, well inside a 0.4s-delay one
    watcher.updateState(100.0, False, False, 0.0, True)
    check("a LAN client's window is short enough not to swallow a real keypress",
          not room.isPaused(), "paused=%s" % room.isPaused())
    server, room, watcher = echoWatcher()
    VIRTUAL[0] += 0.5
    watcher.updateState(100.0, False, False, 0.4, True)
    check("a slow client's window still covers its own round trip",
          room.isPaused(), "paused=%s" % room.isPaused())

    # A wild latency estimate must not make the server permanently deaf.
    server, room, watcher = echoWatcher()
    VIRTUAL[0] += constants.PAUSE_ECHO_GUARD_MAX + 1.0
    watcher.updateState(100.0, False, False, 3600.0, True)
    check("the window is capped however large the latency estimate gets",
          not room.isPaused(), "paused=%s" % room.isPaused())

    # A room that has never changed state has nothing to echo: pauses must work from the first one.
    server = EchoServer()
    room = Room("fresh", None)
    watcher = Watcher(server, EchoConnector(), "someone")
    room.addWatcher(watcher)
    watcher.setFile({"name": "a.mkv", "duration": 3600, "size": 1})
    watcher.establishPosition()
    watcher.updateState(100.0, True, False, 0.4, True)
    check("the first pause in a room is never mistaken for an echo",
          room.isPaused(), "paused=%s" % room.isPaused())

    # A rejected pause (locked room) never changes the room state, so the guard must stay out of the
    # way of the readiness toggle that converts it - suite_admin covers the toggle itself.
    server, room, watcher = echoWatcher()
    room._locked = True
    VIRTUAL[0] += 10.0
    watcher.updateState(100.0, False, False, 0.4, True)
    check("a rejected pause in a locked room still reaches the readiness path",
          room.isPaused() and len(server.readiness) == 1,
          "paused=%s readiness=%s" % (room.isPaused(), server.readiness))


# --------------------------------------------------------------------------- pending user action
def pendingProtocol(playerPaused):
    """A logged-in client protocol whose player reports `playerPaused`."""
    p = SyncClientProtocol.__new__(SyncClientProtocol)
    p._pingService = PingService()
    p.hadFirstStateUpdate = True
    p.clientIgnoringOnTheFly = 0
    p.serverIgnoringOnTheFly = 0
    p._sentBuffering = False
    p._pendingStateChange = False
    p._client = types.SimpleNamespace(
        getLocalState=lambda: (100.0, playerPaused[0], False, False),
        updateGlobalState=lambda *a: None,
        isBuffering=lambda: False,
        getBufferCachePercent=lambda: None,
        setBufferHoldActive=lambda active: None,
        ui=types.SimpleNamespace(updateBufferHold=lambda values: None))
    sent = []
    p.sendMessage = lambda m: sent.append(m["State"])
    return p, sent


def test_pending_state_change():
    # First keypress goes out and is marked as a change.
    playerPaused = [True]
    p, sent = pendingProtocol(playerPaused)
    p.sendState(100.0, True, False, None, True)
    check("a keypress is sent with the playstate and the change marker",
          "playstate" in sent[-1] and sent[-1].get("ignoringOnTheFly", {}).get("client") == 1,
          repr(sent[-1]))

    # Second keypress inside the same round trip cannot be sent - but must not be lost either.
    playerPaused[0] = False
    p.sendState(100.0, False, False, None, True)
    check("a keypress inside the round trip is held, not put on the wire",
          "playstate" not in sent[-1], repr(sent[-1]))
    check("...and held as pending", p._pendingStateChange, "pending=%s" % p._pendingStateChange)
    check("...without inflating the counter for a message nobody will see",
          p.clientIgnoringOnTheFly == 1, "counter=%d" % p.clientIgnoringOnTheFly)

    # The acknowledgement arrives: the held keypress goes out, once, as the change it always was.
    p.handleState({"ignoringOnTheFly": {"client": 1},
                   "playstate": {"position": 100.0, "paused": True, "setBy": "someoneelse"}})
    check("the held keypress is sent as soon as the acknowledgement lands",
          "playstate" in sent[-1] and sent[-1]["playstate"]["paused"] is False,
          repr(sent[-1].get("playstate")))
    check("...as a user action, so the server does not read it as an echo",
          sent[-1].get("ignoringOnTheFly", {}).get("client") == 1, repr(sent[-1].get("ignoringOnTheFly")))
    check("...and is not repeated afterwards", not p._pendingStateChange,
          "pending=%s" % p._pendingStateChange)

    # A plain heartbeat is never marked as a change: that marker is what tells the server the
    # difference between a keypress and this client repeating itself.
    playerPaused[0] = True
    p2, sent2 = pendingProtocol(playerPaused)
    p2.sendState(100.0, True, False, None, False)
    check("a heartbeat carries a playstate and no change marker",
          "playstate" in sent2[-1] and "ignoringOnTheFly" not in sent2[-1], repr(sent2[-1]))


def main():
    _install_clock()
    try:
        test_pingservice()
        test_handle_state_ping_without_latency_calculation()
        test_message_age_caps()
        test_sustained_desync_gating()
        test_stale_echo_guard()
        test_pending_state_change()
        test_link_simulation()
    finally:
        _restore_clock()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\nSUMMARY LAG: {}/{} passed".format(passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
