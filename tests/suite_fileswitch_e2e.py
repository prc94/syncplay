"""E2E S13: advancing to the next file must not rewind it to the previous file's end.

Boots a real server and drives it with a position-aware socket client that models a playlist
advance the way a real client produces one: the end-of-file pause is a state change, so the client
starts ignoring on the fly and its next States carry no playstate at all; a status poll issued
while the new file loads still reports the old file's position. Either of those used to consume the
server's "this report is from a new file" flag, after which the genuine 00:00 report looked like a
backwards teleport and was force-seeked back to where the old file ended - visibly so when both
files are the same length, mid-file when the next one is longer.

Uses ServerBoot for the zombie-port guard and the server-stdout traceback scan; brings its own
client because the harness MiniClient reports a fixed position.
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

PORT = 19081
DURATION = 1400.0        # both episodes are the same length: the classic symptom
CLIENTS = []


class PlaylistClient(threading.Thread):
    """Protocol-faithful client that models a player working through a playlist.

    Echoes ignoringOnTheFly.server - without that the server drops every playstate it reports.
    """
    def __init__(self, name, room, position=0.0, playing=True):
        threading.Thread.__init__(self, daemon=True)
        self.name, self.room = name, room
        self.position = position
        self.paused = not playing
        self.playing = playing      # insists on playing rather than following the room's pause state
        self.serverIgnore = 0
        self.clientIgnore = 0
        self.suppressPlaystate = 0  # States to send with no playstate (client ignoring on the fly)
        self.staleReports = 0       # reports still carrying the old file's position
        self.stalePosition = None
        self.states = []
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
        self.announceFile("ep01.mkv", DURATION)
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
            if not self.paused:
                self.position += now - last
            last = now

    def announceFile(self, name, duration):
        self.send({"Set": {"file": {"name": name, "duration": duration, "size": 123456}}})

    def advancePlaylist(self, name="ep02.mkv", duration=DURATION, pings=0, stale=0):
        """End of file: the player pauses, then the next file loads and restarts at 00:00."""
        self.playing = False
        self.paused = True
        self.suppressPlaystate = pings
        self.staleReports = stale
        self.stalePosition = self.position
        self.announceFile(name, duration)
        if not stale:
            self.position = 0.0

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
        if ps.get("paused") is not None and not self.playing:
            self.paused = ps["paused"]
        if ps.get("doSeek"):
            self.position = ps.get("position", 0)   # a real player honours a forced seek
        if self.playing:
            self.paused = False

        out = {"State": {"ping": {"clientLatencyCalculation": time.time(), "clientRtt": 0}}}
        if "ping" in state and "latencyCalculation" in state["ping"]:
            out["State"]["ping"]["latencyCalculation"] = state["ping"]["latencyCalculation"]
        emit = self.clientIgnore == 0 or self.serverIgnore != 0
        if self.suppressPlaystate > 0:
            self.suppressPlaystate -= 1
            emit = False
        if emit:
            position = self.position
            if self.staleReports > 0:
                self.staleReports -= 1
                position = self.stalePosition
                if self.staleReports == 0:
                    self.position = 0.0
            out["State"]["playstate"] = {"position": position, "paused": self.paused}
        if self.serverIgnore or self.clientIgnore:
            out["State"]["ignoringOnTheFly"] = {}
            if self.serverIgnore:
                out["State"]["ignoringOnTheFly"]["server"] = self.serverIgnore
                self.serverIgnore = 0
            if self.clientIgnore:
                out["State"]["ignoringOnTheFly"]["client"] = self.clientIgnore
        self.send(out)

    def forcedSeeksSince(self, since, minimumPosition):
        return [s for s in self.states
                if s["t"] >= since and s["doSeek"] and s["position"] >= minimumPosition]

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def spawn(*args, **kwargs):
    client = PlaylistClient(*args, **kwargs)
    CLIENTS.append(client)
    client.start()
    return client


def waitUntil(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def stopAll():
    for client in CLIENTS:
        client.stop()
    del CLIENTS[:]
    time.sleep(0.5)


def runAdvance(scen, roomName, pings=0, stale=0, nextDuration=DURATION, second=False):
    """Watch ep01 to the end, advance, then assert nothing drags us back into the old position."""
    print("--- {} ---".format(scen))
    alice = spawn("alice", roomName, position=DURATION - 5.0)
    others = [spawn("bob", roomName, position=DURATION - 5.0)] if second else []
    waitUntil(lambda: len(alice.states) >= 4)
    check(scen, "the room reaches the end of the first file", alice.states[-1]["position"] > DURATION - 20,
          "position={}".format(alice.states[-1]["position"]))
    advanced = time.time()
    alice.advancePlaylist(duration=nextDuration, pings=pings, stale=stale)
    for other in others:
        time.sleep(0.3)
        other.advancePlaylist(duration=nextDuration, pings=pings, stale=stale)
    time.sleep(4.0)   # long enough for the pull rate limiter to have fired several times over
    for client in [alice] + others:
        rewinds = client.forcedSeeksSince(advanced, DURATION / 2)
        check(scen, "{} is not seeked back into the finished file".format(client.name), not rewinds,
              "forced seeks to {}".format([round(s["position"], 1) for s in rewinds]))
    # The room only recomputes once the client has actually reported from the new file, which the
    # suppressed States deliberately delay - so wait for it rather than sampling.
    settled = waitUntil(lambda: alice.states[-1]["position"] < 30, timeout=8.0)
    check(scen, "the room settles at the start of the new file", settled,
          "position={}".format(alice.states[-1]["position"]))
    stopAll()


srv = ServerBoot(PORT, [])

try:
    # 1) the baseline that always worked: the 00:00 report is the very next State
    runAdvance("S13:clean advance", "clean")

    # 2) the reported bug: the end-of-file pause makes the client ignore on the fly, so the State
    #    that lands between Set:file and the first new-file position carries no playstate
    runAdvance("S13:advance behind a ping-only State", "pingonly", pings=1)
    runAdvance("S13:advance behind several ping-only States", "pingonly2", pings=3)

    # 3) same defect via a status poll issued while the new file was still loading
    runAdvance("S13:advance behind a stale position report", "stale", stale=1)

    # 4) a longer next file: the pull target is the old room position whatever the length
    runAdvance("S13:advance to a longer file", "longer", pings=1, nextDuration=DURATION * 2)

    # 5) two watchers finishing together, loading the next file a moment apart
    runAdvance("S13:two watchers advancing", "pair", pings=1, second=True)

    srv.clean_log("S13:fileswitch")
finally:
    stopAll()
    srv.stop()

fails = [r for r in RESULTS if not r[2]]
print("\n===== FILE SWITCH E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for scen, name, ok, detail in fails:
    print("  FAILED: [{}] {} {}".format(scen, name, detail))
sys.exit(1 if fails else 0)
