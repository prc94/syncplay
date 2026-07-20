"""E2E S11: AFK state suppresses the pause warning over a live server.

Boots a real server with a short pause-warning threshold and the yap timer on, then:
- confirms a capable client gets the pauseWarning State field and a legacy client the chat
  fallback once a pause crosses the threshold;
- has a client go AFK and confirms both the State field and the chat fallback stop, while the
  yap-timer State field keeps flowing;
- confirms the warning resumes when the AFK client returns and (separately) when it disconnects;
- confirms a stock client can toggle AFK via the /afk chat command.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S11:afk"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19051, ["--yap-timer", "--pause-warning-after", "2", "--pause-warning-interval", "1",
                         "--salt", "testsalt"])

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
def pump_for(clients, seconds):
    pump_until(clients, lambda: False, timeout=seconds)
def sets_of(c, key):
    return [(t, s[key]) for (t, k, s) in c.events if k == "set" and key in s]
def count_since(events, t_mark):
    return len([e for e in events if e[0] >= t_mark])

CAP_FEATS = {"chat": True, "pauseWarning": True, "yapTimer": True, "afk": True}
LEG_FEATS = {"chat": True}  # stock <1.7: understands chat + ready only

# LEAD pauses at t=0.4 and later goes AFK; CAP is a capable observer; LEG a legacy observer.
LEAD = MiniClient("lead", "d", "1.7.6", CAP_FEATS, role="leader",
                  schedule=[(0.0, "unpause"), (0.6, "pause")], file_={"name": "m.mkv", "duration": 100, "size": 500})
CAP = MiniClient("cap", "d", "1.7.6", CAP_FEATS, file_={"name": "m.mkv", "duration": 100, "size": 500})
LEG = MiniClient("leg", "d", "1.6.0", LEG_FEATS, file_={"name": "m.mkv", "duration": 100, "size": 500})
for c in (LEAD, CAP, LEG):
    c.connect(19051)
assert pump_until([LEAD, CAP, LEG], lambda: all(x.hello for x in (LEAD, CAP, LEG))), "hellos"
now = time.time()
for c in (LEAD, CAP, LEG):
    c.t0 = now

# 1) pause crosses threshold -> capable gets pauseWarning State field, legacy gets chat fallback
assert pump_until([LEAD, CAP, LEG], lambda: evts(CAP, "pw") and chats_matching(LEG, "Paused for"), timeout=8.0), \
    "warning did not fire"
check(SCEN, "capable observer receives pauseWarning State field", len(evts(CAP, "pw")) >= 1)
check(SCEN, "legacy observer receives chat fallback", len(chats_matching(LEG, "Paused for")) >= 1)
check(SCEN, "capable observer gets NO chat fallback (State only)", not chats_matching(CAP, "Paused for"))
check(SCEN, "capable observer receives yapTimer State field", len(evts(CAP, "yap")) >= 1)

# 2) LEAD goes AFK -> warning suppressed room-wide; yap timer keeps flowing
LEAD.send({"Set": {"afk": {"isAfk": True}}})
assert pump_until([LEAD, CAP, LEG], lambda: sets_of(CAP, "afk")), "afk not propagated"
pump_for([LEAD, CAP, LEG], 0.6)  # let the suppression take effect
mark = time.time() - now
pump_for([LEAD, CAP, LEG], 2.5)  # more than one warning interval
check(SCEN, "capable client got Set:afk for the AFK user",
      any(s.get("isAfk") and s.get("username") == "lead" for _, s in sets_of(CAP, "afk")), repr(sets_of(CAP, "afk")))
check(SCEN, "legacy client got AFK chat notice", len(chats_matching(LEG, "AFK")) >= 1, repr(chats_matching(LEG, "AFK")))
check(SCEN, "pauseWarning State field stops while AFK", count_since(evts(CAP, "pw"), mark) == 0,
      "pw events after AFK: {}".format(count_since(evts(CAP, "pw"), mark)))
check(SCEN, "chat fallback stops while AFK", count_since(chats_matching(LEG, "Paused for"), mark) == 0,
      "chats after AFK: {}".format(count_since(chats_matching(LEG, "Paused for"), mark)))
check(SCEN, "yap timer keeps flowing during AFK (not suppressed)", count_since(evts(CAP, "yap"), mark) >= 1)
check(SCEN, "AFK user forced not-ready (broadcast ready=False)",
      any(s.get("username") == "lead" and s.get("isReady") is False for _, s in sets_of(CAP, "ready")), repr(sets_of(CAP, "ready")))

# 3) LEAD returns -> warning resumes (pause still over threshold)
LEAD.send({"Set": {"afk": {"isAfk": False}}})
mark2 = time.time() - now
resumed = pump_until([LEAD, CAP, LEG], lambda: count_since(evts(CAP, "pw"), mark2) >= 1
                     and count_since(chats_matching(LEG, "Paused for"), mark2) >= 1, timeout=6.0)
check(SCEN, "warning resumes after AFK cleared (State + chat)", resumed,
      "pw={} chat={}".format(count_since(evts(CAP, "pw"), mark2), count_since(chats_matching(LEG, "Paused for"), mark2)))

# 4) AFK via disconnect: a second AFK client leaving restores the warning
AFKER = MiniClient("afker", "d", "1.7.6", CAP_FEATS, file_={"name": "m.mkv", "duration": 100, "size": 500})
AFKER.connect(19051)
assert pump_until([LEAD, CAP, LEG, AFKER], lambda: AFKER.hello)
AFKER.t0 = now
AFKER.send({"Set": {"afk": {"isAfk": True}}})
assert pump_until([LEAD, CAP, LEG, AFKER], lambda: sets_of(CAP, "afk") and
                  any(s.get("username") == "afker" and s.get("isAfk") for _, s in sets_of(CAP, "afk")))
pump_for([LEAD, CAP, LEG, AFKER], 0.6)
mark3 = time.time() - now
pump_for([LEAD, CAP, LEG, AFKER], 2.0)
check(SCEN, "warning suppressed again while second client AFK", count_since(evts(CAP, "pw"), mark3) == 0)
AFKER.close()  # disconnect while still AFK
mark4 = time.time() - now
restored = pump_until([LEAD, CAP, LEG], lambda: count_since(evts(CAP, "pw"), mark4) >= 1, timeout=6.0)
check(SCEN, "warning resumes after AFK client disconnects (hasAfkWatcher self-heals)", restored,
      "pw after disconnect: {}".format(count_since(evts(CAP, "pw"), mark4)))

# 5) stock client toggles AFK via /afk chat -> suppresses
LEG.send({"Chat": "/afk"})
mark5 = time.time() - now
suppressedByStock = pump_until([LEAD, CAP, LEG], lambda: count_since(evts(CAP, "pw"), mark5 + 0.5) == 0
                               and (time.time() - now) - mark5 > 2.0, timeout=4.0)
check(SCEN, "stock client /afk chat command suppresses the warning",
      count_since(evts(CAP, "pw"), mark5 + 0.6) == 0, "pw after /afk: {}".format(count_since(evts(CAP, "pw"), mark5 + 0.6)))
LEG.send({"Chat": "/afk"})  # toggle back off

for c in (LEAD, CAP, LEG):
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== AFK E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
