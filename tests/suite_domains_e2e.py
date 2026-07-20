"""E2E S10: admin-published trusted domains over a live server."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S10:trusted-domains"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19061, ["--admin-password", "S3cret", "--salt", "testsalt"])

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

A = MiniClient("adm", "d", "1.7.6", {"chat": True, "trustedDomains": True}, role="leader")
C = MiniClient("cap", "d", "1.7.6", {"chat": True, "trustedDomains": True})
L = MiniClient("leg", "d", "1.6.0", {"chat": True})
for c in (A, C, L):
    c.connect(19061)
assert pump_until([A, C, L], lambda: all(x.hello for x in (A, C, L))), "hellos"
now = time.time()
for c in (A, C, L):
    c.t0 = now

PAYLOAD = {"domains": ["vimeo.com", "cdn.example"]}

# non-admin publish rejected
A.send({"Set": {"trustedDomains": dict(PAYLOAD)}})
assert pump_until([A, C, L], lambda: chats_matching(A, "Only server admins"))
pump_for([A, C, L], 0.4)
check(SCEN, "non-admin publish: private error, nothing broadcast",
      len(chats_matching(A, "Only server admins")) == 1 and not sets_of(C, "trustedDomains") and not evts(L, "chat"))

# authenticate + publish
A.send({"Chat": "/admin S3cret"})
assert pump_until([A, C, L], lambda: chats_matching(A, "You are now a server admin"))
A.send({"Set": {"trustedDomains": dict(PAYLOAD)}})
assert pump_until([A, C, L], lambda: sets_of(C, "trustedDomains") and chats_matching(L, "shared trusted domains"))
pump_for([A, C, L], 0.5)
got = sets_of(C, "trustedDomains")
check(SCEN, "capable client got exactly one Set", len(got) == 1 and got[0][1]["domains"] == ["vimeo.com", "cdn.example"]
      and got[0][1]["by"] == "adm", repr(got))
check(SCEN, "capable client got NO chat", not chats_matching(C, "shared trusted domains"))
legc = chats_matching(L, "shared trusted domains")
check(SCEN, "legacy got exactly one informational chat", len(legc) == 1 and "vimeo.com" in legc[0][1] and "cdn.example" in legc[0][1], repr(legc))
check(SCEN, "publisher got count ack", len(chats_matching(A, "Published 2 trusted")) == 1)

# late joiners (capable + legacy)
LC = MiniClient("lateCap", "d", "1.7.6", {"chat": True, "trustedDomains": True})
LL = MiniClient("lateLeg", "d", "1.6.0", {"chat": True})
LC.connect(19061); LL.connect(19061)
assert pump_until([A, C, L, LC, LL], lambda: LC.hello and LL.hello), "late hellos"
LC.t0 = LL.t0 = time.time()
assert pump_until([A, C, L, LC, LL], lambda: sets_of(LC, "trustedDomains") and chats_matching(LL, "shared trusted domains"), timeout=4.0)
check(SCEN, "late joiner capable got the Set", len(sets_of(LC, "trustedDomains")) == 1)
check(SCEN, "late joiner legacy got the chat", len(chats_matching(LL, "shared trusted domains")) == 1)

# /domains from a legacy client -> private notice
L.send({"Chat": "/domains"})
assert pump_until([A, C, L, LC, LL], lambda: chats_matching(L, "updated client"))
pump_for([A, C, L, LC, LL], 0.4)
check(SCEN, "/domains from legacy: private notice only",
      len(chats_matching(L, "updated client")) == 1 and not chats_matching(C, "updated client"))

for c in (A, C, L, LC, LL):
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== DOMAINS E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
