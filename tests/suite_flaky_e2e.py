"""E2E S13: a real client on a bad link (mobile data, ~1 Mbps, ~800 ms RTT).

Every other suite runs on a perfect wire. This one puts a real `SyncplayClient` + real
`SyncClientProtocol` behind a latency proxy in front of a real `syncplayServer.py`, presses keys on
its player, and asks the only question a user asks: **did that do anything?**

Named link profiles (`flaky_harness.PROFILES`) run the same script over LAN, HSPA and EDGE-grade
links, plus two blackout profiles either side of `PROTOCOL_TIMEOUT` (12.5 s) - the threshold that
decides whether a mobile hiccup is survivable or a disconnect.

What is asserted hard (all true today, so a regression here is a real one):
  - an action taken with room to breathe (3x RTT) always reaches the room, on every profile;
  - the client is never left wedged or mute - `clientIgnoringOnTheFly` comes back to 0 and
    playstates start flowing again within seconds of the user stopping, even after a burst;
  - an outage shorter than PROTOCOL_TIMEOUT costs nothing: no reconnect, and the next action lands;
  - an outage longer than it recovers by itself, and the client works again afterwards;
  - the `pauseOnBuffer` opt-out really opts out: a starved player then disturbs nobody.

Four defects found on a real ~1 Mbps mobile link were first recorded here as `[KNOWN]`
measurements and are now fixed; the checks that replaced them are the regression guard, and each
was verified to fail against the old behaviour before being promoted:

  1. a keypress made inside one RTT of the previous one was dropped rather than queued, and the
     player was then reverted (now: presses inside a round trip are coalesced, the room and the
     player always end up agreeing, and the next deliberate press still works);
  2. a stalled player's one-RTT-stale playstate echo was read by the server as a fresh user action,
     so the room ping-ponged pause/unpause once per RTT - attributed to the stalled user - and kept
     doing it long after the buffer hold that started it was gone;
  3. a seek made while that was going on was overwritten by the hold's position broadcast;
  4. the client-side fallback (any server without --buffer-pause, upstream included) started the
     same ping-pong, so turning the server feature off was no way out of it.

`expect_defect()` in flaky_harness.py stays available for the next one: it reports without failing
while a defect stands, and shouts once it stops reproducing.

Runtime ~2.5 min - a bad link has to be waited out in real time. `--quick` (~1 min) keeps the
profile sweep, the short blackout and the buffering opt-out, and skips the two long scenarios: the
over-timeout outage and the two starved-player defect runs.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
from e2e_harness import check, RESULTS
from flaky_harness import DEFECTS, PROFILES, Session, defect_summary, info
from syncplay import constants

RESULTS.clear()
SCEN = "S13:flaky"
QUICK = "--quick" in sys.argv
print("--- {} ---".format(SCEN))


def spaced_actions(session, kinds, gap):
    """Do each action `gap` seconds apart, so every one has a clear round trip to itself."""
    for kind in kinds:
        session.act(kind, delta=-60.0)
        session.run(gap)


# ---------------------------------------------------------------- 1) delivery, profile by profile
# The baseline promise: if you are not fighting yourself, what you press happens. Run identically
# over three links so a failure points at the link quality rather than at the scenario.
for index, name in enumerate(("lan", "hspa", "edge")):
    profile = PROFILES[name]
    gap = max(1.5, profile.delay * 6)  # 3x RTT, floored so the LAN run still clears the server tick
    session = Session(19091 + index, 19191 + index, profile)
    session.settle()
    started = session.client.rel()
    spaced_actions(session, ["pause", "pause", "seek"], gap)
    session.run(3.0)

    results = session.action_results(within=gap + 3.0, since=started)
    lost = [r for r in results if r[3] is None]
    check(SCEN, "{}: every well-spaced action reaches the room".format(name), not lost,
          "lost={} of {} (rtt {:.1f}s, gap {:.1f}s)".format(len(lost), len(results), profile.rtt, gap))
    slowest = max((r[3] for r in results if r[3] is not None), default=None)
    info(SCEN, "{}: slowest action round trip".format(name),
         "{:.2f}s".format(slowest) if slowest else "n/a")

    # The invariant that matters most: whatever happened, the client is talking again.
    last = session.actions[-1][0]
    check(SCEN, "{}: client is not left wedged (counter back to 0, playstates flowing)".format(name),
          session.client.counter_zero_since(last) is not None,
          "counters={} sent_tail={}".format(session.client.counters(), session.client.sent[-3:]))
    check(SCEN, "{}: no reconnect on a link that never went down".format(name),
          session.client.reconnects == 0, "reconnects={}".format(session.client.reconnects))

    # --------- hammering the key: a burst must leave the client working
    # Only one change can be outstanding at a time, so presses inside one round trip are coalesced.
    # Two things must hold afterwards regardless: the room and the player must agree - the user must
    # never be left looking at a player in one state while the room believes another - and the next
    # deliberate press must still work. Before the pending-change fix the second press of any burst
    # was dropped outright and the player was then reverted by the server, which is what made people
    # press again, and lose more.
    if name == "edge":
        burst_start = session.client.rel()
        for _ in range(6):
            session.act("pause")
            session.run(0.4)          # well inside the 0.8 s round trip
        burst_end = session.client.rel()
        session.run(5.0)
        room = session.client.globals[-1][2]
        check(SCEN, "edge: after a burst the room and the player agree",
              room == session.client.player.paused,
              "room={} player={}".format(room, session.client.player.paused))
        swallowed, total = session.client.stripped_between(burst_start, burst_end)
        info(SCEN, "edge: presses coalesced during the burst",
             "{} of {} States sent without a playstate; {} flips reached the room".format(
                 swallowed, total, session.client.room_flips(since=burst_start)))
        # ...and the client must be in working order once the user stops.
        check(SCEN, "edge: a burst does not wedge the client permanently",
              session.client.counter_zero_since(burst_end) is not None,
              "counters={} sent_tail={}".format(session.client.counters(), session.client.sent[-4:]))
        after_burst = session.client.rel()
        session.act("pause")
        session.run(gap + 2.0)
        post = session.action_results(within=gap + 3.0, since=after_burst)
        check(SCEN, "edge: a deliberate press right after a burst still lands",
              post and not [r for r in post if r[3] is None], repr(post))

    session.close(SCEN)


# ---------------------------------------------------------------- 2) an outage inside the timeout
# 8 s of nothing on a mobile link is ordinary. It must cost nothing at all: PROTOCOL_TIMEOUT is
# 12.5 s and the link holds data rather than losing it, exactly as TCP does.
session = Session(19094, 19194, PROFILES["edge_blackout"])
session.settle()
while session.rel() < PROFILES["edge_blackout"].outage_at + PROFILES["edge_blackout"].outage_for + 2.0:
    session.pump()
    time.sleep(0.01)
check(SCEN, "blackout under PROTOCOL_TIMEOUT: nobody disconnects",
      session.client.reconnects == 0, "reconnects={}".format(session.client.reconnects))
check(SCEN, "blackout under PROTOCOL_TIMEOUT: no timeout error shown to the user",
      not [e for e in session.client.ui.errors if "timeout" in e.lower()],
      repr(session.client.ui.errors[:2]))
after_blackout = session.client.rel()
spaced_actions(session, ["pause", "seek"], 3.0)
session.run(3.0)
post = session.action_results(within=6.0, since=after_blackout)
check(SCEN, "blackout under PROTOCOL_TIMEOUT: actions work again immediately after",
      post and not [r for r in post if r[3] is None], repr(post))
session.close(SCEN)


# ---------------------------------------------------------------- 3) an outage past the timeout
# 15 s is past what either end will wait. Both are entitled to drop the connection; what must not
# happen is the session staying dead once the link comes back.
if not QUICK:
    profile = PROFILES["edge_outage"]
    session = Session(19095, 19195, profile)
    session.settle()
    joined_as = session.client.name()
    end_of_outage = profile.outage_at + profile.outage_for
    while session.rel() < end_of_outage + 8.0:
        session.pump()
        time.sleep(0.01)
    recovered = session.client.rel()
    check(SCEN, "outage past PROTOCOL_TIMEOUT: the client notices and redials",
          session.client.reconnects >= 1, "reconnects={}".format(session.client.reconnects))
    check(SCEN, "outage past PROTOCOL_TIMEOUT: it does not thrash reconnecting",
          session.client.reconnects <= 3, "reconnects={}".format(session.client.reconnects))
    check(SCEN, "outage past PROTOCOL_TIMEOUT: room state flows again",
          [g for g in session.client.globals if g[0] > end_of_outage],
          "{} States after the link returned".format(
              len([g for g in session.client.globals if g[0] > end_of_outage])))
    spaced_actions(session, ["pause", "seek"], 3.0)
    session.run(3.0)
    post = session.action_results(within=6.0, since=recovered)
    check(SCEN, "outage past PROTOCOL_TIMEOUT: actions work again after recovery",
          post and not [r for r in post if r[3] is None], repr(post))
    if session.client.name() != joined_as:
        # Not a failure - findFreeUsername is doing its job - but it is why a reconnecting user
        # sees themselves renamed, and it drops any per-connection state (admin auth included).
        info(SCEN, "reconnect renamed the user (old watcher not reaped yet)",
             "{} -> {}".format(joined_as, session.client.name()))
    session.close(SCEN)


# ---------------------------------------------------------------- 4) a player that cannot keep up
# 1 Mbps and a stream that will not fit down it: the player freezes while still calling itself
# unpaused. Three arrangements of the same stall: the feature off (the quiet baseline), the server
# hold, and - for servers that do not have the feature - the client's own fallback.
STALL_SECONDS = 12.0


def starved_run(port, proxy_port, server_args=(), client_config=None):
    session = Session(port, proxy_port, PROFILES["edge"], server_args=server_args,
                      client_config=client_config)
    session.settle()
    started = session.client.rel()
    session.client.starve()
    session.run(STALL_SECONDS)
    return session, started


# --------- 4a: the opt-out. pauseOnBuffer off means we neither report nor act on our own stall.
session, _started = starved_run(19096, 19196, client_config={"pauseOnBuffer": False})
quiet_flips = session.client.room_flips()
quiet_forced = session.client.forced_in
check(SCEN, "pauseOnBuffer off: a starved player is not announced to the room",
      not session.peer_chats("buffering"), repr(session.peer_chats("buffering")[:2]))
check(SCEN, "pauseOnBuffer off: the room is not flapped in and out of pause",
      quiet_flips <= 2, "flips={}".format(quiet_flips))
info(SCEN, "pauseOnBuffer off: forced updates received while starved", str(quiet_forced))
session.close(SCEN)

if not QUICK:
    # --------- 4b: the server hold. The room may pause and resume while it waits for a cache, but
    # it must not ping-pong: the stale-echo guard is what stops the hold's own pause coming back an
    # RTT later as "the user pressed play", which used to flip the room once per RTT indefinitely -
    # long after the hold itself was gone (the chat stopped; the flipping did not).
    session, started = starved_run(19097, 19197)
    flips = session.client.room_flips(since=started)
    interval = session.client.mean_flip_interval(since=started)
    chats = session.peer_chats("the room is paused") + session.peer_chats("resuming")
    rtt = PROFILES["edge"].rtt
    check(SCEN, "starved player: the room does not ping-pong at one flip per RTT",
          flips <= 5 and (interval is None or interval > rtt * 2),
          "{} flips in {:.0f}s, mean interval {} (RTT {:.1f}s), {} hold announcement(s)".format(
              flips, STALL_SECONDS, "{:.2f}s".format(interval) if interval else "n/a",
              rtt, len(chats)))
    info(SCEN, "server hold: forced updates received while starved",
         "{} (baseline {})".format(session.client.forced_in, quiet_forced))
    info(SCEN, "server hold: hold/release cycles while the cache never fills",
         "{} announcement(s) in {:.0f}s - the heuristic path retries every BUFFER_HOLD_SETTLE ({}s) "
         "+ BUFFER_RECOVER_HOLD; only a player that tracks its own cache (mpv) can hold long enough "
         "to reach BUFFER_HOLD_MAX ({}s)".format(len(chats), STALL_SECONDS,
                                                 constants.BUFFER_HOLD_SETTLE,
                                                 constants.BUFFER_HOLD_MAX))

    # --------- a seek made during a hold must survive it. It used to be undone within a second by
    # the hold's next broadcast re-asserting room.getPosition() at everyone.
    at, _kind, want = session.act("seek", delta=-60.0)
    session.run(3.0)
    dragged = [g for g in session.client.globals if at < g[0] <= at + 2.5 and g[1] > want + 20]
    check(SCEN, "a seek made during a buffer hold is not overwritten", not dragged,
          "seeked to {:.0f}, room came back with {}".format(
              want, [round(g[1]) for g in dragged][:3]))
    session.close(SCEN)

    # --------- 4c: no server-side feature, so the client pauses the room itself
    # (_bufferHoldFallback) - which is also what happens against a stock upstream server. That
    # pause used to start the same ping-pong, so turning the server feature off was no way out of
    # it; the guard is server-side and unconditional, so this path is covered too.
    session, started = starved_run(19098, 19198, server_args=["--no-buffer-pause"])
    fallback_flips = session.client.room_flips(since=started)
    fallback_chats = session.peer_chats("My player is buffering")
    check(SCEN, "the client-side fallback does not ping-pong either", fallback_flips <= 5,
          "{} flips and {} 'my player is buffering' chat line(s) against a server without the "
          "feature (baseline: {} flips)".format(fallback_flips, len(fallback_chats), quiet_flips))
    session.close(SCEN)


fails = [x for x in RESULTS if not x[2]]
fixed = defect_summary()
reproducing = [d for d in DEFECTS if d[2]]
summary = "\n===== FLAKY E2E SUMMARY: {} checks, {} failed".format(len(RESULTS), len(fails))
if DEFECTS:
    summary += "; {} known defects reproducing, {} not".format(len(reproducing), len(fixed))
print(summary + " =====")
for s, n, ok, d in fails:
    print("  FAILED: [{}] {} {}".format(s, n, d))
sys.exit(1 if fails else 0)
