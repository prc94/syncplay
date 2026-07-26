"""E2E S12: the join position guard over a live server (docs/join-position-guard.md).

Boots a real server and drives it with position-aware socket clients to confirm that a watcher
joining with its player still at 00:00 is seeked up to the room instead of dragging the room back
to the start - while deliberate seeks, lone joiners and room switches keep working.

The harness MiniClient reports a fixed position, so this suite brings its own client; it still
uses ServerBoot for the zombie-port guard and the server-stdout traceback scan.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import socket
import threading
import time
from e2e_harness import ServerBoot, check, RESULTS
RESULTS.clear()

PORT = 19071
CLIENTS = []


class PositionClient(threading.Thread):
    """Protocol-faithful client that models a player position.

    Echoes ignoringOnTheFly.server - without that the server drops every playstate it reports.
    """
    def __init__(self, name, room, position=0.0, hasFile=True, leader=False, frozen=False):
        threading.Thread.__init__(self, daemon=True)
        self.name, self.room = name, room
        self.position = position
        self.paused = True
        self.hasFile = hasFile
        self.leader = leader        # insists on playing rather than following the room's pause state
        self.frozen = frozen        # player stuck where it is (still loading / never seeked)
        self.serverIgnore = 0
        self.clientIgnore = 0
        self.pendingSeek = None
        self.states = []            # every State received: position/paused/doSeek/setBy/t
        self.running = True
        self.sock = socket.create_connection(("127.0.0.1", PORT))
        self.buf = b""

    def send(self, obj):
        try:
            self.sock.sendall((json.dumps(obj) + "\r\n").encode("utf-8"))
        except OSError:
            pass

    def run(self):
        self.send({"Hello": {"username": self.name, "room": {"name": self.room},
                             "version": "1.7.0",
                             "features": {"sharedPlaylists": True, "chat": True,
                                          "featureList": True, "readiness": True,
                                          "managedRooms": True}}})
        if self.hasFile:
            self.announceFile("ep01.mkv")
            self.send({"Set": {"ready": {"isReady": True, "manuallyInitiated": False}}})
        last = time.time()
        while self.running:
            try:
                self.sock.settimeout(0.2)
                data = self.sock.recv(65536)
                if not data:
                    break
                self.buf += data
            except socket.timeout:
                pass
            except OSError:
                break
            while b"\r\n" in self.buf:
                line, self.buf = self.buf.split(b"\r\n", 1)
                if line.strip():
                    try:
                        self.handle(json.loads(line.decode("utf-8")))
                    except ValueError:
                        pass
            now = time.time()
            if not self.paused and not self.frozen:
                self.position += now - last
            last = now

    def announceFile(self, name):
        self.send({"Set": {"file": {"name": name, "duration": 3600, "size": 123456}}})

    def seekTo(self, position):
        self.pendingSeek = position

    def handle(self, msg):
        if "State" not in msg:
            return
        state = msg["State"]
        ig = state.get("ignoringOnTheFly", {})
        if "server" in ig:
            self.serverIgnore = ig["server"]
            self.clientIgnore = 0
        elif "client" in ig and ig["client"] == self.clientIgnore:
            self.clientIgnore = 0
        ps = state.get("playstate", {})
        self.states.append({"position": ps.get("position", 0), "paused": ps.get("paused"),
                            "doSeek": ps.get("doSeek"), "setBy": ps.get("setBy"), "t": time.time()})
        if ps.get("paused") is not None and not self.leader:
            self.paused = ps["paused"]
        if ps.get("doSeek"):
            self.position = ps.get("position", 0)   # a real player honours a forced seek
        if self.leader:
            self.paused = False

        out = {"State": {"ping": {"clientLatencyCalculation": time.time(), "clientRtt": 0}}}
        if "ping" in state and "latencyCalculation" in state["ping"]:
            out["State"]["ping"]["latencyCalculation"] = state["ping"]["latencyCalculation"]
        stateChange = False
        if self.clientIgnore == 0 or self.serverIgnore != 0:
            playstate = {"position": self.position, "paused": self.paused}
            if self.pendingSeek is not None:
                self.position = self.pendingSeek
                playstate["position"] = self.position
                playstate["doSeek"] = True
                self.pendingSeek = None
                stateChange = True
            out["State"]["playstate"] = playstate
        if stateChange:
            self.clientIgnore += 1
        if self.serverIgnore or self.clientIgnore:
            out["State"]["ignoringOnTheFly"] = {}
            if self.serverIgnore:
                out["State"]["ignoringOnTheFly"]["server"] = self.serverIgnore
                self.serverIgnore = 0
            if self.clientIgnore:
                out["State"]["ignoringOnTheFly"]["client"] = self.clientIgnore
        self.send(out)

    def roomPosition(self):
        return self.states[-1]["position"] if self.states else None

    def sawSeekTo(self, position, tolerance=15.0, since=0):
        return any(s["doSeek"] and abs(s["position"] - position) <= tolerance
                   for s in self.states if s["t"] >= since)

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def spawn(*args, **kwargs):
    client = PositionClient(*args, **kwargs)
    CLIENTS.append(client)
    client.start()
    return client


def waitUntil(predicate, timeout=10.0):
    """Poll until the condition holds. Keeps the suite from flaking under load."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def waitForStates(client, count, timeout=10.0):
    return waitUntil(lambda: len(client.states) >= count, timeout)


def settled(client, minStates=4):
    """Wait until a client has exchanged enough States for the room to have recomputed."""
    return waitForStates(client, minStates)


