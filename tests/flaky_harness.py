"""Machinery for driving a real client, over a real socket, across a deliberately bad link.

The existing E2E harness talks to the server with `MiniClient`, a hand-written protocol speaker.
That is the right tool for asserting what the *server* sends, but it cannot show what a *client*
does about it - and on a flaky link almost everything that goes wrong is a client-side decision
(when to send a playstate, when to give up on the connection, when to call itself stalled).

So this harness runs the real thing: a real `SyncplayClient` built the way the unit suites build it
(`__new__` plus the attributes the sync path reads - see tests/README.md), a real
`SyncClientProtocol`, a real socket, and a real `syncplayServer.py` at the other end - with a
latency proxy in between that can add delay, jitter and outages. User actions are performed on the
*player*, exactly as a keypress in mpv is, and the client decides for itself what to put on the
wire.

Kept separate from `e2e_harness.py` so the suites that use that one are unaffected.
"""
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
sys.path.insert(0, HERE)
import collections
import random
import socket
import threading
import time
import types

from syncplay import constants
from syncplay.client import SyncplayClient
from syncplay.protocols import SyncClientProtocol
import syncplay.messages as M
from e2e_harness import MiniClient, ServerBoot

M.setLanguage("en")


# --------------------------------------------------------------------------- link profiles
class Profile(collections.namedtuple("Profile", "name delay jitter outage_at outage_for")):
    @property
    def rtt(self):
        return self.delay * 2

#: One-way delay in seconds (so RTT is twice `delay`), plus an optional single outage during which
#: the link holds everything and delivers it on recovery - a stalled mobile link, not a lost one.
#: `edge` is the link this suite exists for: ~1 Mbps mobile data at ~800 ms RTT.
PROFILES = {
    "lan": Profile("lan", 0.0, 0.0, None, None),
    "hspa": Profile("hspa", 0.15, 0.05, None, None),
    "edge": Profile("edge", 0.40, 0.15, None, None),
    # 8 s is inside PROTOCOL_TIMEOUT (12.5 s): nobody may disconnect over this.
    "edge_blackout": Profile("edge_blackout", 0.40, 0.15, 4.0, 8.0),
    # 15 s is outside it: both ends are entitled to drop, and the session must come back by itself.
    "edge_outage": Profile("edge_outage", 0.40, 0.15, 4.0, 15.0),
}


class LaggyLink:
    """TCP proxy that delays every byte, and can black the link out entirely.

    Byte order is preserved: the queue is only ever drained from the head, so a chunk with a small
    jitter draw can never overtake one queued before it (which would corrupt the stream rather than
    model a bad link). An outage holds data instead of dropping it - TCP would have retransmitted
    it - which is what makes the difference between the two edge profiles purely one of duration.
    """

    def __init__(self, listen_port, target_port, delay=0.0, jitter=0.0, seed=1):
        self.target_port = target_port
        self.delay, self.jitter = delay, jitter
        self.rnd = random.Random(seed)  # seeded: a failure has to be reproducible
        self.blackout_until = 0.0
        self.running = True
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", listen_port))
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def rtt(self):
        return self.delay * 2

    def blackout(self, seconds):
        self.blackout_until = time.time() + seconds

    def blacked_out(self):
        return time.time() < self.blackout_until

    def close(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.srv.accept()
            except (OSError, socket.timeout):
                continue
            try:
                upstream = socket.create_connection(("127.0.0.1", self.target_port))
            except OSError:
                client.close()
                continue
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(src, dst), daemon=True).start()

    def _pipe(self, src, dst):
        queue = collections.deque()
        src.settimeout(0.02)
        while self.running:
            try:
                data = src.recv(65536)
                if not data:
                    break
                wait = self.delay + (self.rnd.uniform(-self.jitter, self.jitter) if self.jitter else 0.0)
                queue.append((time.time() + max(0.0, wait), data))
            except socket.timeout:
                pass
            except OSError:
                break
            now = time.time()
            while queue and queue[0][0] <= now and not self.blacked_out():
                try:
                    dst.sendall(queue.popleft()[1])
                except OSError:
                    return
        for sock in (src, dst):
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- client-side fakes
class FlakyPlayer:
    """The player a user actually presses keys on. Position is driven by the session loop."""
    speedSupported = True
    bufferStateSupported = False  # heuristic stall detection: the path every non-mpv player uses
    bufferHoldOSDSupported = False

    def __init__(self):
        self.pos = 0.0
        self.paused = True
        self.speed = 1.0
        self.events = []

    def setPosition(self, position):
        self.events.append((time.time(), "seek", position))
        self.pos = position

    def setPaused(self, paused):
        self.events.append((time.time(), "pause", paused))
        self.paused = paused

    def setSpeed(self, speed):
        self.events.append((time.time(), "speed", speed))
        self.speed = speed

    def getBufferState(self):
        return None

    def setFeatures(self, features):
        pass  # the client hands the server's feature list to the player on Hello

    def askForStatus(self):
        # Real players answer asynchronously, so the status lands on the driver's next poll rather
        # than re-entering the client from inside updateGlobalState. Deliberately a no-op.
        pass


