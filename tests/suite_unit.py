"""Deep unit suites for yap timer + pause warning. Prints one line per check."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import time, sys
import unittest.mock as mock

RESULTS = []  # (suite, name, ok, detail)

def check(suite, name, cond, detail=""):
    RESULTS.append((suite, name, bool(cond), detail))
    print("[{}] {} :: {} {}".format("PASS" if cond else "FAIL", suite, name, ("- " + detail) if detail else ""))

def expect_raise_free(suite, name, fn, detail=""):
    try:
        fn()
        check(suite, name, True, detail)
    except Exception as e:
        check(suite, name, False, "raised {}: {}".format(type(e).__name__, e))

from syncplay import constants
from syncplay.utils import meetsMinVersion, formatTime
from syncplay.server import Room, ControlledRoom, SyncFactory, Watcher
from syncplay.protocols import SyncServerProtocol, PingService

# ---------------- Suite A: Room pause clock ----------------
S = "A:RoomClock"
r = Room("a", None)
check(S, "initial elapsed 0", r.yapCurrentElapsed() == 0.0)
check(S, "initial total 0", r.yapTotal() == 0.0)
check(S, "unpause without pause -> None", r.yapEndPause() is None)
check(S, "initial pausedBy None", r.yapPausedByName() is None)

r.yapStartPause("Alice")
first_start = r._yapPauseStartedAt
r.yapStartPause("Bob")  # second start while paused must be idempotent
check(S, "yapStartPause idempotent (start time kept)", r._yapPauseStartedAt == first_start)
check(S, "yapStartPause idempotent (attribution kept)", r.yapPausedByName() == "Alice")

r._yapPauseStartedAt = time.time() - 1.5
cur = r.yapCurrentElapsed()
check(S, "current elapsed ~1.5s", 1.4 < cur < 1.7, "measured %.3fs" % cur)
tot_live = r.yapTotal()
check(S, "live total includes current pause", abs(tot_live - cur) < 0.1, "total %.3fs" % tot_live)
e1 = r.yapEndPause()
check(S, "endPause returns elapsed ~1.5s", 1.4 < e1 < 1.7, "returned %.3fs" % e1)
check(S, "endPause is one-shot (second call None)", r.yapEndPause() is None)
check(S, "current elapsed 0 after end", r.yapCurrentElapsed() == 0.0)

# accumulate 3 pauses: 1.5 + 0.7 + 2.0 = 4.2
for dur, who in ((0.7, "Bob"), (2.0, "Carol")):
    r.yapStartPause(who)
    r._yapPauseStartedAt = time.time() - dur
    r.yapEndPause()
check(S, "total accumulates 3 pauses ~4.2s", 4.0 < r.yapTotal() < 4.5, "total %.3fs -> %s" % (r.yapTotal(), formatTime(r.yapTotal())))

before = r.yapTotal()
r.yapResetIfFileChanged("k1")           # None -> k1: resets
check(S, "key None->k1 resets total", r.yapTotal() == 0.0, "was %.2fs" % before)
r.yapStartPause("A"); r._yapPauseStartedAt = time.time() - 3.0; r.yapEndPause()
before = r.yapTotal()
r.yapResetIfFileChanged("k1")           # same key: keep
check(S, "same key keeps total", abs(r.yapTotal() - before) < 0.05, "kept %.2fs" % r.yapTotal())
r.yapResetIfFileChanged("k2")           # k1 -> k2: reset
check(S, "key change resets total", r.yapTotal() == 0.0)
r.yapStartPause("A")
r.yapReset()
check(S, "yapReset clears in-flight pause", r.yapCurrentElapsed() == 0.0 and r._yapPauseStartedAt is None)

cr = ControlledRoom("+ctl:aaaaaaaaaaaa", None)
check(S, "ControlledRoom inherits yap fields", cr._yapPauseStartedAt is None and cr._pauseWarningTimer is None and cr._pauseWarningActive is False)

# ---------------- Suite A2: yap active/AFK split ----------------
S = "A2:AfkSplit"
class AfkW:  # minimal watcher: only isAfk() feeds Room.hasAfkWatcher()
    def __init__(self, afk): self._afk = afk
    def isAfk(self): return self._afk
    def getName(self): return "w"

# All-active pause (nobody AFK): afkTotal stays 0, active == full elapsed.
ra = Room("split-active", None)
ra._watchers = {"w": AfkW(False)}
ra.yapStartPause("A")
check(S, "pause with no AFK watcher: no open AFK segment", ra._yapAfkSegStartedAt is None)
ra._yapPauseStartedAt = time.time() - 4.0
check(S, "all-active pause: afkTotal 0", ra.yapAfkTotal() == 0.0, "afk %.3f" % ra.yapAfkTotal())
check(S, "all-active pause: active == elapsed ~4s", 3.8 < (ra.yapTotal() - ra.yapAfkTotal()) < 4.2,
      "active %.3f" % (ra.yapTotal() - ra.yapAfkTotal()))
ra.yapEndPause()
check(S, "closed all-active pause: per-file afk 0, active ~4s",
      ra._yapAfkTotalThisFile == 0.0 and 3.8 < ra._yapTotalThisFile < 4.2, "afk=%.3f tot=%.3f" % (ra._yapAfkTotalThisFile, ra._yapTotalThisFile))

# A watcher goes AFK mid-pause, then returns: only the AFK window counts as AFK time.
rs = Room("split", None)
present = AfkW(False); rs._watchers = {"w": present}
rs.yapStartPause("A")
rs._yapPauseStartedAt = time.time() - 5.0     # 5s paused so far, all active
present._afk = True
rs.yapNoteAfkPresence(rs.hasAfkWatcher())      # presence flips -> opens an AFK segment
rs._yapAfkSegStartedAt = time.time() - 2.0     # backdate: AFK for the last 2s
check(S, "open AFK segment counts toward afkTotal ~2s", 1.8 < rs.yapAfkTotal() < 2.3, "afk %.3f" % rs.yapAfkTotal())
check(S, "active = total - afk ~3s", 2.7 < (rs.yapTotal() - rs.yapAfkTotal()) < 3.3, "active %.3f" % (rs.yapTotal() - rs.yapAfkTotal()))
present._afk = False
rs.yapNoteAfkPresence(rs.hasAfkWatcher())       # returns -> segment closes and is banked
check(S, "return from AFK banks the segment, clears open marker",
      rs._yapAfkSegStartedAt is None and 1.8 < rs._yapAfkAccumThisPause < 2.3, "accum %.3f" % rs._yapAfkAccumThisPause)
rs.yapEndPause()
check(S, "unpause folds afk portion into per-file total ~2s", 1.8 < rs._yapAfkTotalThisFile < 2.3, "afk %.3f" % rs._yapAfkTotalThisFile)
check(S, "per-file total > afk total (active time present)", rs._yapTotalThisFile > rs._yapAfkTotalThisFile,
      "tot=%.3f afk=%.3f" % (rs._yapTotalThisFile, rs._yapAfkTotalThisFile))
check(S, "noting AFK presence while playing is a no-op", (lambda rr: (rr.yapNoteAfkPresence(True), rr._yapAfkSegStartedAt is None)[1])(Room("idle", None)))

# ---------------- Suite A3: yap rewind-to-start reset ----------------
S = "A3:RewindReset"
rw = Room("rewind", None)
rw._playState = Room.STATE_PAUSED
rw.yapStartPause("A")
rw._yapPauseStartedAt = time.time() - 6.0       # 6s into the current pause
rw._yapTotalThisFile = 10.0                     # plus prior pauses
check(S, "pre-rewind total accumulated", rw.yapTotal() > 15.0, "total %.2f" % rw.yapTotal())
rw.yapResetOnRewind()
check(S, "rewind wipes per-file totals", rw._yapTotalThisFile == 0.0 and rw._yapAfkTotalThisFile == 0.0)
check(S, "rewind re-arms a fresh pause clock (still paused)", rw._yapPauseStartedAt is not None and rw.yapCurrentElapsed() < 0.5,
      "current %.3f" % rw.yapCurrentElapsed())
check(S, "rewind keeps the pause attribution", rw.yapPausedByName() == "A")
# rewind while playing must not re-arm a phantom pause
rw2 = Room("rewind2", None)
rw2._playState = Room.STATE_PLAYING
rw2._yapTotalThisFile = 8.0
rw2.yapResetOnRewind()
check(S, "rewind while playing resets total, arms no pause", rw2._yapTotalThisFile == 0.0 and rw2._yapPauseStartedAt is None)

# SyncFactory gate: only a controller seek to <= YAP_TIMER_REWIND_RESET_POSITION resets, and only when enabled.
fg = SyncFactory.__new__(SyncFactory)
fg.yapTimer = True
fg.pauseWarningAfter = 0  # pause-warning feature off here; these cases exercise yap reset only
rg = Room("gate", None); rg._playState = Room.STATE_PAUSED
rg.yapStartPause("A"); rg._yapTotalThisFile = 5.0
fg._yapNoteRewind(rg, 0.5)                       # <= 1.0s boundary -> reset
check(S, "_yapNoteRewind resets on seek to start", rg._yapTotalThisFile == 0.0)
rg._yapTotalThisFile = 5.0
fg._yapNoteRewind(rg, constants.YAP_TIMER_REWIND_RESET_POSITION + 0.5)  # past the boundary -> no reset
check(S, "_yapNoteRewind ignores mid-file seeks", rg._yapTotalThisFile == 5.0)
expect_raise_free(S, "_yapNoteRewind safe on None position/room", lambda: (fg._yapNoteRewind(rg, None), fg._yapNoteRewind(None, 0.0)))
fg.yapTimer = False
fg.bufferPause = True
rg._yapTotalThisFile = 5.0
fg._yapNoteRewind(rg, 0.0)                        # feature off -> never resets
check(S, "_yapNoteRewind no-op when yapTimer disabled", rg._yapTotalThisFile == 5.0)

# ---------------- Suite A4: pause-warning reset on file change / rewind ----------------
# The pause-warning threshold and OSD text share the yap pause clock (yapCurrentElapsed), so a
# file change or rewind that resets that clock must also void an in-progress warning - otherwise
# room._pauseWarningActive stays stuck and capable clients keep blinking the OSD with a
# sub-threshold duration on the new/rewound file (the reported bug).
S = "A4:PauseWarnReset"
fp = SyncFactory.__new__(SyncFactory)
fp.yapTimer = True; fp.pauseWarningAfter = 300; fp.pauseWarningInterval = 300; fp.pauseWarningMessage = "W {}!"

# file change while a warning is active -> flag cleared, and NOT re-armed (yap clock waits for the
# next pause event on the new file, so the warning does too)
rfc = Room("pwfile", None); rfc._playState = Room.STATE_PAUSED
rfc.yapStartPause("A"); rfc._pauseWarningActive = True
rfc._yapCurrentFileKey = "file:old"              # current key resolves to None -> counts as a change
fp._yapNoteFileChange(rfc)
check(S, "file change clears stuck pause warning", rfc._pauseWarningActive is False)
check(S, "file change does not re-arm on new file", rfc._pauseWarningDelayed is None)

# same-file update must NOT clear an in-progress warning
rfs = Room("pwsame", None); rfs._playState = Room.STATE_PAUSED
rfs.yapStartPause("A"); rfs._pauseWarningActive = True
rfs._yapCurrentFileKey = None                    # equals _getRoomFileKey(no file) -> no change
fp._yapNoteFileChange(rfs)
check(S, "same-file update keeps active warning", rfs._pauseWarningActive is True)

# rewind-to-start while paused -> stale state dropped AND re-armed from now (mirrors the yap clock)
rfr = Room("pwrewind", None); rfr._playState = Room.STATE_PAUSED
rfr.yapStartPause("A"); rfr._pauseWarningActive = True
fp._yapNoteRewind(rfr, 0.0)
check(S, "rewind clears stuck pause warning", rfr._pauseWarningActive is False)
check(S, "rewind re-arms threshold timer (still paused)",
      rfr._pauseWarningDelayed is not None and rfr._pauseWarningDelayed.active())
if rfr._pauseWarningDelayed is not None:
    rfr._pauseWarningDelayed.cancel()            # don't leave a live DelayedCall on the reactor

# rewind while playing -> clear, no phantom re-arm
rfp = Room("pwplay", None); rfp._playState = Room.STATE_PLAYING
rfp._pauseWarningActive = True
fp._yapNoteRewind(rfp, 0.0)
check(S, "rewind while playing clears and does not re-arm",
      rfp._pauseWarningActive is False and rfp._pauseWarningDelayed is None)

# feature off -> the hooks leave the pause-warning state untouched
fp.pauseWarningAfter = 0
rfo = Room("pwoff", None); rfo._playState = Room.STATE_PAUSED
rfo.yapStartPause("A"); rfo._pauseWarningActive = True
rfo._yapCurrentFileKey = "file:old"
fp._yapNoteFileChange(rfo)
check(S, "pause-warning disabled: file change leaves flag untouched", rfo._pauseWarningActive is True)

# ---------------- Suite B: SyncFactory config & text ----------------
S = "B:ConfigText"
f = SyncFactory.__new__(SyncFactory)
f.yapTimer = False
f.bufferPause = True

# ctor-equivalent logic executed through real ctor
real = SyncFactory("8999", "", None, None, None, False, "saltsaltsalt", False, False, 150, 16, None, None,
                   False, 0, 0, None)
check(S, "defaults: pauseWarningAfter off (0)", real.pauseWarningAfter == 0)
check(S, "defaults: yapTimer off", real.yapTimer is False)
check(S, "default message from i18n", "Paused" in real.pauseWarningMessage, repr(real.pauseWarningMessage))
real2 = SyncFactory("8999", "", None, None, None, False, "saltsaltsalt", False, False, 150, 16, None, None,
                    True, 120, None, "custom")
check(S, "interval None falls back to threshold", real2.pauseWarningInterval == 120)
check(S, "custom message stored", real2.pauseWarningMessage == "custom")
real3 = SyncFactory("8999", "", None, None, None, False, "saltsaltsalt", False, False, 150, 16, None, None,
                    False, 60, 15, None)
check(S, "explicit interval respected", real3.pauseWarningInterval == 15)

rt = Room("rt", None); rt.yapStartPause("A"); rt._yapPauseStartedAt = time.time() - 65
cases = [
    ("Paused {} - resume!", True,  "placeholder filled"),
    ("RESUME NOW",          False, "no placeholder -> verbatim"),
    ("bad {",               False, "unclosed brace -> ValueError -> verbatim"),
    ("hi {name}",           False, "named placeholder -> KeyError -> verbatim"),
    ("pos {0} again {0}",   True,  "positional {0} works"),
]
for msg, filled, why in cases:
    real.pauseWarningMessage = msg
    try:
        out = real.pauseWarningText(rt)
        ok = ("01:05" in out) if filled else (out == msg)
        check(S, "pauseWarningText: " + why, ok, repr(out))
    except Exception as e:
        check(S, "pauseWarningText: " + why, False, "raised " + repr(e))

# _getRoomFileKey branches
class FW:
    def __init__(self, file_): self._f = file_
    def getFile(self): return self._f
kr = Room("k", None)
kr._playlist = ["a.mkv", "b.mkv"]; kr._playlistIndex = 1
check(S, "filekey: playlist+index", real._getRoomFileKey(kr) == "index:1:b.mkv")
kr._playlistIndex = 5
kr._setBy = FW({"name": "m.mkv", "duration": 1})
check(S, "filekey: out-of-range index -> setBy file dict", real._getRoomFileKey(kr) == "file:m.mkv")
kr._playlistIndex = None
kr._setBy = FW("http://a/b.mkv")
check(S, "filekey: string file (URL)", real._getRoomFileKey(kr) == "file:http://a/b.mkv")
kr._setBy = FW(None)
check(S, "filekey: setBy without file -> None", real._getRoomFileKey(kr) is None)
kr._setBy = None
check(S, "filekey: no setBy -> None", real._getRoomFileKey(kr) is None)

# off-switch no-ops must not crash even with None watcher
real.pauseWarningAfter = 0
expect_raise_free(S, "updatePauseWarning no-op when off", lambda: real.updatePauseWarning(Room("x", None), True, None))
real.yapTimer = False
real.bufferPause = True
expect_raise_free(S, "updateYapTimer no-op when off", lambda: real.updateYapTimer(Room("x", None), True, None))
expect_raise_free(S, "updatePauseWarning room=None", lambda: (setattr(real, 'pauseWarningAfter', 10), real.updatePauseWarning(None, True, None)))
real.pauseWarningAfter = 0

# _stopPauseWarningTimer robustness (nothing armed / already stopped)
rr = Room("rr", None)
expect_raise_free(S, "_stopPauseWarningTimer idle-safe", lambda: (real._stopPauseWarningTimer(rr), real._stopPauseWarningTimer(rr)))
check(S, "_stop clears active flag", rr._pauseWarningActive is False)

# _firePauseWarning when already unpaused -> no chat, no loop
real.pauseWarningAfter = 10; real.pauseWarningInterval = 10; real.pauseWarningMessage = "m"
rp = Room("rp", None); rp._playState = Room.STATE_PLAYING
rp._watchers = {}
real._firePauseWarning(rp)
check(S, "_firePauseWarning aborts if playing", rp._pauseWarningActive is False and rp._pauseWarningTimer is None)

# ---------------- Suite C: gating matrices ----------------
S = "C:Gating"
class FakeWatcher:
    def __init__(self, name, version, features):
        self._name, self._version, self._features = name, version, features
        self.chats = []
    def getName(self): return self._name
    def isAfk(self): return False
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def sendChatMessage(self, message, skipIfSupportsFeature=None):
        # replicates server.Watcher.sendChatMessage gating exactly
        if meetsMinVersion(self._version, constants.CHAT_MIN_VERSION):
            if skipIfSupportsFeature and self.supportsFeature(skipIfSupportsFeature):
                return
            self.chats.append((message["username"], message["message"]))

gf = SyncFactory.__new__(SyncFactory)
gf.yapTimer = True
gf.pauseWarningAfter = 300; gf.pauseWarningInterval = 300; gf.pauseWarningMessage = "W {}!"

groom = Room("g", None); groom._playState = Room.STATE_PAUSED
groom.yapStartPause("Alice"); groom._yapPauseStartedAt = time.time() - 10
matrix = {
    "modernBoth":  FakeWatcher("modernBoth",  "1.7.6", {"yapTimer": True,  "pauseWarning": True}),
    "yapOnly":     FakeWatcher("yapOnly",     "1.7.6", {"yapTimer": True}),
    "pwOnly":      FakeWatcher("pwOnly",      "1.7.6", {"pauseWarning": True}),
    "plainChat":   FakeWatcher("plainChat",   "1.6.0", {}),
    "preChat":     FakeWatcher("preChat",     "1.4.0", {}),
}
groom._watchers = dict(matrix)

gf._broadcastYapToRoom(groom, "Alice", "yapmsg")
gf._broadcastPauseWarningChat(groom)
yap_expect = {"modernBoth": 0, "yapOnly": 0, "pwOnly": 1, "plainChat": 1, "preChat": 0}
pw_expect  = {"modernBoth": 0, "yapOnly": 1, "pwOnly": 0, "plainChat": 1, "preChat": 0}
for name, w in matrix.items():
    yap_n = sum(1 for u, m in w.chats if m == "yapmsg")
    pw_n = sum(1 for u, m in w.chats if m.startswith("W "))
    check(S, "chat matrix yap -> {}".format(name), yap_n == yap_expect[name], "got {} want {}".format(yap_n, yap_expect[name]))
    check(S, "chat matrix pw  -> {}".format(name), pw_n == pw_expect[name], "got {} want {}".format(pw_n, pw_expect[name]))
pw_msgs = [m for u, m in matrix["plainChat"].chats if m.startswith("W ")]
check(S, "pw chat fills duration", pw_msgs and "00:10" in pw_msgs[0], repr(pw_msgs))
check(S, "pw chat attributed to pauser", any(u == "Alice" for u, m in matrix["plainChat"].chats if m.startswith("W ")))

def make_proto(yap_on, pw_flag, pw_active, supports_yap, supports_pw, paused_secs=10):
    p = SyncServerProtocol.__new__(SyncServerProtocol)
    fac = SyncFactory.__new__(SyncFactory)
    fac.yapTimer = yap_on; fac.pauseWarningAfter = pw_flag
    fac.pauseWarningInterval = pw_flag; fac.pauseWarningMessage = "W {}!"
    p._factory = fac
    p._pingService = PingService()
    p._clientLatencyCalculationArrivalTime = 0; p._clientLatencyCalculation = 0
    p.serverIgnoringOnTheFly = 0; p.clientIgnoringOnTheFly = 0
    rm = Room("sr", None); rm._playState = Room.STATE_PAUSED
    rm.yapStartPause("A"); rm._yapPauseStartedAt = time.time() - paused_secs
    rm._pauseWarningActive = pw_active
    feats = {"yapTimer": supports_yap, "pauseWarning": supports_pw}
    class WT:
        def getRoom(self): return rm
        def getName(self): return "A"
        def supportsFeature(self, k): return feats.get(k, False)
    p._watcher = WT(); p._sent = []
    p.sendMessage = lambda m: p._sent.append(m)
    return p

# (yapOn, pwFlag, pwActive, supY, supP) -> expect (yap field?, pw field?)
state_cases = [
    ((True,  300, True,  True,  True),  (True,  True)),
    ((True,  300, True,  False, False), (False, False)),
    ((True,  300, False, True,  True),  (True,  False)),
    ((False, 300, True,  True,  True),  (False, True)),
    ((True,  0,   True,  True,  True),  (True,  False)),
    ((False, 0,   False, True,  True),  (False, False)),
    ((True,  300, True,  True,  False), (True,  False)),
    ((True,  300, True,  False, True),  (False, True)),
]
for args, (want_yap, want_pw) in state_cases:
    p = make_proto(*args)
    p.sendState(5.0, True, False, None, False)
    st = p._sent[0]["State"]
    got = ("yapTimer" in st, "pauseWarning" in st)
    check(S, "State matrix {} -> yap={} pw={}".format(args, want_yap, want_pw), got == (want_yap, want_pw), "got {}".format(got))

p = make_proto(True, 300, True, True, True, paused_secs=42)
p.sendState(5.0, True, False, None, False)
st = p._sent[0]["State"]
check(S, "State yap payload sane", st["yapTimer"]["paused"] is True and 41 < st["yapTimer"]["current"] < 44,
      "current=%.2f total=%.2f" % (st["yapTimer"]["current"], st["yapTimer"]["total"]))
check(S, "State yap payload carries afkTotal split field (0 with no AFK watcher)",
      "afkTotal" in st["yapTimer"] and st["yapTimer"]["afkTotal"] == 0, repr(st["yapTimer"]))
check(S, "State pw payload duration", "00:42" in st["pauseWarning"]["message"], repr(st["pauseWarning"]))

# watcher without room must not crash
p = make_proto(True, 300, True, True, True)
p._watcher.getRoom = lambda: None
expect_raise_free(S, "sendState with room=None safe", lambda: p.sendState(5.0, True, False, None, False))

# ---------------- Suite C2: ControlledRoom authority guard ----------------
S = "C2:Authority"
server_mock = mock.Mock()
conn = mock.Mock()
conn.isLogged.return_value = True
w = Watcher(server_mock, conn, "pleb")
croom = ControlledRoom("+room:ABCDEFGHIJKL", None)
croom.addWatcher(w)
w._sendStateTimer.stop() if w._sendStateTimer and w._sendStateTimer.running else None
server_mock.reset_mock()
w.updateState(5.0, True, False, 0)  # pleb (non-controller) tries to pause
check(S, "non-controller pause: room stays as-is", croom.isPaused(), "ControlledRoom ignores setPaused from non-controller")
check(S, "non-controller pause: yap hook NOT called", not server_mock.updateYapTimer.called)
check(S, "non-controller pause: pw hook NOT called", not server_mock.updatePauseWarning.called)

nroom = Room("n", None)
w2 = Watcher(server_mock, conn, "user")
nroom.addWatcher(w2)
w2._sendStateTimer.stop() if w2._sendStateTimer and w2._sendStateTimer.running else None
server_mock.reset_mock()
nroom._playState = Room.STATE_PLAYING
w2.updateState(5.0, True, False, 0)  # normal room: pause is authoritative
check(S, "normal room pause: hooks called", server_mock.updateYapTimer.called and server_mock.updatePauseWarning.called)
check(S, "normal room pause: hook args (paused=True)",
      server_mock.updateYapTimer.call_args[0][1] is True and server_mock.updatePauseWarning.call_args[0][1] is True)
server_mock.reset_mock()
w2.updateState(6.0, True, False, 0)  # same paused state again -> no pauseChanged
check(S, "no pauseChanged -> hooks NOT re-called", not server_mock.updateYapTimer.called and not server_mock.updatePauseWarning.called)

# ---------------- Suite D: client & players ----------------
S = "D:Client"
from syncplay.client import UiManager
from syncplay.players.basePlayer import BasePlayer, DummyPlayer
from syncplay.players.mpv import MpvPlayer
from syncplay.players.mpvnet import MpvnetPlayer
from syncplay.players.iina import IinaPlayer
from syncplay.players.vlc import VlcPlayer
from syncplay.players.mplayer import MplayerPlayer
from syncplay.players.memento import MementoPlayer

caps = {}
for cls in (BasePlayer, DummyPlayer, MpvPlayer, MpvnetPlayer, IinaPlayer, VlcPlayer, MplayerPlayer, MementoPlayer):
    caps[cls.__name__] = (getattr(cls, "yapTimerOSDSupported", False), getattr(cls, "pauseWarningOSDSupported", False))
print("    capability map:", caps)
for name in ("MpvPlayer", "MpvnetPlayer", "IinaPlayer", "MementoPlayer"):
    expected = (True, True)  # Memento subclasses Mpv? verify dynamically below
for name, (y, pw_) in caps.items():
    if name in ("MpvPlayer", "MpvnetPlayer", "IinaPlayer"):
        check(S, "capability {} = (True, True)".format(name), (y, pw_) == (True, True), str((y, pw_)))
    elif name in ("BasePlayer", "DummyPlayer", "VlcPlayer", "MplayerPlayer"):
        check(S, "capability {} = (False, False)".format(name), (y, pw_) == (False, False), str((y, pw_)))
    else:
        check(S, "capability {} recorded".format(name), True, str((y, pw_)))  # informational (e.g. Memento)
check(S, "getattr-default path (class without attrs)", getattr(type("X", (), {}), "pauseWarningOSDSupported", False) is False)

expect_raise_free(S, "BasePlayer.updateYapTimerOSD no-op", lambda: BasePlayer().updateYapTimerOSD("t"))
expect_raise_free(S, "BasePlayer.updatePauseWarningOSD no-op", lambda: BasePlayer().updatePauseWarningOSD("t"))

class FakePlayer:
    yapTimerOSDSupported = True
    pauseWarningOSDSupported = True
    def __init__(self): self.yap, self.pw = [], []
    def updateYapTimerOSD(self, t): self.yap.append(t)
    def updatePauseWarningOSD(self, t): self.pw.append(t)
class FakeClient:
    def __init__(self, player): self._player = player

fp = FakePlayer()
ui = UiManager(FakeClient(fp), mock.Mock())
ui.updateYapTimer(True, 12, 130, 30)   # total 02:10 = 01:40 active + 00:30 AFK
ui.updateYapTimer(True, 13, 131, 30)
ui.updateYapTimer(False, 0, 130, 30)   # resume: final total shown once
ui.updateYapTimer(False, 0, 130, 30)   # already resumed -> silent
check(S, "UiManager yap: 2 paused + 1 resume-total + silence", len(fp.yap) == 3, str(fp.yap))
check(S, "UiManager yap paused OSD renders active/AFK split",
      "01:40 active" in fp.yap[0] and "00:30 AFK" in fp.yap[0], fp.yap[0])
check(S, "UiManager yap resume shows total + active/AFK split once",
      "02:10" in fp.yap[2] and "01:40 active" in fp.yap[2] and "00:30 AFK" in fp.yap[2], fp.yap[2])
ui.updatePauseWarning("W 00:12!")
check(S, "UiManager pw passthrough", fp.pw == ["W 00:12!"])
ui_nop = UiManager(FakeClient(None), mock.Mock())
expect_raise_free(S, "UiManager yap: no player guard", lambda: ui_nop.updateYapTimer(True, 1, 1))
expect_raise_free(S, "UiManager pw: no player guard", lambda: ui_nop.updatePauseWarning("x"))

# mpv payload check without a real player process
mpv = MpvPlayer.__new__(MpvPlayer)
expect_raise_free(S, "mpv pw OSD safe without listener", lambda: mpv.updatePauseWarningOSD("x"))
expect_raise_free(S, "mpv yap OSD safe without listener", lambda: mpv.updateYapTimerOSD("x"))
class FakeListener:
    def __init__(self): self.lines = []
    def sendLine(self, l): self.lines.append(l)
mpv._listener = FakeListener()
mpv.updatePauseWarningOSD('Resume "now" {5}!')
line = mpv._listener.lines[0]
check(S, "mpv pw payload routing", line[0:3] == ["script-message-to", "syncplayintf", "pausewarning-osd"], str(line[:3]))
check(S, "mpv pw payload sanitized (braces escaped, quotes kept)", "\\\\{" in line[3] and '\\"' in line[3], repr(line[3]))
mpv.updateYapTimerOSD("Yap 00:05")
check(S, "mpv yap payload routing", mpv._listener.lines[1][2] == "yaptimer-osd")

# client protocol parse of incoming State extras
from syncplay.protocols import SyncClientProtocol
cp = SyncClientProtocol.__new__(SyncClientProtocol)
cp.hadFirstStateUpdate = True
cp.clientIgnoringOnTheFly = 0
cp.serverIgnoringOnTheFly = 0
cp._pendingStateChange = False
cp._pingService = PingService()
calls = {"yap": [], "pw": []}
client_mock = mock.Mock()
client_mock.ui.updateYapTimer = lambda *a: calls["yap"].append(a)
client_mock.ui.updatePauseWarning = lambda *a: calls["pw"].append(a)
client_mock.getLocalState.return_value = (None, None, None, False)
cp._client = client_mock
cp.sendMessage = lambda m: None
cp.handleState({"ping": {"latencyCalculation": 0, "serverRtt": 0},
                "playstate": {"position": 1, "paused": True, "setBy": "x"},
                "yapTimer": {"paused": True, "current": 3.2, "total": 9.9, "afkTotal": 4.4, "duration": 6000},
                "pauseWarning": {"message": "W!"}})
check(S, "client parses yapTimer field (incl. afkTotal split + duration)", calls["yap"] == [(True, 3.2, 9.9, 4.4, 6000)], str(calls["yap"]))
check(S, "client parses pauseWarning field", calls["pw"] == [("W!",)], str(calls["pw"]))
calls["yap"].clear(); calls["pw"].clear()
cp.handleState({"ping": {"latencyCalculation": 0, "serverRtt": 0},
                "playstate": {"position": 1, "paused": True, "setBy": "x"}})
check(S, "no extras -> no UI calls (legacy server compat)", calls == {"yap": [], "pw": []})
cp.handleState({"ping": {"latencyCalculation": 0, "serverRtt": 0},
                "playstate": {"position": 1, "paused": True, "setBy": "x"},
                "yapTimer": {}, "pauseWarning": {}})
check(S, "malformed empty extras -> defaults, no crash", calls["yap"] == [(False, 0, 0, 0, None)] and calls["pw"] == [("",)])

# ---------------- Suite E: i18n integrity ----------------
S = "E:i18n"
import syncplay.messages as M
M.setLanguage("en")
keys = ["yap-timer-paused-chat-message", "yap-timer-ongoing-chat-message", "yap-timer-unpaused-chat-message",
        "yap-timer-osd-paused-message", "yap-timer-osd-paused-detail-message",
        "yap-timer-osd-total-message", "server-yap-timer-argument",
        "pause-warning-default-message", "server-pause-warning-after-argument",
        "server-pause-warning-interval-argument", "server-pause-warning-message-argument"]
for k in keys:
    try:
        v = M.getMessage(k)
        check(S, "en key present: " + k, bool(v))
    except KeyError:
        check(S, "en key present: " + k, False, "KeyError")
miss = M.getMissingStrings()
bad = [l for l in miss.splitlines() if "Unused" in l and ("yap" in l.lower() or "pause-warning" in l.lower())]
check(S, "no yap/pw keys leaked into non-English dicts", not bad, repr(bad))
n_missing = len([l for l in miss.splitlines() if "Missing" in l and ("yap" in l.lower() or "pause-warning" in l.lower())])
# Derive the expected count from the live English dict x non-English languages instead of a
# hard-coded magic number, so adding a yap/pw message never silently rebreaks this check.
en_yap_pw_keys = [k for k in M.messages["en"] if "yap" in k.lower() or "pause-warning" in k.lower()]
# Mirror getMissingStrings' own iteration: it skips both "en" and the "CURRENT" pseudo-language.
n_langs = len([lang for lang in M.messages if lang not in ("en", "CURRENT")])
check(S, "translation fallback count = yap/pw keys * non-en langs",
      n_missing == len(en_yap_pw_keys) * n_langs,
      "{} missing vs {} keys * {} langs".format(n_missing, len(en_yap_pw_keys), n_langs))
fmt_checks = [
    ("yap-timer-ongoing-chat-message", 4), ("yap-timer-unpaused-chat-message", 4),
    ("yap-timer-osd-paused-message", 1), ("yap-timer-osd-paused-detail-message", 3),
    ("yap-timer-osd-total-message", 3),
    ("pause-warning-default-message", 1),
]
for k, n in fmt_checks:
    tmpl = M.getMessage(k)
    try:
        tmpl.format(*(["00:0%d" % i for i in range(n)]))
        check(S, "template arity OK: " + k, tmpl.count("{}") == n, "{} placeholders".format(tmpl.count("{}")))
    except Exception as e:
        check(S, "template arity OK: " + k, False, repr(e))

# ---------------- summary ----------------
fails = [x for x in RESULTS if not x[2]]
print("\n===== UNIT SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
