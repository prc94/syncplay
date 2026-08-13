"""E2E S12: the buffer hold over a live server (docs/buffer-pause.md).

Boots a real server and drives it with protocol-faithful clients. One of them reports a stalled
cache on its State heartbeat, exactly as the real client does, and the suite asserts what the rest
of the room actually receives:

- the room is force-paused, and everybody's State says so;
- the room is told in chat - both the capable client and the stock one, which has no other way
  of learning why playback stopped;
- capable clients get the live bufferHold State field, and stop getting it once the hold ends;
- the room resumes by itself when the stall clears, and again when the stalled client simply
  vanishes mid-stall (the staleness rule, which is the only thing standing between a crashed
  client and a room paused for ever);
- --no-buffer-pause turns all of it off.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S12:buffer"
print("--- {} ---".format(SCEN))

CAP_FEATS = {"chat": True, "bufferPause": True}
LEG_FEATS = {"chat": True}  # stock client: chat is the only channel it understands
FILE = {"name": "m.mkv", "duration": 3600, "size": 500}


def pump_until(clients, pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in clients:
            c.act(time.time() - c.t0 if c.t0 else 0)
            c.pump(); c.tick()
        if pred():
            return True
        time.sleep(0.02)
    return False


def start(clients, port):
    for c in clients:
        c.connect(port)
    assert pump_until(clients, lambda: all(x.hello for x in clients)), "hellos"
    now = time.time()
    for c in clients:
        c.t0 = now
    return now


def paused_states(c):
    return [(t, p) for (t, p) in evts(c, "state") if p is True]


# ---------------------------------------------------------------- 1) hold, announce, release
srv = ServerBoot(19081, ["--salt", "testsalt"])
# Both start the room playing, then become ordinary followers: a client that keeps re-asserting
# its own playstate would look like a user overriding the hold on every single tick.
LEAD = MiniClient("lead", "d", "1.7.6", CAP_FEATS, role="leader",
                  schedule=[(0.0, "unpause"), (0.5, "follow")], file_=FILE)
STALLER = MiniClient("staller", "d", "1.7.6", CAP_FEATS, role="leader",
                     schedule=[(0.0, "unpause"), (0.5, "follow"), (1.0, "buffer"), (5.0, "unbuffer")],
                     file_=FILE)
LEG = MiniClient("leg", "d", "1.6.0", LEG_FEATS, file_=FILE)
clients = [LEAD, STALLER, LEG]
start(clients, 19081)

# Let the room settle into playing before anybody stalls.
pump_until(clients, lambda: False, timeout=1.2)
gotHold = pump_until(clients, lambda: chats_matching(LEAD, "is buffering"), timeout=6.0)
check(SCEN, "capable peer is told in chat that someone is buffering", gotHold,
      repr(chats_matching(LEAD, "is buffering")))
check(SCEN, "stock peer is told too (chat is all it has)", chats_matching(LEG, "is buffering"),
      repr(chats_matching(LEG, "is buffering")))
check(SCEN, "the chat names the stalled user",
      any("staller" in u for (t, (u, m)) in evts(LEAD, "chat") if "is buffering" in m),
      repr(evts(LEAD, "chat")))
check(SCEN, "the room is force-paused", paused_states(LEAD), repr(evts(LEAD, "state")[-3:]))

gotField = pump_until(clients, lambda: evts(LEAD, "hold"), timeout=4.0)
check(SCEN, "capable peer gets the live bufferHold State field", gotField, repr(evts(LEAD, "hold")[:1]))
if evts(LEAD, "hold"):
    payload = evts(LEAD, "hold")[-1][1]
    check(SCEN, "bufferHold names the user", payload.get("user") == "staller", repr(payload))
    check(SCEN, "bufferHold reports how long the wait has been", payload.get("elapsed", -1) >= 0, repr(payload))
check(SCEN, "stock peer is never sent the bufferHold field", not evts(LEG, "hold"), repr(evts(LEG, "hold")))

gotRelease = pump_until(clients, lambda: chats_matching(LEAD, "resuming"), timeout=8.0)
check(SCEN, "the room is told it is resuming", gotRelease, repr(chats_matching(LEAD, "resuming")))
holdsBefore = len(evts(LEAD, "hold"))
pump_until(clients, lambda: False, timeout=2.0)
check(SCEN, "the bufferHold field stops once the hold ends", len(evts(LEAD, "hold")) == holdsBefore,
      "{} -> {}".format(holdsBefore, len(evts(LEAD, "hold"))))
check(SCEN, "the room actually resumes playing",
      any(p is False for (t, p) in evts(LEAD, "state")[-5:]), repr(evts(LEAD, "state")[-5:]))

for c in clients:
    c.close()
srv.clean_log(SCEN)
srv.stop()

# ---------------------------------------------------------------- 2) the stalled client freezes
# The one failure mode that has no recovery message of its own: a client that stalls and then stops
# responding altogether. Its socket stays open, so nothing tells the server anything - only the
# staleness rule stands between that and a room paused for ever.
srv = ServerBoot(19082, ["--salt", "testsalt"])
LEAD = MiniClient("lead", "d", "1.7.6", CAP_FEATS, role="leader",
                  schedule=[(0.0, "unpause"), (0.5, "follow")], file_=FILE)
GHOST = MiniClient("ghost", "d", "1.7.6", CAP_FEATS, role="leader",
                   schedule=[(0.0, "unpause"), (0.5, "follow"), (1.0, "buffer")], file_=FILE)
clients = [LEAD, GHOST]
start(clients, 19082)
held = pump_until(clients, lambda: chats_matching(LEAD, "is buffering"), timeout=6.0)
check(SCEN, "frozen client: the hold is in force", held, repr(chats_matching(LEAD, "is buffering")))
# Socket left open, but GHOST is no longer pumped: it sends nothing further, exactly like a client
# whose process has locked up.
resumed = pump_until([LEAD], lambda: chats_matching(LEAD, "resuming"), timeout=10.0)
check(SCEN, "a client that goes silent mid-stall stops holding the room", resumed,
      repr(chats_matching(LEAD, "resuming")))
check(SCEN, "and it stops within the staleness window, not the protocol timeout",
      chats_matching(LEAD, "resuming") and
      chats_matching(LEAD, "resuming")[0][0] < chats_matching(LEAD, "is buffering")[0][0] + 6.0,
      repr(chats_matching(LEAD, "resuming")))

GHOST.close()
LEAD.close()
srv.clean_log(SCEN)
srv.stop()

# ---------------------------------------------------------------- 3) --no-buffer-pause
srv = ServerBoot(19083, ["--salt", "testsalt", "--no-buffer-pause"])
LEAD = MiniClient("lead", "d", "1.7.6", CAP_FEATS, role="leader",
                  schedule=[(0.0, "unpause"), (0.5, "follow")], file_=FILE)
STALLER = MiniClient("staller", "d", "1.7.6", CAP_FEATS, role="leader",
                     schedule=[(0.0, "unpause"), (0.5, "follow"), (1.0, "buffer")], file_=FILE)
clients = [LEAD, STALLER]
start(clients, 19083)
pump_until(clients, lambda: False, timeout=5.0)
check(SCEN, "--no-buffer-pause: nothing is announced", not chats_matching(LEAD, "is buffering"),
      repr(chats_matching(LEAD, "is buffering")))
check(SCEN, "--no-buffer-pause: no bufferHold field", not evts(LEAD, "hold"), repr(evts(LEAD, "hold")))

for c in clients:
    c.close()
srv.clean_log(SCEN)
srv.stop()

# ---------------------------------------------------------------- 4) where everyone ends up
# The promise is that the room waits *at the stall point* - not that it carries on and leaves the
# stalled watcher behind. Nothing else measures this: the other scenarios only assert that a pause
# and a resume happened, which would also be true of a hold that quietly loses four seconds.
srv = ServerBoot(19084, ["--salt", "testsalt"])
LEAD = MiniClient("lead", "d", "1.7.6", CAP_FEATS, role="leader",
                  schedule=[(0.0, "unpause"), (0.5, "follow")], file_=FILE)
STALLER = MiniClient("staller", "d", "1.7.6", CAP_FEATS, role="leader",
                     schedule=[(0.0, "unpause"), (0.5, "follow"), (1.5, "buffer"), (5.5, "unbuffer")],
                     file_=FILE)
clients = [LEAD, STALLER]
t0 = start(clients, 19084)

# Both report a position that advances in real time, as a playing player would. The staller's
# freezes when it starts buffering, which is exactly what a cache stall looks like on the wire.
frozen_at = [None]
def advance_positions():
    now = time.time() - t0
    LEAD.position = 5.0 + now
    if STALLER.buffering:
        if frozen_at[0] is None:
            frozen_at[0] = STALLER.position
    elif frozen_at[0] is None:
        STALLER.position = 5.0 + now
    else:
        STALLER.position = frozen_at[0]  # a cache that has filled resumes from where it stopped

deadline = time.time() + 9.0
while time.time() < deadline:
    advance_positions()
    for c in clients:
        c.act(time.time() - c.t0)
        c.pump(); c.tick()
    time.sleep(0.02)

held = [(t, p) for (t, p) in evts(LEAD, "pos") if p is not None]
paused_at = [t for (t, p) in evts(LEAD, "state") if p is True]
check(SCEN, "position: the room actually stalled and resumed", paused_at and chats_matching(LEAD, "resuming"),
      "paused_at={} resumed={}".format(paused_at[:1], bool(chats_matching(LEAD, "resuming"))))
if paused_at and frozen_at[0] is not None:
    duringHold = [p for (t, p) in held if t >= paused_at[0]]
    drift = max(abs(p - frozen_at[0]) for p in duringHold) if duringHold else None
    check(SCEN, "position: the room waits at the stall point, not past it",
          drift is not None and drift <= 2.0,
          "staller froze at {:.1f}; room ranged {:.1f}..{:.1f} (drift {:.1f}s)".format(
              frozen_at[0], min(duringHold), max(duringHold), drift or -1))
    check(SCEN, "position: nobody is sent backwards further than the stall lasted",
          all(p >= frozen_at[0] - 5.0 for p in duringHold),
          "min broadcast {:.1f} vs stall point {:.1f}".format(min(duringHold), frozen_at[0]))

for c in clients:
    c.close()
srv.clean_log(SCEN)
srv.stop()

# ---------------------------------------------------------------- 5) a file change during a hold
# The hold survives Set:file, so a release can restore "playing" against a file nobody was
# watching when the hold started. Asserted here rather than reasoned about.
srv = ServerBoot(19085, ["--salt", "testsalt"])
LEAD = MiniClient("lead", "d", "1.7.6", CAP_FEATS, role="leader",
                  schedule=[(0.0, "unpause"), (0.5, "follow"), (3.0, "setfile:episode2.mkv")], file_=FILE)
STALLER = MiniClient("staller", "d", "1.7.6", CAP_FEATS, role="leader",
                     schedule=[(0.0, "unpause"), (0.5, "follow"), (1.5, "buffer"),
                               (3.0, "setfile:episode2.mkv"), (5.0, "unbuffer")], file_=FILE)
clients = [LEAD, STALLER]
start(clients, 19085)
pump_until(clients, lambda: chats_matching(LEAD, "is buffering"), timeout=6.0)
check(SCEN, "file change: the hold is in force first", chats_matching(LEAD, "is buffering"))
pump_until(clients, lambda: False, timeout=6.0)
resumedAfterSwitch = [p for (t, p) in evts(LEAD, "state")[-4:] if p is False]
check(SCEN, "file change: a hold released after a file switch does not leave the room stuck",
      chats_matching(LEAD, "resuming"), repr(chats_matching(LEAD, "resuming")))
print("   [info] file-change tail states: {}".format(evts(LEAD, "state")[-4:]))
print("   [info] file-change tail positions: {}".format(evts(LEAD, "pos")[-4:]))

for c in clients:
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== BUFFER E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
