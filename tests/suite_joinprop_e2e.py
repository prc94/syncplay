"""E2E S12: join-time propagation of room state (trusted domains + track proposals).

The bug this covers: the server used to push Set:trustedDomains / Set:trackProposal during
addWatcher - i.e. *before* its Hello - and the client wiped its session-only copy of that state
while handling the Hello. On the wire the fix is simply that the Hello now comes first.
"""
import os, sys, time, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S12:join-propagation"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19071, ["--admin-password", "S3cret", "--salt", "testsalt"])

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
def index_of_hello(c):
    for i, (t, k, p) in enumerate(c.events):
        if k == "hello":
            return i
    return -1
def index_of_set(c, key):
    for i, (t, k, p) in enumerate(c.events):
        if k == "set" and key in p:
            return i
    return -1
def index_of_chat(c, needle):
    for i, (t, k, p) in enumerate(c.events):
        if k == "chat" and needle in p[1]:
            return i
    return -1

CAPABLE = {"chat": True, "trackProposals": True, "trustedDomains": True, "featureList": True}
DOMAINS = {"domains": ["example.com", "cdn.test"], "by": "adm"}
PROPOSAL = {"audioId": 2, "subId": "no", "audioName": "#2 jpn (Main)", "subName": "off",
            "signature": "audio:1:eng|audio:2:jpn|sub:1:eng"}

# --- an admin publishes both kinds of room state ---
A = MiniClient("adm", "jp", "1.7.6", CAPABLE, role="leader",
               file_={"name": "ep1.mkv", "duration": 100, "size": 500})
A.connect(19071)
assert pump_until([A], lambda: A.hello), "admin hello"
t0 = time.time(); A.t0 = t0
A.send({"Chat": "/admin S3cret"})
assert pump_until([A], lambda: chats_matching(A, "You are now a server admin")), "admin auth"
A.send({"Set": {"trustedDomains": dict(DOMAINS)}})
A.send({"Set": {"trackProposal": dict(PROPOSAL)}})
assert pump_until([A], lambda: chats_matching(A, "Published")
                  or chats_matching(A, "trusted domain")), "publish acks"
pump_for([A], 0.6)

# --- a capable late joiner ---
LC = MiniClient("lateCap", "jp", "1.7.6", CAPABLE)
LC.connect(19071)
LC.t0 = t0
assert pump_until([A, LC], lambda: LC.hello and sets_of(LC, "trustedDomains")
                  and sets_of(LC, "trackProposal"), timeout=6.0), "late joiner state"
pump_for([A, LC], 0.6)

iHello, iDomains, iTracks = index_of_hello(LC), index_of_set(LC, "trustedDomains"), index_of_set(LC, "trackProposal")
check(SCEN, "late joiner received the trusted domains", iDomains >= 0)
check(SCEN, "late joiner received the track proposal", iTracks >= 0)
check(SCEN, "Hello arrives BEFORE the trusted domains (the regression)",
      iHello >= 0 and iHello < iDomains, "hello@{} domains@{}".format(iHello, iDomains))
check(SCEN, "Hello arrives BEFORE the track proposal",
      iHello >= 0 and iHello < iTracks, "hello@{} tracks@{}".format(iHello, iTracks))
got = sets_of(LC, "trustedDomains")
check(SCEN, "domain payload intact and attributed",
      len(got) == 1 and got[0][1]["domains"] == ["example.com", "cdn.test"] and got[0][1]["by"] == "adm",
      repr(got))
gotP = sets_of(LC, "trackProposal")
check(SCEN, "proposal payload intact and attributed",
      len(gotP) == 1 and gotP[0][1]["audioId"] == 2 and gotP[0][1]["by"] == "adm", repr(gotP))
check(SCEN, "capable joiner got no fallback chat for either",
      not chats_matching(LC, "shared trusted domains") and not chats_matching(LC, "recommends tracks"))

# --- a legacy (fallback) late joiner gets chat, also after its Hello ---
LF = MiniClient("lateFb", "jp", "1.6.0", {"chat": True},
                file_={"name": "ep1.mkv", "duration": 100, "size": 500})
LF.connect(19071)
LF.t0 = t0
assert pump_until([A, LC, LF], lambda: LF.hello and chats_matching(LF, "shared trusted domains"),
                  timeout=6.0), "fallback joiner chat"
pump_for([A, LC, LF], 0.6)
iHelloF, iChatF = index_of_hello(LF), index_of_chat(LF, "shared trusted domains")
check(SCEN, "fallback joiner got the domains as chat", iChatF >= 0)
check(SCEN, "fallback joiner's Hello precedes that chat too",
      iHelloF >= 0 and iHelloF < iChatF, "hello@{} chat@{}".format(iHelloF, iChatF))
check(SCEN, "fallback joiner got the track recommendation as chat",
      len(chats_matching(LF, "recommends tracks")) == 1, repr(chats_matching(LF, "recommends tracks")))
check(SCEN, "fallback joiner got no Set for either",
      not sets_of(LF, "trustedDomains") and not sets_of(LF, "trackProposal"))