def stopAll():
    for client in CLIENTS:
        client.stop()
    del CLIENTS[:]
    time.sleep(0.5)


srv = ServerBoot(PORT, [])

try:
    # 1) a joiner stuck at 00:00 must not rewind a playing room
    SCEN = "S12:joining a playing room"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "playing", position=1800.0, leader=True)
    settled(alice)
    waitUntil(lambda: alice.states[-1]["paused"] is False)
    check(SCEN, "the room is playing", alice.states[-1]["paused"] is False)
    check(SCEN, "the room sits where alice is", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    joined = time.time()
    bob = spawn("bob", "playing", position=0.0, frozen=True)
    seeked = waitUntil(lambda: bob.sawSeekTo(1800.0, since=joined))
    settled(alice, len(alice.states) + 4)   # let the room recompute with the joiner present
    check(SCEN, "alice is not rewound by the joiner", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    check(SCEN, "the joiner is seeked up to the room", seeked,
          "states={}".format(bob.states[:3]))
    stopAll()

    # 2) ... nor a paused room
    SCEN = "S12:joining a paused room"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "paused", position=1800.0)
    settled(alice)
    check(SCEN, "the room sits at alice's position", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    joined = time.time()
    bob = spawn("bob", "paused", position=0.0, frozen=True)
    seeked = waitUntil(lambda: bob.sawSeekTo(1800.0, since=joined))
    settled(alice, len(alice.states) + 4)
    check(SCEN, "alice is not rewound while paused", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    check(SCEN, "the joiner is seeked while paused", seeked)
    stopAll()

    # 3) a reconnecting client whose player restarted
    SCEN = "S12:rejoin"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "rejoin", position=1800.0, leader=True)
    bob = spawn("bob", "rejoin", position=1800.0)
    settled(alice)
    settled(bob)
    check(SCEN, "both watchers start in sync", alice.roomPosition() > 1795)
    bob.stop()
    time.sleep(1.0)   # let the server process the disconnect before the name is reused
    bobAgain = spawn("bob", "rejoin", position=0.0, frozen=True)
    seeked = waitUntil(lambda: bobAgain.sawSeekTo(1800.0, tolerance=25.0))
    settled(alice, len(alice.states) + 4)
    check(SCEN, "alice survives the rejoin", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    check(SCEN, "the rejoiner is seeked forward", seeked)
    stopAll()

    # 4) a lone joiner still defines a room nobody has watched in
    SCEN = "S12:fresh room"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "fresh", position=600.0)
    settled(alice, 5)
    check(SCEN, "the room adopts the lone watcher's position", abs(alice.roomPosition() - 600.0) < 10,
          "position={}".format(alice.roomPosition()))
    check(SCEN, "and the lone watcher is not yanked to 00:00", alice.roomPosition() > 100)
    stopAll()

    # 5) explicit seeks still drive the room, including back to 00:00
    SCEN = "S12:deliberate seek"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "seeking", position=1800.0, leader=True)
    bob = spawn("bob", "seeking", position=1800.0)
    settled(alice)
    settled(bob)
    bob.seekTo(0.0)
    waitUntil(lambda: alice.roomPosition() < 20)
    check(SCEN, "a deliberate seek to 00:00 moves the room", alice.roomPosition() < 20,
          "position={}".format(alice.roomPosition()))
    stopAll()

    # 6) a pulled client must not end up permanently ignored by the server
    SCEN = "S12:pull does not wedge the client"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "notignored", position=1800.0, leader=True)
    settled(alice)
    bob = spawn("bob", "notignored", position=0.0)   # not frozen: honours the pull
    pulled = waitUntil(lambda: bob.position > 1700)
    check(SCEN, "the joiner was pulled up to the room", pulled,
          "bob at {}".format(bob.position))
    bob.seekTo(300.0)
    waitUntil(lambda: abs(alice.roomPosition() - 300.0) < 20)
    check(SCEN, "its later seek is still accepted", abs(alice.roomPosition() - 300.0) < 20,
          "position={}".format(alice.roomPosition()))
    stopAll()

    # 7) a joiner that loads its file only after connecting
    SCEN = "S12:late file load"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "latefile", position=1800.0, leader=True)
    settled(alice)
    bob = spawn("bob", "latefile", position=0.0, hasFile=False, frozen=True)
    settled(bob)
    settled(alice, len(alice.states) + 3)
    check(SCEN, "alice is untouched while the joiner has nothing loaded", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    loaded = time.time()
    bob.announceFile("ep01.mkv")
    seeked = waitUntil(lambda: bob.sawSeekTo(1800.0, tolerance=25.0, since=loaded))
    settled(alice, len(alice.states) + 4)
    check(SCEN, "alice is untouched once the joiner's file appears", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    check(SCEN, "the joiner is seeked as soon as it announces a file", seeked,
          "states={}".format(bob.states[-4:]))
    stopAll()

    # 8) switching into an occupied room
    SCEN = "S12:room switch"
    print("--- {} ---".format(SCEN))
    alice = spawn("alice", "target", position=1800.0, leader=True)
    wanderer = spawn("wanderer", "elsewhere", position=0.0, frozen=True)
    settled(alice)
    settled(wanderer)
    wanderer.send({"Set": {"room": {"name": "target"}}})
    settled(alice, len(alice.states) + 6)
    check(SCEN, "the target room is not rewound by an arrival", alice.roomPosition() > 1795,
          "position={}".format(alice.roomPosition()))
    stopAll()

    srv.clean_log("S12:joinguard")
finally:
    stopAll()
    srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== JOIN GUARD E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