class RecordingUI:
    """Swallows the UI calls the client makes; keeps the ones a suite may want to assert on."""

    def __init__(self):
        self.messages = []
        self.errors = []

    def showMessage(self, message, *a, **k):
        self.messages.append(message)

    def showErrorMessage(self, message, *a, **k):
        self.errors.append(message)

    def showDebugMessage(self, message, *a, **k):
        pass

    def getUIMode(self):
        return constants.CONSOLE_UI_MODE

    def __getattr__(self, name):
        return lambda *a, **k: None  # the rest of the UI surface is not what is under test


class StubUser:
    def __init__(self, username):
        self.username = username
        self.room = None
        self.file = {"name": "flaky.mkv", "duration": 7200, "size": 900, "path": "/flaky.mkv"}
        self._ready = True
        self._locked = False

    def canControl(self):
        return True  # a plain room: authority is covered by suite_admin, not here

    def isController(self):
        return False

    def isReady(self):
        return self._ready

    def setReady(self, ready):
        self._ready = ready

    def isRoomLocked(self):
        return self._locked

    def setRoomLocked(self, locked):
        self._locked = bool(locked)


class StubPlaylist:
    """The shared playlist is not what is under test; only its answers to the sync path matter."""

    def notJustChangedPlaylist(self):
        return True

    def canSwitchToNextPlaylistIndex(self):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


class StubUserlist:
    """Just enough userlist for the join path; the room model under test lives on the server."""

    def __init__(self, username):
        self.currentUser = StubUser(username)

    def isRoomLocked(self, room):
        return False

    def clearRoomLocks(self):
        pass

    def isReady(self, username):
        return True

    def hasRoomStateChanged(self):
        return False

    def areAllOtherUsersInRoomReady(self):
        return True

    def __getattr__(self, name):
        return lambda *a, **k: None


class FakeTransport:
    """The two attributes LineReceiver touches, plus a socket to write to."""

    def __init__(self, sock):
        self.sock = sock
        self.disconnecting = False
        self.lost = False

    def write(self, data):
        try:
            self.sock.sendall(data)
        except OSError:
            self.lost = True

    def loseConnection(self):
        self.lost = True
        self.disconnecting = True
        try:
            self.sock.close()
        except OSError:
            pass

    def getPeer(self):
        return types.SimpleNamespace(host="127.0.0.1", port=0)


