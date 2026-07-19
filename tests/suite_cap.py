"""Unit suite for the 1-hour give-up cap."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import time, sys
import unittest.mock as mock
from syncplay import constants
from syncplay.utils import meetsMinVersion
from syncplay.server import Room, SyncFactory
from syncplay.protocols import SyncServerProtocol, PingService

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Cap :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

MAX = constants.YAP_TIMER_MAX_PAUSE
check("constant is one hour", MAX == 3600)

# --- trip semantics ---
r = Room("r", None)
r._yapTotalThisFile = 500.0
r.yapStartPause("Alice")
r._yapPauseStartedAt = time.time() - (MAX - 1)          # 59:59
check("just under cap: not expired", r.yapCheckExpired() is False)
check("just under cap: total intact", r._yapTotalThisFile == 500.0)
r._yapPauseStartedAt = time.time() - (MAX + 1)          # 1:00:01
check("over cap: trips", r.yapCheckExpired() is True)
check("trip wipes per-file total", r._yapTotalThisFile == 0.0)
check("clock kept (pause-warning text stays truthful)", r._yapPauseStartedAt is not None)
r._yapTotalThisFile = 123.0                              # simulate something writing later
check("sticky: second check stays expired, no re-wipe", r.yapCheckExpired() is True and r._yapTotalThisFile == 123.0)
r._yapTotalThisFile = 0.0

# --- unpause after expiry: discard, no summary, flag survives until next pause ---
e = r.yapEndPause()
check("expired pause discarded (endPause -> None)", e is None)
check("no accumulation from expired pause", r._yapTotalThisFile == 0.0)
check("clock cleared on unpause", r._yapPauseStartedAt is None)
check("flag survives unpause (field stays suppressed during play)", r._yapExpired is True)
check("expired with no clock: check stays True, no crash", r.yapCheckExpired() is True)

# --- rearm on next pause ---
r.yapStartPause("Bob")
check("next pause clears expiry", r._yapExpired is False)
check("fresh count", r.yapCurrentElapsed() < 1.0)
r._yapPauseStartedAt = time.time() - 30
e = r.yapEndPause()
check("post-rearm pause accumulates normally", e is not None and 29 < r.yapTotal() < 31,
      "total %.1f" % r.yapTotal())

# --- yapReset clears expiry too (file change / room emptied) ---
r._yapExpired = True
r.yapReset()
check("yapReset clears expiry", r._yapExpired is False)

# --- idempotent-start guard: re-pause report while already paused must NOT clear expiry ---
r2 = Room("r2", None)
r2.yapStartPause("A")
r2._yapPauseStartedAt = time.time() - (MAX + 5)
r2.yapCheckExpired()
r2.yapStartPause("B")                                    # duplicate start mid-pause (protocol race)
check("duplicate start mid-pause does not un-expire", r2._yapExpired is True)

# --- SyncFactory guards ---
f = SyncFactory.__new__(SyncFactory)
f.yapTimer = True
f.pauseWarningAfter = 300; f.pauseWarningInterval = 300; f.pauseWarningMessage = "W {}!"

class FakeWatcher:
    def __init__(self): self.chats = []
    def getName(self): return "w"
    def supportsFeature(self, ft): return False
    def sendChatMessage(self, m, skipIfSupportsFeature=None): self.chats.append(m["message"])

def expired_room():
    rm = Room("x", None)
    rm._playState = Room.STATE_PAUSED
    rm._watchers = {"w": FakeWatcher()}
    rm.yapStartPause("A")
    rm._yapPauseStartedAt = time.time() - (MAX + 10)
    return rm

rm = expired_room()
f._startYapTicker(rm)
f._yapTick(rm)
w = rm._watchers["w"]
check("_yapTick on expired room: no chat, ticker stopped", w.chats == [] and rm._yapTickTimer is None)

rm = expired_room()
f._firePauseWarning(rm)
check("_firePauseWarning aborts on expired room", rm._watchers["w"].chats == [] and rm._pauseWarningActive is False
      and rm._pauseWarningTimer is None)

rm = expired_room()
rm._pauseWarningActive = True
rm._pauseWarningTimer = None
f._startPauseWarningTimer(rm)          # arms delayed; simulate the repeat path directly
rm._pauseWarningActive = True
f._repeatPauseWarning(rm)
check("_repeatPauseWarning stops on expired room", rm._watchers["w"].chats == [] and rm._pauseWarningActive is False)
f._stopPauseWarningTimer(rm)

# --- sendState suppression (the 1s lazy trip point) ---
def make_proto(rm, sup=True):
    p = SyncServerProtocol.__new__(SyncServerProtocol)
    p._factory = f
    p._pingService = PingService()
    p._clientLatencyCalculationArrivalTime = 0; p._clientLatencyCalculation = 0
    p.serverIgnoringOnTheFly = 0; p.clientIgnoringOnTheFly = 0
    class WT:
        def getRoom(self): return rm
        def getName(self): return "A"
        def supportsFeature(self, k): return sup
    p._watcher = WT(); p._sent = []
    p.sendMessage = lambda m: p._sent.append(m)
    return p

# under cap: both fields flow
rm = expired_room(); rm._yapPauseStartedAt = time.time() - 100; rm._yapExpired = False
rm._pauseWarningActive = True
p = make_proto(rm); p.sendState(5.0, True, False, None, False)
st = p._sent[0]["State"]
check("under cap: both fields emitted", "yapTimer" in st and "pauseWarning" in st)
check("pw message shows true near-cap duration", "01:40" in st["pauseWarning"]["message"], st["pauseWarning"]["message"])

# over cap: sendState itself trips it and suppresses both
rm = expired_room(); rm._yapExpired = False   # not yet tripped - sendState must trip lazily
rm._pauseWarningActive = True
p = make_proto(rm); p.sendState(5.0, True, False, None, False)
st = p._sent[0]["State"]
check("over cap: sendState trips lazily and suppresses both fields",
      "yapTimer" not in st and "pauseWarning" not in st and rm._yapExpired is True)

# post-expiry unpause: field stays suppressed during play (no spurious resume toast)
rm._playState = Room.STATE_PLAYING
rm.yapEndPause()
p = make_proto(rm); p.sendState(5.0, False, False, None, False)
check("post-expiry play: yap field still suppressed (no 00:00 toast)", "yapTimer" not in p._sent[0]["State"])
# next pause rearms emission
rm._playState = Room.STATE_PAUSED
rm.yapStartPause("C")
p = make_proto(rm); p.sendState(5.0, True, False, None, False)
st = p._sent[0]["State"]
check("next pause: yap field flows again from 00:00",
      "yapTimer" in st and st["yapTimer"]["current"] < 1.0 and st["yapTimer"]["total"] < 1.0,
      "current=%.2f total=%.2f" % (st["yapTimer"].get("current", -1), st["yapTimer"].get("total", -1)))

# watcher without room still safe
p = make_proto(None); p.sendState(5.0, True, False, None, False)
check("sendState with room=None safe", "yapTimer" not in p._sent[0]["State"])

fails = [x for x in RESULTS if not x[1]]
print("\n===== CAP SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
