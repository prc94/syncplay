"""E2E: server admins - /admin auth, /lock revert enforcement, /unlock, /osd anywhere, Set:adminAuth."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_harness import MiniClient, ServerBoot, check, RESULTS, evts, chats_matching
RESULTS.clear()

SCEN = "S8:server-admins"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19041, ["--admin-password", "S3cret", "--salt", "testsalt"])

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

def states_of(c):
    return [(t, s) for (t, k, s) in c.events if k == "state"]

def sets_of(c, key):
    return [(t, s[key]) for (t, k, s) in c.events if k == "set" and key in s]

A = MiniClient("admA", "adm", "1.7.6", {"chat": True}, role="leader")
B = MiniClient("userB", "adm", "1.7.6", {"chat": True}, role="leader")
for c in (A, B):
    c.connect(19041)
assert pump_until([A, B], lambda: A.hello and B.hello), "hellos"
t0 = time.time(); A.t0 = t0; B.t0 = t0

# room starts paused; A (not yet admin, plain room) unpauses - allowed
A.desired = False
assert pump_until([A, B], lambda: any(s is False for _, s in states_of(B))), "initial unpause"

# 1. wrong password -> private fail only
A.send({"Chat": "/admin WRONG"})
assert pump_until([A, B], lambda: chats_matching(A, "Wrong admin password"))
pump_for([A, B], 0.5)
check(SCEN, "wrong /admin: private fail to A only",
      len(chats_matching(A, "Wrong admin password")) == 1 and not evts(B, "chat"))

# 2. correct password -> success + controller-status broadcast
A.send({"Chat": "/admin S3cret"})
assert pump_until([A, B], lambda: chats_matching(A, "You are now a server admin"))
pump_for([A, B], 0.5)
check(SCEN, "correct /admin: private success", len(chats_matching(A, "You are now a server admin")) == 1)
check(SCEN, "password never appeared in B's chat", not chats_matching(B, "S3cret"))
auths = [v for _, v in sets_of(B, "controllerAuth")]
check(SCEN, "B observed operator-status broadcast for admA",
      any(v.get("user") == "admA" and v.get("success") for v in auths), repr(auths))

# 3. /lock -> room-wide notice; B's pause attempts are reverted
A.send({"Chat": "/lock"})
assert pump_until([A, B], lambda: chats_matching(B, "locked this room"))
check(SCEN, "/lock notice reached the room", len(chats_matching(B, "locked this room")) == 1)

fight_start = time.time() - t0
B.desired = True                     # locked room: server must keep reverting this
pump_for([A, B], 2.2)
B.desired = False
pump_for([A, B], 0.6)
fight_end = time.time() - t0
paused_seen_by_A = [s for (t, s) in states_of(A) if fight_start <= t <= fight_end and s is True]
check(SCEN, "locked: B's pause attempts never became room state", not paused_seen_by_A,
      "A saw {} paused-frames during the fight window".format(len(paused_seen_by_A)))

# admin pause DOES work
A.desired = True
assert pump_until([A, B], lambda: any(s is True for _, s in states_of(B)[-4:])), "admin pause propagates"
check(SCEN, "locked: admin pause becomes room state", True)
A.desired = False
pump_for([A, B], 0.8)

# 4. /unlock -> B can pause again
A.send({"Chat": "/unlock"})
assert pump_until([A, B], lambda: chats_matching(B, "unlocked"))
check(SCEN, "/unlock notice reached the room", True)
B.desired = True
got_pause = pump_until([A, B], lambda: any(s is True for _, s in states_of(A)[-4:]), timeout=4.0)
check(SCEN, "unlocked: B's pause works again", got_pause)
B.desired = False
pump_for([A, B], 0.5)

# 5. admin /osd in a plain room -> B gets fallback chat
A.send({"Chat": "/osd dur=2 Admin says hi"})
assert pump_until([A, B], lambda: chats_matching(B, "Admin says hi"))
check(SCEN, "admin /osd works in plain room (B got fallback chat)", True)

# 6. token matching: /osdx is ordinary chat
A.send({"Chat": "/osdx just chat"})
assert pump_until([A, B], lambda: chats_matching(B, "/osdx just chat"))
check(SCEN, "/osdx falls through as ordinary chat", True)

# 7. modded-client auto-auth via Set:adminAuth in a separate room
C = MiniClient("modC", "c2", "1.7.6", {"chat": True}, role="leader")
C.connect(19041)
assert pump_until([A, B, C], lambda: C.hello), "C hello"
C.t0 = time.time()
C.send({"Set": {"adminAuth": {"password": "S3cret"}}})
assert pump_until([A, B, C], lambda: chats_matching(C, "You are now a server admin"))
check(SCEN, "Set:adminAuth grants admin (auto-auth path)", True)
C.send({"Chat": "/lock"})
assert pump_until([A, B, C], lambda: chats_matching(C, "locked this room"))
check(SCEN, "Set-authed admin can /lock own room", True)
lock_notices_B = [m for _, m in chats_matching(B, "locked this room - only server admins")]
check(SCEN, "no cross-room leak of C's activity",
      len(lock_notices_B) == 1 and lock_notices_B[0].startswith("admA")
      and not any(m.startswith("modC") for _, m in evts(B, "chat") if isinstance(m, str)),
      repr(lock_notices_B))

for c in (A, B, C):
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== ADMIN E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