def build_client(username, room, ui, player, config_overrides=None):
    """A real SyncplayClient with the attributes the sync and connection paths read.

    Same recipe as tests/suite_lag.py and tests/suite_buffer.py - __init__ wants a real player, a
    real UI and an installed reactor, none of which exist here. Everything the suite actually
    measures (updatePlayerStatus, updateGlobalState, getLocalState, _updateBufferingState,
    checkIfConnected, connected) is the real method.
    """
    client = SyncplayClient.__new__(SyncplayClient)
    client._player = player
    client.ui = ui
    client._config = {
        "rewindThreshold": constants.DEFAULT_REWIND_THRESHOLD,
        "fastforwardThreshold": constants.DEFAULT_FASTFORWARD_THRESHOLD,
        "slowdownThreshold": constants.DEFAULT_SLOWDOWN_KICKIN_THRESHOLD,
        "rewindOnDesync": True, "fastforwardOnDesync": True, "slowOnDesync": True,
        "dontSlowDownWithMe": False, "pauseOnBuffer": True,
        # UNPAUSE_ALWAYS_MODE keeps a play keypress a play keypress: with any other setting
        # _toggleReady converts it into a readiness toggle, which has its own suites.
        "unpauseAction": constants.UNPAUSE_ALWAYS_MODE,
        "readyAtStart": True, "loadPlaylistFromFile": None, "sharedPlaylistEnabled": False,
        "adminPassword": None, "room": room, "name": username,
    }
    client._config.update(config_overrides or {})
    client.userlist = StubUserlist(username)
    client.userlist.currentUser.room = room
    client.playlist = StubPlaylist()
    client._warnings = types.SimpleNamespace(checkWarnings=lambda: None, checkReadyStates=lambda: None)
    client.serverFeatures = {}
    client.serverVersion = "1.7.0"
    client._protocol = None
    client._clientSupportsTLS = False
    client._serverSupportsTLS = False
    client._serverTrustedDomains = []
    client.protocolFactory = types.SimpleNamespace(stopRetrying=lambda: None)
    # capability flags read from the player class at startup in the real client
    client._yapTimerOSDSupported = False
    client._pauseWarningOSDSupported = False
    client._genericOSDSupported = False
    client._trackProposalsSupported = False
    # sync state
    client._speedChanged = False
    client.behindFirstDetected = None
    client._desyncSince = {}
    client._buffering = False
    client._bufferingSince = None
    client._bufferCachePercent = None
    client._bufferHoldActive = False
    client._bufferFallbackPaused = False
    client._lastBufferChatTime = None
    client._stallReference = None
    client._userOffset = 0
    client._playerPosition = player.pos
    client._playerPaused = player.paused
    client._lastPlayerUpdate = None
    client._globalPosition = 0.0
    client._globalPaused = True
    client._lastGlobalUpdate = None
    client._syncedWithRoomSinceConnect = False
    client._afkKeybindPausePending = False
    client.playerPositionBeforeLastSeek = 0
    client.lastRewindTime = None
    client.lastUpdatedFileTime = None
    client.lastAdvanceTime = None
    client.lastConnectTime = None
    client.lastLeftTime = 0
    client.lastLeftUser = ""
    client.lastPausedOnLeaveTime = None
    client.lastSetRoomTime = 0
    client.waitingToLoadNewfile = False
    client.waitingToLoadNewfileSince = None
    client.fileOpenBeforeChangingPlaylistIndex = None
    client.autoPlay = False
    client.autoPlayThreshold = None
    client.playlistMayNeedRestoring = False
    client._host = "127.0.0.1"        # read by thisIsPublicServer during the Hello
    client._serverPassword = None
    client._publicServers = []
    client.reconnecting = False
    client._running = True
    # Name-mangled inside SyncplayClient, so it has to be set under its mangled name from out here -
    # updateGlobalState reads it on the very first State and would otherwise raise.
    client._SyncplayClient__getUserlistOnLogon = False
    # reIdentifyAsController needs the controlled-room password store; nothing here is controlled
    client.reIdentifyAsController = lambda: None
    return client


