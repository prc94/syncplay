"""Multi-client live-server E2E suite for yap timer + pause warning.

Boots real syncplayServer.py processes and drives them over raw sockets with a
protocol-faithful mini-client (Hello, State ping/echo, ignoringOnTheFly echo,
Set file). Records every Chat line and State extra with timestamps, then
asserts behavior + timing and scans server stdout for tracebacks.
"""
import json, socket, subprocess, sys, time, os, threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []

def check(scen, name, cond, detail=""):
    RESULTS.append((scen, name, bool(cond), detail))
    print("[{}] {} :: {} {}".format("PASS" if cond else "FAIL", scen, name, ("- " + detail) if detail else ""))


class MiniClient:
    """Protocol-faithful test client. role='leader' follows a schedule; 'follower' mirrors room state."""
    def __init__(self, name, room, version, features, role="follower", schedule=None, file_=None):
        self.name, self.room, self.version, self.features = name, room, version, features
        self.role, self.schedule, self.file_ = role, list(schedule or []), file_
        self.sock = None; self.buf = b""
        self.desired = None            # our reported paused state (None until we adopt one)
        self.server_iotf = 0           # ignoringOnTheFly counter to echo back
        self.hello = False
        self.events = []               # (t_rel, kind, payload)
        self.last_send = 0.0
        self.t0 = None

    def connect(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        self.sock.settimeout(0.0)      # fully non-blocking
        self.send({"Hello": {"username": self.name, "room": {"name": self.room},
                             "version": self.version, "features": self.features}})

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\r\n").encode())

    def log(self, kind, payload):
        t = (time.time() - self.t0) if self.t0 is not None else -1.0
        self.events.append((t, kind, payload))

    def pump(self):
        try:
            data = self.sock.recv(65536)
            if data:
                self.buf += data
        except (BlockingIOError, socket.timeout):
            pass
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line.decode())
            if "Hello" in msg:
                self.hello = True
                if self.file_:
                    self.send({"Set": {"file": self.file_}})
            if "Chat" in msg:
                self.log("chat", (msg["Chat"].get("username"), msg["Chat"]["message"]))
            if "Set" in msg:
                self.log("set", msg["Set"])
            if "State" in msg:
                st = msg["State"]
                if "yapTimer" in st:
                    self.log("yap", st["yapTimer"])
                if "pauseWarning" in st:
                    self.log("pw", st["pauseWarning"]["message"])
                if "playstate" in st:
                    self.log("state", st["playstate"].get("paused"))
                io = st.get("ignoringOnTheFly", {})
                if "server" in io:
                    self.server_iotf = io["server"]
                    ps = st.get("playstate", {})
                    if ps.get("paused") is not None and self.role == "follower":
                        self.desired = ps["paused"]      # adopt server-directed change
                elif "playstate" in st and self.role == "follower" and self.desired is None:
                    self.desired = st["playstate"].get("paused")

    def act(self, now_rel):
        while self.schedule and now_rel >= self.schedule[0][0]:
            _, action = self.schedule.pop(0)
            if action == "pause":
                self.desired = True
                self.log("act", "pause")
            elif action == "unpause":
                self.desired = False
                self.log("act", "unpause")
            elif action.startswith("setfile:"):
                fname = action.split(":", 1)[1]
                self.send({"Set": {"file": {"name": fname, "duration": 100, "size": 500}}})
                self.log("act", "setfile " + fname)

    def tick(self):
        self.pump()
        now = time.time()
        if now - self.last_send >= 0.2:
            self.last_send = now
            state = {"State": {"ping": {"clientRtt": 0}}}
            if self.desired is not None:
                state["State"]["playstate"] = {"position": 5.0, "paused": self.desired, "doSeek": False}
            if self.server_iotf:
                state["State"]["ignoringOnTheFly"] = {"server": self.server_iotf}
                self.server_iotf = 0
            self.send(state)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class ServerBoot:
    def __init__(self, port, extra_args):
        self.port = port
        # Fail loudly if a previous crashed run left a zombie server on this port -
        # otherwise tests silently run against stale code.
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            raise RuntimeError("port {} already in use (zombie server from a crashed run?)".format(port))
        except OSError:
            pass
        env = dict(os.environ); env["PYTHONPATH"] = BASE
        self.proc = subprocess.Popen([sys.executable, "syncplayServer.py", "--port", str(port)] + extra_args,
                                     cwd=BASE, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.out_lines = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        deadline = time.time() + 6
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def _read(self):
        for line in self.proc.stdout:
            self.out_lines.append(line.rstrip())

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.reader.join(timeout=2)

    def clean_log(self, scen):
        bad = [l for l in self.out_lines if "Traceback" in l or "Unhandled" in l or "Failure" in l]
        check(scen, "server log free of tracebacks/unhandled errors", not bad,
              (bad[0] if bad else "{} log lines scanned".format(len(self.out_lines))))


def run_clients(clients, port, duration):
    for c in clients:
        c.connect(port)
    deadline = time.time() + 5
    while time.time() < deadline and not all(c.hello for c in clients):
        for c in clients:
            c.pump()
        time.sleep(0.02)
    assert all(c.hello for c in clients), "not all clients completed Hello"
    t0 = time.time()
    for c in clients:
        c.t0 = t0
    while time.time() - t0 < duration:
        rel = time.time() - t0
        for c in clients:
            c.act(rel)
            c.tick()
        time.sleep(0.02)
    for c in clients:
        c.close()


def evts(client, kind):
    return [(t, p) for (t, k, p) in client.events if k == kind]

def chats_matching(client, needle):
    return [(t, m) for (t, (u, m)) in evts(client, "chat") if needle in m]

def near(t, target, tol=1.4):
    return abs(t - target) <= tol


