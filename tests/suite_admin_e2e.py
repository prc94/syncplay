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

# 6. unknown /osdx: private warning to the sender, never broadcast to the room
A.send({"Chat": "/osdx just chat"})
assert pump_until([A, B], lambda: chats_matching(A, "Unknown command"))
pump_for([A, B], 0.5)
check(SCEN, "unknown /osdx: warned to sender, not broadcast",
      len(chats_matching(A, "/osdx")) >= 1 and not chats_matching(B, "/osdx just chat"))

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

# 8. locked room: pause/unpause in the player is a readiness toggle, not a control attempt
D = MiniClient("dAdmin", "lockready", "1.7.6", {"chat": True}, role="leader",
               file_={"name": "m.mkv", "duration": 100, "size": 500})
E = MiniClient("eStock", "lockready", "1.7.6", {"chat": True}, role="leader",          # stock client
               file_={"name": "m.mkv", "duration": 100, "size": 500})
F = MiniClient("fModded", "lockready", "1.7.6", {"chat": True, "roomLock": True}, role="leader",
               file_={"name": "m.mkv", "duration": 100, "size": 500})
for c in (D, E, F):
    c.connect(19041)
assert pump_until([D, E, F], lambda: D.hello and E.hello and F.hello), "D/E/F hello"
t8 = time.time()
for c in (D, E, F):
    c.t0 = t8

# Everyone settles at the room position first: an unestablished watcher, or one whose file is still
# loading, is treated as player noise. Reporting a position the freshly-set file cannot explain is
# what tells the server the file is up (see Watcher._consumeFileChange).
for c in (D, E, F):
    c.desired = False
pump_for([D, E, F], 0.8)
for c in (D, E, F):
    c.position = 12.0
pump_for([D, E, F], 1.2)

D.send({"Chat": "/admin S3cret"})
assert pump_until([D, E, F], lambda: chats_matching(D, "You are now a server admin"))
D.send({"Chat": "/lock"})
assert pump_until([D, E, F], lambda: chats_matching(E, "locked this room"))
locks = [v for _, v in sets_of(F, "roomLock")]
check(SCEN, "capable client got Set:roomLock on /lock",
      any(v.get("locked") is True and v.get("room") == "lockready" for v in locks), repr(locks))
check(SCEN, "stock client got no Set:roomLock", not sets_of(E, "roomLock"))

readies_before = len([v for _, v in sets_of(F, "ready") if v.get("username") == "eStock"])
press_start = time.time() - t8
E.desired = True                      # stock client presses pause in its player
got_toggle = pump_until([D, E, F], lambda: len(
    [v for _, v in sets_of(F, "ready") if v.get("username") == "eStock"]) > readies_before, timeout=4.0)
eReadies = [v for _, v in sets_of(F, "ready") if v.get("username") == "eStock"]
check(SCEN, "locked: stock client's pause became a readiness toggle", got_toggle, repr(eReadies))
check(SCEN, "locked: readiness went to ready", eReadies and eReadies[-1].get("isReady") is True, repr(eReadies))
check(SCEN, "locked: the pause itself never became room state",
      not [s for (t, s) in states_of(D) if t >= press_start and s is True], repr(states_of(D)[-3:]))

# one keypress, one toggle: the client keeps repeating the rejected pause until the revert lands
pump_for([D, E, F], 2.0)
eReadies = [v for _, v in sets_of(F, "ready") if v.get("username") == "eStock"]
check(SCEN, "locked: a repeated rejected pause does not flap readiness",
      len(eReadies) == readies_before + 1, repr(eReadies))

# a client that tracks the lock itself is left alone by the server-side fallback
fReadies_before = len([v for _, v in sets_of(E, "ready") if v.get("username") == "fModded"])
F.desired = True
pump_for([D, E, F], 2.0)
fReadies = [v for _, v in sets_of(E, "ready") if v.get("username") == "fModded"]
check(SCEN, "locked: capable client gets no server-side readiness toggle",
      len(fReadies) == fReadies_before, repr(fReadies))

D.send({"Chat": "/unlock"})
assert pump_until([D, E, F], lambda: chats_matching(E, "unlocked"))
locks = [v for _, v in sets_of(F, "roomLock")]
check(SCEN, "capable client got Set:roomLock on /unlock",
      locks and locks[-1].get("locked") is False, repr(locks))

for c in (D, E, F):
    c.close()

for c in (A, B, C):
    c.close()
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== ADMIN E2E SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