class FlakyClient:
    """A real client on the far side of a `LaggyLink`, plus the instrumentation to judge it."""

    RECONNECT_DELAY = 0.3  # what ClientService's retry policy works out to for the first attempt

    def __init__(self, username, room, port, start_pos=0.0, config_overrides=None):
        self.username, self.room, self.port = username, room, port
        self.ui = RecordingUI()
        self.player = FlakyPlayer()
        self.player.pos = start_pos
        self.client = build_client(username, room, self.ui, self.player, config_overrides)
        self.starved = False
        self.reconnects = 0
        self.sent = []            # (t, has_playstate, ignoringOnTheFly)
        self.globals = []         # (t, position, paused, doSeek, setBy)
        self.forced_in = 0        # forced (ignoringOnTheFly.server) updates received
        self.t0 = time.time()
        self._reconnect_at = None
        self._last_poll = time.time()
        self.proto = None
        self.sock = None
        self._connect()

    # ---------------------------------------------------------------- connection
    def _connect(self):
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=3.0)
        self.sock.settimeout(0.0)
        self.proto = SyncClientProtocol(self.client)
        self.proto.transport = FakeTransport(self.sock)
        self._instrument(self.proto)
        self.proto.connectionMade()

    def _instrument(self, proto):
        client, log = self, self.sent

        send = proto.sendMessage
        def sendMessage(message):
            if "State" in message:
                state = message["State"]
                log.append((client.rel(), "playstate" in state, state.get("ignoringOnTheFly")))
            send(message)
        proto.sendMessage = sendMessage

        handle = proto.handleState
        def handleState(state):
            if (state.get("ignoringOnTheFly") or {}).get("server"):
                client.forced_in += 1
            handle(state)
        proto.handleState = handleState

        update = self.client.updateGlobalState
        def updateGlobalState(position, paused, doSeek, setBy, messageAge):
            client.globals.append((client.rel(), position, paused, doSeek, setBy))
            update(position, paused, doSeek, setBy, messageAge)
        self.client.updateGlobalState = updateGlobalState

    def _reconnect(self):
        # What ClientService does on a lost connection: reset the per-connection state, then dial
        # again. A fresh protocol means fresh ignoringOnTheFly counters, which is exactly why
        # "restart the app" is the workaround users find for a wedged session.
        self.client._performRetryStateReset()
        self.reconnects += 1
        try:
            self._connect()
        except OSError:
            self._reconnect_at = time.time() + self.RECONNECT_DELAY  # link still down; try again

    def rel(self):
        return round(time.time() - self.t0, 2)

    # ---------------------------------------------------------------- the 10 Hz driver
    def pump(self):
        """One iteration of the client's own loop: read the socket, poll the player, watchdog."""
        if self._reconnect_at is not None:
            if time.time() >= self._reconnect_at:
                self._reconnect_at = None
                self._reconnect()
            return
        try:
            data = self.sock.recv(65536)
            if data:
                self.proto.dataReceived(data)
            else:
                self._drop()  # server closed on us
                return
        except (BlockingIOError, socket.timeout):
            pass
        except OSError:
            self._drop()
            return
        if self.proto.transport.lost:
            self._drop()
            return
        now = time.time()
        if now - self._last_poll >= constants.PLAYER_ASK_DELAY:
            elapsed = now - self._last_poll
            self._last_poll = now
            if not self.player.paused and not self.starved:
                self.player.pos += elapsed
            # askPlayer(), minus the reactor: the status poll and the connection watchdog
            self.client.updatePlayerStatus(self.player.paused, self.player.pos)
            self.client.checkIfConnected()

    def _drop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self._reconnect_at = time.time() + self.RECONNECT_DELAY

    # ---------------------------------------------------------------- user actions
    def press_pause(self):
        """A pause/unpause keypress in the player - not a call into the client."""
        self.player.paused = not self.player.paused
        return self.player.paused

    def press_seek(self, delta):
        """A seek keypress. Well over SEEK_THRESHOLD so the client cannot read it as drift."""
        self.player.pos = max(0.0, self.player.pos + delta)
        return self.player.pos

    def starve(self):
        """The cache empties: playback freezes while the player still calls itself unpaused."""
        self.starved = True

    def feed(self):
        self.starved = False

    # ---------------------------------------------------------------- measurements
    def counters(self):
        return (self.proto.clientIgnoringOnTheFly, self.proto.serverIgnoringOnTheFly)

    def name(self):
        return self.client.getUsername()  # the server may have renamed us (findFreeUsername)

    def landed(self, after, kind, want, within, tolerance=4.0):
        """Did the room adopt this action within `within` seconds?

        Matched on the value, not on who the State says set it: `Room.getPosition` reassigns the
        room's `_setBy` to whichever watcher is currently the position reference, so a pause you
        made comes back attributed to whoever happens to be furthest behind. Judging delivery by
        attribution therefore reports about one action in fifteen as lost when it plainly was not.

        A value match is only ambiguous if the room is changing state on its own, which is the
        ping-pong - and that has a check of its own (`room does not ping-pong at one flip per RTT`),
        so it cannot quietly turn this one green.
        """
        for (t, position, paused, doSeek, setBy) in self.globals:
            if t <= after + 0.05 or t > after + within:
                continue
            if kind == "pause" and paused == want:
                return t - after
            if kind == "seek" and paused is not None and abs(position - want) <= tolerance:
                return t - after
        return None

    def stripped_between(self, start, end):
        """(swallowed, total) States in a window - swallowed meaning the playstate was left out.

        That is where a keypress goes to die: the client bumped ignoringOnTheFly, sent a State
        without a playstate, and nothing on the wire ever carried the user's change.
        """
        window = [s for s in self.sent if start <= s[0] <= end]
        return sum(1 for s in window if not s[1]), len(window)

    def seeks_after(self, after, within):
        return [g for g in self.globals if after < g[0] <= after + within and g[3]]

    def flips(self, since=0.0):
        """Every point at which the room changed pause state, as (t, paused, setBy)."""
        out, previous = [], None
        for (t, _position, paused, _doSeek, setBy) in self.globals:
            if previous is not None and paused != previous and t >= since:
                out.append((t, paused, setBy))
            previous = paused
        return out

    def room_flips(self, since=0.0):
        return len(self.flips(since))

    def mean_flip_interval(self, since=0.0):
        times = [t for (t, _p, _s) in self.flips(since)]
        if len(times) < 2:
            return None
        return (times[-1] - times[0]) / (len(times) - 1)

    def flips_attributed_to_us(self, since=0.0):
        me = self.name()
        return [f for f in self.flips(since) if f[2] == me]

    def counter_zero_since(self, after):
        """When clientIgnoringOnTheFly last returned to 0 - the 'not wedged' invariant."""
        for (t, has_playstate, iotf) in self.sent:
            if t > after and has_playstate and not (iotf or {}).get("client"):
                return t
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- session
class Session:
    """A server, a bad link, a healthy peer and one real client, pumped together."""

    #: Everyone starts here rather than at 00:00, so a backward seek has somewhere to go and two
    #: successive seeks are distinguishable (both would otherwise clamp to zero).
    START_POS = 300.0

    def __init__(self, port, proxy_port, profile, server_args=(), peer=True, seed=1,
                 client_config=None):
        self.profile = profile
        self.srv = ServerBoot(port, ["--salt", "testsalt"] + list(server_args))
        self.link = LaggyLink(proxy_port, port, delay=profile.delay, jitter=profile.jitter, seed=seed)
        self.peer = None
        if peer:
            # Connects directly, not through the link: it is the healthy other user in the room.
            self.peer = MiniClient("peer", "flakyroom", "1.7.6", {"chat": True, "bufferPause": True},
                                   role="leader", schedule=[(0.0, "unpause"), (0.5, "follow")],
                                   file_={"name": "flaky.mkv", "duration": 7200, "size": 900})
            self.peer.position = self.START_POS
            self.peer.connect(port)
            self.peer.t0 = time.time()
        self.client = FlakyClient("flakyuser", "flakyroom", proxy_port, start_pos=self.START_POS,
                                  config_overrides=client_config)
        self.t0 = self.client.t0
        self._peer_tick = time.time()
        self._blackout_done = False
        self.actions = []

    def rel(self):
        return time.time() - self.t0

    def pump(self):
        rel = self.rel()
        now = time.time()
        elapsed, self._peer_tick = now - self._peer_tick, now
        if self.peer:
            self.peer.pump()
            # Wall-clock, not per-iteration: a position advancing at loop speed would run away from
            # real time. And only while the room plays, so the peer never drags the room forward
            # through a hold it is supposed to be waiting out.
            if self.peer.desired is False:
                self.peer.position += elapsed
            self._peerFollowsSeeks()
            self.peer.act(rel)
            self.peer.tick()
        if (not self._blackout_done and self.profile.outage_at is not None
                and rel >= self.profile.outage_at):
            self._blackout_done = True
            self.link.blackout(self.profile.outage_for)
        self.client.pump()

    def _peerFollowsSeeks(self):
        """Keep the peer where the room is, as a real client would.

        MiniClient adopts a forced *pause* but never a forced *position*, so left alone it carries
        on from wherever it was when somebody seeked - and a peer permanently 60s adrift keeps the
        room in a position fight, with the forced updates that go with it. That is not a property of
        anything under test here; it just quietly poisons whatever the scenario was measuring.
        """
        seen = [p for (_t, kind, p) in self.peer.events if kind == "pos" and p is not None]
        if seen and abs(self.peer.position - seen[-1]) > constants.JOIN_SYNC_TOLERANCE:
            self.peer.position = seen[-1]

    def run(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.pump()
            time.sleep(0.01)

    def settle(self):
        """Let the join finish and the connect grace window (LAST_PAUSED_DIFF_THRESHOLD) expire.

        Inside it recentlyConnected() makes every pause change 'player noise' and suppresses stall
        detection, so anything a scenario does before this has different rules.
        """
        self.run(constants.LAST_PAUSED_DIFF_THRESHOLD + 1.5)

    def act(self, kind, delta=-45.0):
        at = self.client.rel()
        if kind == "pause":
            want = self.client.press_pause()
        else:
            want = self.client.press_seek(delta)
        self.actions.append((at, kind, want))
        return at, kind, want

    def action_results(self, within=6.0, since=0.0):
        out = []
        for (at, kind, want) in self.actions:
            if at < since:
                continue
            out.append((at, kind, want, self.client.landed(at, kind, want, within)))
        return out

    def peer_chats(self, needle):
        if not self.peer:
            return []
        return [(t, m) for (t, kind, payload) in self.peer.events if kind == "chat"
                for (_u, m) in [payload] if needle in m]

    def close(self, scen):
        self.client.close()
        if self.peer:
            self.peer.close()
        self.link.close()
        self.srv.clean_log(scen)
        self.srv.stop()


# --------------------------------------------------------------------------- known defects
#: Behaviour this suite reproduces but does not fail on: real bugs, not yet fixed. Recorded so the
#: measurements have a home and so nobody has to rediscover them - and so that when one stops
#: reproducing the summary says so loudly and it can be promoted to an ordinary check.
DEFECTS = []


def expect_defect(scen, name, present, detail=""):
    DEFECTS.append((scen, name, bool(present), detail))
    print("[{}] {} :: {} {}".format("KNOWN" if present else "FIXED", scen, name,
                                    ("- " + detail) if detail else ""))
    return bool(present)


def info(scen, name, detail):
    print("   [info] {} :: {} - {}".format(scen, name, detail))


def defect_summary():
    fixed = [d for d in DEFECTS if not d[2]]
    if fixed:
        print("\n!!!!! {} known defect(s) DID NOT REPRODUCE - if the fix is real, promote these to "
              "checks and delete the expect_defect call:".format(len(fixed)))
        for scen, name, _present, detail in fixed:
            print("      [{}] {} {}".format(scen, name, detail))
    return fixed
