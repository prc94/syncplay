"""E2E round 2: pause/unpause flapping stress + cross-room isolation."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_harness import MiniClient, ServerBoot, run_clients, evts, chats_matching, check, RESULTS
RESULTS.clear()

# ---------- S5: flapping stress ----------
SCEN = "S5:flapping-stress"
print("--- {} ---".format(SCEN))
srv = ServerBoot(19011, ["--yap-timer", "--pause-warning-after", "2", "--pause-warning-message", "PW {}!",
                         "--salt", "testsalt"])
# 8 rapid pause/unpause cycles of 0.5s pause + 0.4s play, all under the 2s threshold
sched = [(0.0, "unpause")]
t = 1.0
for i in range(8):
    sched.append((t, "pause")); sched.append((t + 0.5, "unpause")); t += 0.9
flapper = MiniClient("flap", "fr", "1.7.6", {"chat": True}, role="leader", schedule=sched)
watcher = MiniClient("watch", "fr", "1.7.6", {"chat": True, "yapTimer": True, "pauseWarning": True})
run_clients([flapper, watcher], 19011, t + 2.5)

paused_n = len(chats_matching(flapper, "yap timer running"))
unpaused = chats_matching(flapper, "yapped for")
pw_n = len(chats_matching(flapper, "PW ")) + len(evts(watcher, "pw"))
check(SCEN, "8 pause + 8 unpause chat lines (no dupes/losses)", paused_n == 8 and len(unpaused) == 8,
      "paused={} unpaused={}".format(paused_n, len(unpaused)))
check(SCEN, "ZERO pause warnings (all pauses < threshold)", pw_n == 0, "got {}".format(pw_n))
if unpaused:
    last = unpaused[-1][1]
    check(SCEN, "total accumulated ~4s over 8x0.5s", "00:04" in last or "00:05" in last or "00:03" in last, last)
yap_frames = evts(watcher, "yap")
check(SCEN, "watcher yap frames flowed throughout", len(yap_frames) >= int(t), "{} frames".format(len(yap_frames)))
srv.clean_log(SCEN)

# ---------- S6: cross-room isolation (same boot) ----------
SCEN = "S6:cross-room-isolation"
print("--- {} ---".format(SCEN))
la = MiniClient("la", "roomA", "1.7.6", {"chat": True}, role="leader",
                schedule=[(0.0, "unpause"), (0.8, "pause"), (3.6, "unpause")])
lb = MiniClient("lb", "roomB", "1.7.6", {"chat": True}, role="leader",
                schedule=[(0.0, "unpause")])   # roomB just plays
run_clients([la, lb], 19011, 5.0)
check(SCEN, "roomA got its yap+PW chat",
      len(chats_matching(la, "yap timer running")) == 1 and len(chats_matching(la, "PW ")) >= 1,
      str([m for _, m in evts(la, "chat")]))
check(SCEN, "roomB got ZERO chat (no cross-room leak)", not evts(lb, "chat"),
      str([m for _, m in evts(lb, "chat")]))
check(SCEN, "roomB got ZERO state extras", not evts(lb, "yap") and not evts(lb, "pw"))
srv.clean_log(SCEN)
srv.stop()

fails = [x for x in RESULTS if not x[2]]
print("\n===== E2E-2 SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