# --- a room switch still delivers the state immediately (no Hello involved) ---
SW = MiniClient("switcher", "other", "1.7.6", CAPABLE)
SW.connect(19071)
SW.t0 = t0
assert pump_until([A, SW], lambda: SW.hello), "switcher hello"
pump_for([A, SW], 0.4)
check(SCEN, "watcher in another room has no room state yet",
      not sets_of(SW, "trustedDomains") and not sets_of(SW, "trackProposal"))
SW.send({"Set": {"room": {"name": "jp"}}})
assert pump_until([A, SW], lambda: sets_of(SW, "trustedDomains") and sets_of(SW, "trackProposal"),
                  timeout=6.0), "state after room switch"
check(SCEN, "room switch delivers domains + proposal", len(sets_of(SW, "trustedDomains")) == 1
      and len(sets_of(SW, "trackProposal")) == 1)

# --- an ORDINARY room is discarded once empty: layouts persist, its domains do not ---
for c in (A, LC, LF, SW):
    c.close()
time.sleep(1.2)  # let the server observe every disconnect and run its empty-room cleanup

FRESH = MiniClient("fresh", "jp", "1.7.6", CAPABLE)
FRESH.connect(19071)
FRESH.t0 = time.time()
assert pump_until([FRESH], lambda: FRESH.hello), "fresh hello"
pump_for([FRESH], 1.2)
check(SCEN, "next session's joiner still gets the remembered layout",
      len(sets_of(FRESH, "trackProposal")) == 1, repr(sets_of(FRESH, "trackProposal")))
check(SCEN, "remembered layout still arrives after the Hello",
      index_of_hello(FRESH) < index_of_set(FRESH, "trackProposal"),
      "hello@{} tracks@{}".format(index_of_hello(FRESH), index_of_set(FRESH, "trackProposal")))
check(SCEN, "an ordinary room's domains go with it when it is torn down",
      not sets_of(FRESH, "trustedDomains"), repr(sets_of(FRESH, "trustedDomains")))
FRESH.close()

srv.clean_log(SCEN)
srv.stop()

# --- a PERMANENT room is never torn down, so its domains do survive being empty ---
tmp = tempfile.mkdtemp(prefix="syncplay-joinprop-")
permFile = os.path.join(tmp, "permanent.txt")
with open(permFile, "w") as fh:
    fh.write("perm\n")
srvP = ServerBoot(19073, ["--admin-password", "S3cret", "--salt", "testsalt",
                          "--rooms-db-file", os.path.join(tmp, "rooms.db"),
                          "--permanent-rooms-file", permFile])
PA = MiniClient("permAdm", "perm", "1.7.6", CAPABLE, role="leader",
                file_={"name": "ep1.mkv", "duration": 100, "size": 500})
PA.connect(19073)
assert pump_until([PA], lambda: PA.hello), "perm admin hello"
PA.t0 = time.time()
PA.send({"Chat": "/admin S3cret"})
assert pump_until([PA], lambda: chats_matching(PA, "You are now a server admin")), "perm admin auth"
PA.send({"Set": {"trustedDomains": dict(DOMAINS)}})
assert pump_until([PA], lambda: chats_matching(PA, "trusted domain")), "perm publish ack"
pump_for([PA], 0.6)
PA.close()
time.sleep(1.2)  # room is now empty - but permanent, so it is not discarded

PJ = MiniClient("permJoiner", "perm", "1.7.6", CAPABLE)
PJ.connect(19073)
PJ.t0 = time.time()
assert pump_until([PJ], lambda: PJ.hello), "perm joiner hello"
pump_for([PJ], 1.2)
gotP = sets_of(PJ, "trustedDomains")
check(SCEN, "a permanent room keeps its domains across an empty session", len(gotP) == 1, repr(gotP))
check(SCEN, "those domains keep their payload and attribution",
      gotP and gotP[0][1]["domains"] == ["example.com", "cdn.test"] and gotP[0][1]["by"] == "permAdm",
      repr(gotP))
check(SCEN, "and still arrive after the Hello",
      index_of_hello(PJ) < index_of_set(PJ, "trustedDomains"),
      "hello@{} domains@{}".format(index_of_hello(PJ), index_of_set(PJ, "trustedDomains")))
PJ.close()
srvP.clean_log(SCEN)
srvP.stop()
shutil.rmtree(tmp, ignore_errors=True)

# --- ...but nothing outlives the server process ---
srv2 = ServerBoot(19072, ["--admin-password", "S3cret", "--salt", "testsalt"])
AFTER = MiniClient("afterRestart", "jp", "1.7.6", CAPABLE)
AFTER.connect(19072)
AFTER.t0 = time.time()
assert pump_until([AFTER], lambda: AFTER.hello), "post-restart hello"
pump_for([AFTER], 1.2)
check(SCEN, "a server restart clears the remembered domains", not sets_of(AFTER, "trustedDomains"),
      repr(sets_of(AFTER, "trustedDomains")))
check(SCEN, "a server restart clears the remembered layouts", not sets_of(AFTER, "trackProposal"),
      repr(sets_of(AFTER, "trackProposal")))
AFTER.close()
srv2.clean_log(SCEN)
srv2.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== JOINPROP E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
