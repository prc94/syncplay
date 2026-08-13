"""E2E S9: admin track proposals over a live server."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S9:track-proposals"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19051, ["--admin-password", "S3cret", "--salt", "testsalt"])

def pump_until(clients, pred, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in clients:
            c.pump(); c.tick()
        if pred():
            return True
        time.sleep(0.02)
    return False
def pump_for(clients, seconds):
    pump_until(clients, lambda: False, timeout=seconds)
def sets_of(c, key):
    return [(t, s[key]) for (t, k, s) in c.events if k == "set" and key in s]

PROPOSAL = {"audioId": 2, "subId": "no", "audioName": "#2 jpn (Main)", "subName": "off",
            "signature": "audio:1:eng|audio:2:jpn|sub:1:eng"}

A = MiniClient("adm", "tr", "1.7.6", {"chat": True, "trackProposals": True}, role="leader",
               file_={"name": "ep1.mkv", "duration": 100, "size": 500})
C = MiniClient("cap", "tr", "1.7.6", {"chat": True, "trackProposals": True})
F = MiniClient("fb", "tr", "1.6.0", {"chat": True}, file_={"name": "ep1.mkv", "duration": 100, "size": 500})
for c in (A, C, F):
    c.connect(19051)
assert pump_until([A, C, F], lambda: all(x.hello for x in (A, C, F))), "hellos"
t0 = time.time()
for c in (A, C, F):
    c.t0 = t0

# non-admin publish rejected
A.send({"Set": {"trackProposal": dict(PROPOSAL)}})
assert pump_until([A, C, F], lambda: chats_matching(A, "Only server admins"))
pump_for([A, C, F], 0.4)
check(SCEN, "non-admin publish: private error, nothing broadcast",
      len(chats_matching(A, "Only server admins")) == 1 and not sets_of(C, "trackProposal") and not evts(F, "chat"))

# authenticate and publish
A.send({"Chat": "/admin S3cret"})
assert pump_until([A, C, F], lambda: chats_matching(A, "You are now a server admin"))
A.send({"Set": {"trackProposal": dict(PROPOSAL)}})
assert pump_until([A, C, F], lambda: sets_of(C, "trackProposal") and chats_matching(F, "recommends tracks"))
pump_for([A, C, F], 0.5)
got = sets_of(C, "trackProposal")
check(SCEN, "capable client got exactly one Set proposal", len(got) == 1, repr(got))
if got:
    p = got[0][1]
    check(SCEN, "payload normalized + attributed",
          p["audioId"] == 2 and p["subId"] == "no" and p["by"] == "adm" and p["signature"] == PROPOSAL["signature"], repr(p))
check(SCEN, "capable client got NO chat for it", not chats_matching(C, "recommends tracks"))
fbc = chats_matching(F, "recommends tracks")
check(SCEN, "fallback got exactly one chat", len(fbc) == 1
      and fbc[0][1] == "adm recommends tracks - audio: #2 jpn (Main), subtitles: off", repr(fbc))
check(SCEN, "publisher got private ack", len(chats_matching(A, "recommendation published")) == 1)
check(SCEN, "publisher (capable) also received the Set", len(sets_of(A, "trackProposal")) == 1)

# fallback user's own file change -> per-watcher reminder; capable stays silent
F.send({"Set": {"file": {"name": "ep2.mkv", "duration": 100, "size": 600}}})
assert pump_until([A, C, F], lambda: len(chats_matching(F, "recommends tracks")) >= 2)
pump_for([A, C, F], 0.5)
check(SCEN, "fallback file change: reminded exactly once", len(chats_matching(F, "recommends tracks")) == 2)
F.send({"Set": {"file": {"name": "ep2.mkv", "duration": 100, "size": 600}}})
pump_for([A, C, F], 0.6)
check(SCEN, "same file re-sent: no duplicate reminder", len(chats_matching(F, "recommends tracks")) == 2)
C.send({"Set": {"file": {"name": "ep2.mkv", "duration": 100, "size": 600}}})
pump_for([A, C, F], 0.6)
check(SCEN, "capable file change: no new Set, no chat",
      len(sets_of(C, "trackProposal")) == 1 and not chats_matching(C, "recommends tracks"))

# late joiners
LC = MiniClient("lateCap", "tr", "1.7.6", {"chat": True, "trackProposals": True})
LF = MiniClient("lateFb", "tr", "1.6.0", {"chat": True}, file_={"name": "ep2.mkv", "duration": 100, "size": 600})
LC.connect(19051); LF.connect(19051)
assert pump_until([A, C, F, LC, LF], lambda: LC.hello and LF.hello), "late hellos"
LC.t0 = LF.t0 = time.time()
assert pump_until([A, C, F, LC, LF], lambda: sets_of(LC, "trackProposal") and chats_matching(LF, "recommends tracks"), timeout=4.0)
check(SCEN, "late joiner (capable) got the proposal via Set", len(sets_of(LC, "trackProposal")) == 1)
check(SCEN, "late joiner (fallback) got it via chat", len(chats_matching(LF, "recommends tracks")) == 1)

# fileless fallback joiner: chat deferred until their first file loads
LN = MiniClient("lateNofile", "tr", "1.6.0", {"chat": True})
LN.connect(19051)
assert pump_until([A, C, F, LC, LF, LN], lambda: LN.hello), "LN hello"
LN.t0 = time.time()
pump_for([A, C, F, LC, LF, LN], 0.8)
check(SCEN, "fileless joiner: no stale chat at join", not chats_matching(LN, "recommends tracks"))
LN.send({"Set": {"file": {"name": "ep2.mkv", "duration": 100, "size": 600}}})
assert pump_until([A, C, F, LC, LF, LN], lambda: chats_matching(LN, "recommends tracks"), timeout=4.0)
check(SCEN, "fileless joiner: reminded when first file loads", len(chats_matching(LN, "recommends tracks")) == 1)

# /tracks from a stock/legacy client -> private notice
F.send({"Chat": "/tracks"})
assert pump_until([A, C, F, LC, LF, LN], lambda: chats_matching(F, "compatible player"))
pump_for([A, C, F, LC, LF, LN], 0.4)
check(SCEN, "/tracks from legacy: private notice only",
      len(chats_matching(F, "compatible player")) == 1 and not chats_matching(C, "compatible player"))

for c in (A, C, F, LC, LF, LN):
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== TRACKS E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
