"""E2E: /osd command through a live server with a real managed-room controller flow."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S7:osd-channel"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19031, ["--salt", "testsalt"])

def pump_until(clients, pred, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in clients:
            c.pump(); c.tick()
        if pred():
            return True
        time.sleep(0.02)
    return False

def sets_of(client, key):
    return [(t, s[key]) for (t, s) in evts(client, "set") if key in s]

# 1. boss connects and creates/auths a managed room
boss = MiniClient("boss", "lobby", "1.7.6", {"chat": True})
boss.connect(19031)
assert pump_until([boss], lambda: boss.hello), "boss hello"
boss.t0 = time.time()
boss.send({"Set": {"controllerAuth": {"room": "osdtest", "password": "AB-123-456"}}})
assert pump_until([boss], lambda: sets_of(boss, "newControlledRoom")), "newControlledRoom reply"
roomName = sets_of(boss, "newControlledRoom")[0][1]["roomName"]
print("    managed room:", roomName)
check(SCEN, "controlled room name minted", roomName.startswith("+osdtest:"))

boss.send({"Set": {"room": {"name": roomName}}})
boss.send({"Set": {"controllerAuth": {"room": roomName, "password": "AB-123-456"}}})
assert pump_until([boss], lambda: any(v.get("success") for _, v in sets_of(boss, "controllerAuth"))), "controller auth"
check(SCEN, "boss authenticated as controller", True)

# 2. the audience joins: capable + fallback + non-controller
cap = MiniClient("cap", roomName, "1.7.6", {"chat": True, "osdMessages": True})
fb = MiniClient("fb", roomName, "1.6.0", {"chat": True})
pleb = MiniClient("pleb", roomName, "1.7.6", {"chat": True})
for c in (cap, fb, pleb):
    c.connect(19031)
assert pump_until([boss, cap, fb, pleb], lambda: all(c.hello for c in (cap, fb, pleb))), "audience hello"
now = time.time()
for c in (cap, fb, pleb):
    c.t0 = now

ALL = [boss, cap, fb, pleb]

# 3. controller sends a rich /osd
boss.send({"Chat": "/osd ass=1 dur=3 colour=#FF0000 pos=top {\\b1}Restart!{\\b0} in 5"})
assert pump_until(ALL, lambda: sets_of(cap, "osdMessage") or chats_matching(fb, "Restart")), "osd delivery"
pump_until(ALL, lambda: False, timeout=1.0)  # settle

osd = sets_of(cap, "osdMessage")
check(SCEN, "capable client got exactly one Set.osdMessage", len(osd) == 1, repr(osd))
if osd:
    p = osd[0][1]
    check(SCEN, "raw ASS text intact over the wire", p["text"] == "{\\b1}Restart!{\\b0} in 5", repr(p["text"]))
    check(SCEN, "payload normalized (ass/colour/pos/dur)",
          p["ass"] is True and p["colour"] == "#FF0000" and p["position"] == "top" and p["duration"] == 3.0, repr(p))
check(SCEN, "capable client got NO chat for it", not chats_matching(cap, "Restart"))
fbc = chats_matching(fb, "Restart")
check(SCEN, "fallback got exactly one tag-stripped chat", len(fbc) == 1 and fbc[0][1] == "Restart! in 5", repr(fbc))
check(SCEN, "controller (non-capable) also saw the fallback chat", len(chats_matching(boss, "Restart")) == 1)
check(SCEN, "pleb (non-capable) saw the fallback chat too", len(chats_matching(pleb, "Restart")) == 1)

# 4. non-controller tries /osd -> private error only
pleb.send({"Chat": "/osd hax attempt"})
assert pump_until(ALL, lambda: chats_matching(pleb, "operators")), "pleb error"
pump_until(ALL, lambda: False, timeout=0.6)
check(SCEN, "non-controller got private error", len(chats_matching(pleb, "operators")) == 1)
check(SCEN, "error not broadcast (no 'hax' anywhere)",
      not any(chats_matching(c, "hax") for c in ALL))

# 5. long command survives the 150-char default chat truncation
long_text = "A" * 220
boss.send({"Chat": "/osd " + long_text})
assert pump_until(ALL, lambda: chats_matching(fb, "AAAA")), "long osd"
got = chats_matching(fb, "AAAA")[0][1]
check(SCEN, "220-char message intact end-to-end (pre-truncation interception)", got == long_text, "len=%d" % len(got))
cap_osd2 = [v for _, v in sets_of(cap, "osdMessage") if v["text"].startswith("A")]
check(SCEN, "capable payload full length too", cap_osd2 and len(cap_osd2[0]["text"]) == 220)

# 6. ordinary chat still works for everyone
boss.send({"Chat": "hello room"})
assert pump_until(ALL, lambda: chats_matching(cap, "hello room")), "plain chat"
check(SCEN, "ordinary chat reaches capable client as chat", len(chats_matching(cap, "hello room")) == 1)
check(SCEN, "ordinary chat reaches fallback", len(chats_matching(fb, "hello room")) == 1)

for c in ALL:
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== OSD E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
