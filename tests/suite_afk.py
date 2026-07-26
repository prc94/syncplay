"""Unit suite for the AFK user state (suppresses pause warning, forces not-ready)."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import time, types
from syncplay import constants
from syncplay.server import Room, ControlledRoom, SyncFactory, Watcher
from syncplay.protocols import SyncServerProtocol, SyncClientProtocol
import syncplay.messages as M
M.setLanguage("en")
from syncplay.messages import getMessage

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] AFK :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


class FW:
    """Watcher stand-in tracking AFK/ready broadcasts and chat/Set traffic."""
    def __init__(self, name, features=None, ready=None, admin=False):
        self._name = name
        self._features = features or {}
        self._ready = ready
        self._afk = False
        self._admin = admin
        self._room = None
        self.chats = []          # chat message bodies received
        self.afkSets = []        # (username, isAfk) from sendSetAfk
        self.readyBroadcasts = []  # (username, isReady, manuallyInitiated)
    def getName(self): return self._name
    def getRoom(self): return self._room
    def isAdmin(self): return self._admin
    def isController(self): return self._admin
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def getFile(self): return {"name": "f.mkv"}
    def getPosition(self): return 0.0
    def isPositionEstablished(self): return True  # settled watcher; see suite_joinguard.py
    def isReady(self): return self._ready
    def setReady(self, v): self._ready = v
    def isAfk(self): return self._afk
    def setAfk(self, v): self._afk = v
    def sendSetAfk(self, username, isAfk, setBy=None):
        self.afkSets.append((username, isAfk) if setBy is None else (username, isAfk, setBy))
    def sendSetReady(self, username, isReady, manuallyInitiated=True, setByUsername=None):
        self.readyBroadcasts.append((username, isReady, manuallyInitiated))
    def sendChatMessage(self, m, skipIfSupportsFeature=None):
        if skipIfSupportsFeature and self.supportsFeature(skipIfSupportsFeature):
            return
        self.chats.append(m["message"])
    def clear(self):
        self.chats.clear(); self.afkSets.clear(); self.readyBroadcasts.clear()


class StubRoomManager:
    def broadcastRoom(self, sender, l):
        for w in sender.getRoom().getWatchers():
            l(w)
    def broadcast(self, sender, l):
        self.broadcastRoom(sender, l)


def make_factory(disableReady=False, pauseWarningAfter=0):
    f = SyncFactory.__new__(SyncFactory)
    f.disableReady = disableReady
    f.yapTimer = False
    f.pauseWarningAfter = pauseWarningAfter
    f.pauseWarningMessage = "Paused for {} - please resume"
    f.maxChatMessageLength = 150
    f._roomManager = StubRoomManager()
    return f


def make_room(name="d", watchers=()):
    room = Room(name, None)
    room._watchers = {w.getName(): w for w in watchers}
    for w in watchers:
        w._room = room
    return room


# ---------- hasAfkWatcher over the live roster ----------
cap = FW("cap", {"afk": True})
leg = FW("leg", {})  # stock: no afk feature
room = make_room("d", (cap, leg))
check("hasAfkWatcher false initially", room.hasAfkWatcher() is False)
cap._afk = True
check("hasAfkWatcher true when a watcher is AFK", room.hasAfkWatcher() is True)
del room._watchers["cap"]
check("hasAfkWatcher self-heals when AFK watcher leaves", room.hasAfkWatcher() is False)
cap._afk = False
room._watchers["cap"] = cap

# ---------- setAfk: broadcast split + forced not-ready ----------
f = make_factory()
cap._ready = True; leg._ready = True
for w in (cap, leg): w.clear()
f.setAfk(cap, True)
check("going AFK: capable peer gets Set:afk, no chat",
      cap.afkSets == [("cap", True)] and cap.chats == [], repr((cap.afkSets, cap.chats)))
check("going AFK: legacy peer gets chat fallback, no Set",
      leg.afkSets == [] and any("AFK" in c for c in leg.chats), repr(leg.chats))
check("going AFK forces not-ready (broadcast once to whole room)",
      ("cap", False, False) in cap.readyBroadcasts and ("cap", False, False) in leg.readyBroadcasts
      and cap._ready is False, repr(cap.readyBroadcasts))
check("watcher marked AFK", cap.isAfk() is True)

# idempotent
for w in (cap, leg): w.clear()
f.setAfk(cap, True)
check("setAfk to same value is a no-op", cap.afkSets == [] and leg.chats == [])

# returning from AFK: broadcast, no ready restore
for w in (cap, leg): w.clear()
f.setAfk(cap, False)
check("returning: capable peer gets Set:afk False", cap.afkSets == [("cap", False)])
check("returning: legacy peer gets chat", any("no longer AFK" in c for c in leg.chats), repr(leg.chats))
check("returning does NOT auto-restore ready", cap._ready is False and cap.readyBroadcasts == [],
      repr(cap.readyBroadcasts))

# disableReady: no ready force
fdr = make_factory(disableReady=True)
capdr = FW("capdr", {"afk": True}); capdr._ready = None
rdr = make_room("dr", (capdr,))
fdr.setAfk(capdr, True)
check("disableReady: AFK still set, no ready broadcast",
      capdr.isAfk() is True and capdr.readyBroadcasts == [], repr(capdr.readyBroadcasts))

# ---------- suppression: chat fallback gate + _pauseWarningActive untouched ----------
fp = make_factory(pauseWarningAfter=2)
pw_cap = FW("pcap", {"pauseWarning": True})
pw_leg = FW("pleg", {})
proom = make_room("p", (pw_cap, pw_leg))
proom._yapPauseStartedAt = time.time() - 30  # 30s into a pause
proom._yapPausedByName = "pcap"
proom._pauseWarningActive = True
for w in (pw_cap, pw_leg): w.clear()
fp._broadcastPauseWarningChat(proom)
check("no AFK: legacy gets pause-warning chat", any("Paused for" in c for c in pw_leg.chats), repr(pw_leg.chats))
check("no AFK: capable client skipped (gets warning via State, not chat)", pw_leg.chats and pw_cap.chats == [])
pw_leg.clear()
pw_cap._afk = True
fp._broadcastPauseWarningChat(proom)
check("AFK in room: chat fallback suppressed", pw_leg.chats == [], repr(pw_leg.chats))
check("suppression leaves _pauseWarningActive armed (auto-resumes)", proom._pauseWarningActive is True)
pw_cap._afk = False
fp._broadcastPauseWarningChat(proom)
check("AFK cleared: chat fallback resumes", any("Paused for" in c for c in pw_leg.chats), repr(pw_leg.chats))

# ---------- suppression: State pauseWarning field gate (real sendState) ----------
class FakePing:
    def newTimestamp(self): return 0
    def getRtt(self): return 0

def run_sendState(watcher, factory):
    p = SyncServerProtocol.__new__(SyncServerProtocol)
    p._factory = factory
    p._watcher = watcher
    p._pingService = FakePing()
    p._clientLatencyCalculationArrivalTime = 0
    p._clientLatencyCalculation = 0
    p.serverIgnoringOnTheFly = 0
    p.clientIgnoringOnTheFly = 0
    out = []
    p.sendMessage = lambda m: out.append(m)
    p.sendState(5.0, True, False, None, False)
    return out[0]["State"] if out else {}

sw_cap = FW("scap", {"pauseWarning": True})
sroom = make_room("s", (sw_cap,))
sroom._yapPauseStartedAt = time.time() - 30
sroom._yapPausedByName = "scap"
sroom._pauseWarningActive = True
st = run_sendState(sw_cap, fp)
check("State carries pauseWarning when nobody AFK", "pauseWarning" in st, repr(st.keys()))
sw_cap._afk = True
st = run_sendState(sw_cap, fp)
check("State omits pauseWarning while a watcher is AFK", "pauseWarning" not in st, repr(st.keys()))
# yap timer keeps flowing during AFK (independent of the warning)
fp.yapTimer = True
st = run_sendState(sw_cap, fp)
sw_cap._features["yapTimer"] = True
st = run_sendState(sw_cap, fp)
check("yapTimer State field still sent during AFK", "yapTimer" in st, repr(st.keys()))
fp.yapTimer = False
sw_cap._afk = False

# ---------- auto-clear on activity ----------
# (a) chatting clears AFK
fa = make_factory()
ca = FW("ca", {"afk": True}); ob = FW("ob", {"afk": True})
aroom = make_room("a", (ca, ob))
ca._afk = True
for w in (ca, ob): w.clear()
fa.sendChat(ca, "hello room")
check("plain chat clears the sender's AFK", ca.isAfk() is False and ("ca", False) in ob.afkSets,
      repr((ca.isAfk(), ob.afkSets)))
check("plain chat still broadcasts the message", any("hello room" in c for c in ob.chats), repr(ob.chats))

# (b) /afk chat toggles for stock clients (and does NOT self-cancel via the chat clear)
stockA = FW("stock", {})  # no afk feature
sroom2 = make_room("st", (stockA,))
stockA.clear()
fa.sendChat(stockA, "/afk")
check("/afk chat toggles a stock client to AFK", stockA.isAfk() is True, repr(stockA.isAfk()))
stockA.clear()
fa.sendChat(stockA, "/afk")
check("/afk chat toggles back off", stockA.isAfk() is False)

# (c) manual ready change clears AFK
fr = make_factory()
cr = FW("cr", {"afk": True}); cr._ready = False; cr._afk = True
rr = make_room("r", (cr,))
cr.clear()
fr.setReady(cr, True, manuallyInitiated=True)
check("manual ready=True clears AFK", cr.isAfk() is False, repr(cr.isAfk()))
# forced ready (manuallyInitiated=False) must NOT clear AFK
cr._afk = True; cr._ready = True; cr.clear()
fr.setReady(cr, False, manuallyInitiated=False)
check("forced ready=False (manuallyInitiated=False) does NOT clear AFK", cr.isAfk() is True)

# (d) updateState pause/seek clears AFK (via a real Watcher)
import unittest.mock as mock
server_mock = mock.Mock()
server_mock.setAfk = lambda w, v: w.setAfk(v)
conn = mock.Mock(); conn.isLogged.return_value = True
uw = Watcher(server_mock, conn, "uw")
uroom = Room("u", None); uroom.addWatcher(uw)
if uw._sendStateTimer and uw._sendStateTimer.running: uw._sendStateTimer.stop()
uroom.setPaused(Room.STATE_PLAYING, uw)  # room now playing
uw.setAfk(True)
uw.updateState(5.0, True, False, 0)  # report pause -> pauseChanged, but pausing is not "returning"
check("updateState pausing does NOT clear AFK (stepping away)", uw.isAfk() is True)
uw.updateState(5.0, False, False, 0)  # report unpause -> returning activity
check("updateState unpausing clears AFK", uw.isAfk() is False)
uw.setAfk(True)
uw.updateState(5.0, False, True, 0)   # doSeek
check("updateState seek clears AFK", uw.isAfk() is False)

# (e) controller readies an AFK target -> target's AFK cleared
fc = make_factory()
ctl = FW("ctl", {"afk": True}, admin=True)
target = FW("tgt", {"afk": True}); target._afk = True; target._ready = False
croom = make_room("c", (ctl, target))
ctl.clear(); target.clear()
fc.setReady(ctl, True, manuallyInitiated=True, username="tgt")
check("controller sets AFK target ready -> target AFK cleared", target.isAfk() is False, repr(target.isAfk()))

# ---------- protocol payload shapes ----------
cp = SyncClientProtocol.__new__(SyncClientProtocol)
out = []
cp.sendMessage = lambda m: out.append(m)
cp.setAfk(True)
check("client setAfk payload shape", out == [{"Set": {"afk": {"isAfk": True}}}], repr(out))

sp = SyncServerProtocol.__new__(SyncServerProtocol)
out2 = []
sp.sendMessage = lambda m: out2.append(m)
sp.sendSetAfk("alice", True)
check("server sendSetAfk payload shape", out2 == [{"Set": {"afk": {"username": "alice", "isAfk": True}}}], repr(out2))

# server handleSet routes afk to factory.setAfk with bool coercion
routed = []
sp2 = SyncServerProtocol.__new__(SyncServerProtocol)
sp2._logged = True
sp2._watcher = FW("hs", {"afk": True})
sp2._factory = types.SimpleNamespace(setAfk=lambda w, v, username=None: routed.append((w.getName(), v)))
sp2.handleSet({"afk": {"isAfk": 1}})
check("server handleSet afk branch coerces to bool", routed == [("hs", True)], repr(routed))
routed.clear()
sp2.handleSet({"afk": "garbage"})
check("server handleSet afk malformed: safe False", routed == [("hs", False)], repr(routed))

# ---------- player keybind: toggleAfkWithPause pauses on the way out ----------
from syncplay.client import SyncplayClient, SyncplayUser, SyncplayUserlist

def run_afk_keybind(currentlyAfk, playerPaused):
    c = SyncplayClient.__new__(SyncplayClient)
    c.serverVersion = "1.7.6"
    c.serverFeatures = {"afk": True}
    cu = SyncplayUser("me", "d"); cu.setAfk(currentlyAfk)
    c.userlist = types.SimpleNamespace(currentUser=cu)
    rec = []
    c.setPaused = lambda v: rec.append(("pause", v))
    c.toggleAfk = lambda: rec.append(("toggle",))
    c.getPlayerPaused = lambda: playerPaused
    c.toggleAfkWithPause()
    return rec

check("keybind: playing + not-AFK -> pause then set AFK",
      run_afk_keybind(False, False) == [("pause", True), ("toggle",)], repr(run_afk_keybind(False, False)))
check("keybind: already paused + not-AFK -> just set AFK",
      run_afk_keybind(False, True) == [("toggle",)], repr(run_afk_keybind(False, True)))
check("keybind: already AFK -> just toggle off, never pauses",
      run_afk_keybind(True, False) == [("toggle",)], repr(run_afk_keybind(True, False)))

# The keybind pause must NOT be re-interpreted by the readiness-toggle-on-pause machinery
# (_toggleReady): that races with the Set:afk echo and could clear the AFK we just set or flip
# readiness (observed as the status snapping to "Not ready" right after going AFK). Drive the real
# updatePlayerStatus and assert the only thing sent is setAfk - no setReady, no player revert.
def run_afk_keybind_then_pause(canControl):
    c = SyncplayClient.__new__(SyncplayClient)
    c.serverVersion = "1.7.6"
    c.serverFeatures = {"afk": True, "readiness": True}
    cu = SyncplayUser("me", "d"); cu.setReady(True); cu.setAfk(False)
    cu.file = {"name": "m.mkv", "duration": 3600, "path": "/m.mkv"}
    cu.canControl = lambda: canControl
    c.userlist = types.SimpleNamespace(currentUser=cu, isReady=lambda n: cu.isReady())
    c._playerPaused = False; c._globalPaused = False
    c._playerPosition = 5.0; c._globalPosition = 5.0
    c._lastGlobalUpdate = time.time(); c._lastPlayerUpdate = time.time()
    c.lastPausedOnLeaveTime = None; c.lastRewindTime = None; c.lastUpdatedFileTime = None
    c.lastAdvanceTime = None; c.playerPositionBeforeLastSeek = 5.0
    c._userOffset = 0.0; c.waitingToLoadNewfile = False
    c._afkKeybindPausePending = False
    wire = []
    class FakePlayer:
        def setPaused(self, v): wire.append(("player.setPaused", v))
        def setPosition(self, p): pass
    c._player = FakePlayer()
    class FakeProto:
        def setAfk(self, v): wire.append(("setAfk", v))
        def setReady(self, v, m, u=None): wire.append(("setReady", v, m))
        def sendState(self, *a, **k): pass
    c._protocol = FakeProto()
    c.ui = types.SimpleNamespace(showMessage=lambda *a, **k: None,
                                 showDebugMessage=lambda *a, **k: None,
                                 showErrorMessage=lambda *a, **k: None,
                                 userListChange=lambda: None)
    c._warnings = types.SimpleNamespace(checkReadyStates=lambda: None)
    c.playlist = types.SimpleNamespace(advancePlaylistCheck=lambda: None,
                                       notJustChangedPlaylist=lambda: True,
                                       canSwitchToNextPlaylistIndex=lambda: False)
    c.toggleAfkWithPause()          # issues player pause + sends setAfk, arms the one-shot
    c.updatePlayerStatus(True, 5.0) # player reports the pause before the Set:afk echo arrives
    return wire

for role, ctl in (("controller", True), ("non-controller", False)):
    w = run_afk_keybind_then_pause(ctl)
    check("keybind pause (%s): sends setAfk(True)" % role, ("setAfk", True) in w, repr(w))
    check("keybind pause (%s): no spurious setReady" % role,
          not any(e[0] == "setReady" for e in w), repr(w))
    check("keybind pause (%s): pause not reverted on the player" % role,
          ("player.setPaused", False) not in w, repr(w))

# The one-shot must not leak into the next, genuine user pause (that one SHOULD toggle readiness).
def normal_user_pause_still_toggles_ready():
    c = SyncplayClient.__new__(SyncplayClient)
    c.serverVersion = "1.7.6"; c.serverFeatures = {"afk": True, "readiness": True}
    cu = SyncplayUser("me", "d"); cu.setReady(True); cu.setAfk(False)
    cu.file = {"name": "m.mkv", "duration": 3600, "path": "/m.mkv"}
    cu.canControl = lambda: True
    c.userlist = types.SimpleNamespace(currentUser=cu, isReady=lambda n: cu.isReady())
    c._playerPaused = False; c._globalPaused = False
    c._playerPosition = 5.0; c._globalPosition = 5.0
    c._lastGlobalUpdate = time.time(); c._lastPlayerUpdate = time.time()
    c.lastPausedOnLeaveTime = None; c.lastRewindTime = None; c.lastUpdatedFileTime = None
    c.lastAdvanceTime = None; c.playerPositionBeforeLastSeek = 5.0
    c._userOffset = 0.0; c.waitingToLoadNewfile = False
    c._afkKeybindPausePending = False   # no keybind involved this time
    wire = []
    c._player = types.SimpleNamespace(setPaused=lambda v: None, setPosition=lambda p: None)
    class FakeProto:
        def setAfk(self, v): wire.append(("setAfk", v))
        def setReady(self, v, m, u=None): wire.append(("setReady", v, m))
        def sendState(self, *a, **k): pass
    c._protocol = FakeProto()
    c.ui = types.SimpleNamespace(showMessage=lambda *a, **k: None,
                                 showDebugMessage=lambda *a, **k: None,
                                 showErrorMessage=lambda *a, **k: None,
                                 userListChange=lambda: None)
    c._warnings = types.SimpleNamespace(checkReadyStates=lambda: None)
    c.playlist = types.SimpleNamespace(advancePlaylistCheck=lambda: None,
                                       notJustChangedPlaylist=lambda: True,
                                       canSwitchToNextPlaylistIndex=lambda: False)
    c.updatePlayerStatus(True, 5.0)
    return wire

nw = normal_user_pause_still_toggles_ready()
check("normal user pause still toggles readiness (guard didn't leak)",
      any(e[0] == "setReady" for e in nw), repr(nw))

# ---------- client-side userlist + user model ----------
u = SyncplayUser("bob", "d")
check("SyncplayUser defaults not AFK", u.isAfk() is False)
u.setAfk(True)
check("SyncplayUser setAfk", u.isAfk() is True)

ul = SyncplayUserlist.__new__(SyncplayUserlist)
ul.currentUser = SyncplayUser("me", "d")
ul._users = {}
ul._roomUsersChanged = False
ul.ui = types.SimpleNamespace(userListChange=lambda: None)
ul._client = types.SimpleNamespace(autoplayCheck=lambda: None)
ul.addUser("alice", "d", None, noMessage=True, isAfk=True)
check("userlist addUser stores AFK", ul.isAfk("alice") is True)
ul.setAfk("alice", False)
check("userlist setAfk updates", ul.isAfk("alice") is False)
ul.addUser("me", "d", None, noMessage=True, isAfk=True)  # currentUser early-return path
check("userlist addUser sets currentUser AFK (List refresh path)", ul.currentUser.isAfk() is True)
check("userlist getUserRoom", ul.getUserRoom("alice") == "d" and ul.getUserRoom("nobody") is None)

# ---------- persistent OSD: AFK listed on its own line, separate from plain not-ready ----------
ol = SyncplayUserlist.__new__(SyncplayUserlist)
ol.currentUser = SyncplayUser("me", "d")
ol.currentUser.setReady(True)
afkUser = SyncplayUser("alice", "d", file_={"name": "x"})
afkUser.setReady(False); afkUser.setAfk(True)
notReadyUser = SyncplayUser("bob", "d", file_={"name": "x"})
notReadyUser.setReady(False)
ol._users = {"alice": afkUser, "bob": notReadyUser}
check("usersInRoomAfk lists only AFK users", ol.usersInRoomAfk() == "alice", repr(ol.usersInRoomAfk()))
check("usersInRoomNotReady(excludeAfk) drops AFK users",
      ol.usersInRoomNotReady(excludeAfk=True) == "bob", repr(ol.usersInRoomNotReady(excludeAfk=True)))
nr_all = ol.usersInRoomNotReady()
check("usersInRoomNotReady default still counts AFK as not-ready",
      "alice" in nr_all and "bob" in nr_all, repr(nr_all))
# an AFK current user shows up on the AFK line
ol.currentUser.setAfk(True)
check("usersInRoomAfk includes AFK currentUser", "me" in ol.usersInRoomAfk(), repr(ol.usersInRoomAfk()))

# ---------- feature advertisement ----------
pf = make_factory()
pf.isolateRooms = False; pf.roomsDbFile = None; pf.disableChat = False
pf.maxUsernameLength = 16; pf.adminPassword = None
feats = SyncFactory.getFeatures(pf)
check("server featureList advertises afk=True", feats.get("afk") is True, repr(feats.get("afk")))

# ---------- i18n ----------
keys = ["set-as-afk-notification", "set-as-not-afk-notification", "other-afk-notification",
        "other-not-afk-notification", "afk-userlist-userflag", "afk-on-chat-message",
        "afk-off-chat-message", "feature-afk", "afk-menu-label", "not-afk-menu-label",
        "afk-tooltip", "afk-guipushbuttonlabel", "commandlist-notification/afk",
        "afk-osd-notification"]
for k in keys:
    check("en key: " + k, k in M.messages["en"])
bad = [l for l in M.getMissingStrings().splitlines() if "Unused" in l and "afk" in l.lower()]
check("no afk keys leaked to non-English dicts", not bad, repr(bad))

# ---------- GUI: offscreen MainWindow (three-way ready/AFK radio + delegate) ----------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
gui_ok, gui_detail = False, ""
try:
    from syncplay.vendor.Qt import QtWidgets, QtGui, QtCore
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])
    from syncplay.ui.gui import MainWindow
    w = MainWindow()
    steps = []
    radios = [w.readyRadio, w.notReadyRadio, w.afkRadio]
    steps.append(("three radios exist", all(isinstance(r, QtWidgets.QRadioButton) for r in radios)))
    steps.append(("radios share one exclusive group",
                  set(w.readyAfkButtonGroup.buttons()) == set(radios) and w.readyAfkButtonGroup.exclusive()))
    steps.append(("afk radio starts disabled", not w.afkRadio.isEnabled()))
    # feature gating
    w.setFeatures({"readiness": True, "chat": True, "sharedPlaylists": True, "afk": True})
    steps.append(("afk radio enabled when server advertises afk", w.afkRadio.isEnabled()))
    w.setFeatures({"readiness": True, "chat": True, "sharedPlaylists": True})  # stock server, no afk key
    steps.append(("afk radio disabled on stock server (no KeyError)", not w.afkRadio.isEnabled()))
    steps.append(("ready/not-ready stay enabled on stock server",
                  w.readyRadio.isEnabled() and w.notReadyRadio.isEnabled()))
    w.setFeatures({"readiness": False, "chat": True, "sharedPlaylists": True})
    steps.append(("all three disabled when readiness unsupported",
                  not any(r.isEnabled() for r in radios)))
    w.setFeatures({"readiness": True, "chat": True, "sharedPlaylists": True, "afk": True})  # re-enable
    # updateReadyAfkRadio selects the right option (and exclusivity unchecks the rest)
    for ready, afk, want in [(True, False, w.readyRadio), (False, False, w.notReadyRadio),
                             (None, False, w.notReadyRadio), (True, True, w.afkRadio),
                             (False, True, w.afkRadio)]:
        w.updateReadyAfkRadio(ready, afk)
        sel = [r for r in radios if r.isChecked()]
        steps.append(("radio ({}, {}) -> only the right one".format(ready, afk),
                      sel == [want]))
    # selection handlers dispatch to the correct client calls
    import unittest.mock as _mock
    w._syncplayClient = _mock.Mock()
    w._syncplayClient.userlist.currentUser.isAfk.return_value = False
    w.selectReady()
    steps.append(("selectReady -> changeReadyState(True)",
                  w._syncplayClient.changeReadyState.call_args[0] == (True,)))
    w._syncplayClient.reset_mock(); w._syncplayClient.userlist.currentUser.isAfk.return_value = False
    w.selectNotReady()
    steps.append(("selectNotReady (not afk) -> changeReadyState(False)",
                  w._syncplayClient.changeReadyState.call_args[0] == (False,) and not w._syncplayClient.changeAfkState.called))
    w._syncplayClient.reset_mock(); w._syncplayClient.userlist.currentUser.isAfk.return_value = True
    w.selectNotReady()
    steps.append(("selectNotReady (afk) -> changeAfkState(False)",
                  w._syncplayClient.changeAfkState.call_args[0] == (False,) and not w._syncplayClient.changeReadyState.called))
    w._syncplayClient.reset_mock()
    w.selectAfk()
    steps.append(("selectAfk -> changeAfkState(True)",
                  w._syncplayClient.changeAfkState.call_args[0] == (True,)))
    # userlist delegate still paints an AFK row (icons unchanged)
    model = QtGui.QStandardItemModel()
    item = QtGui.QStandardItem("alice")
    item.setData(True, QtCore.Qt.UserRole + constants.USERITEM_AFK_ROLE)
    item.setData(False, QtCore.Qt.UserRole + constants.USERITEM_READY_ROLE)
    model.appendRow(item)
    pix = QtGui.QPixmap(200, 20); painter = QtGui.QPainter(pix)
    opt = QtWidgets.QStyleOptionViewItem(); opt.rect = QtCore.QRect(0, 0, 200, 20)
    w.listTreeView.itemDelegate().paint(painter, opt, model.index(0, 0))
    painter.end()
    steps.append(("userlist delegate paints an AFK row", True))
    gui_ok = all(v for _, v in steps)
    gui_detail = "; ".join(n for n, v in steps if not v) or "all live GUI steps verified"
except Exception as e:
    gui_detail = "offscreen construction failed: {}: {}".format(type(e).__name__, e)
check("GUI: offscreen MainWindow ready/AFK radio + delegate", gui_ok, gui_detail)

# ---------- setOthersAfk: targeted AFK changes (mirrors setOthersReadiness) ----------
fo = make_factory()
setter = FW("setter", {"afk": True})
tgt2 = FW("tgt2", {"afk": True}); tgt2._ready = True
legobs = FW("legobs", {})
oroom = make_room("o", (setter, tgt2, legobs))
for w in (setter, tgt2, legobs): w.clear()
fo.setAfk(setter, True, username="tgt2")
check("targeted set: target marked AFK", tgt2.isAfk() is True)
check("targeted set: capable peers get Set:afk carrying setBy",
      ("tgt2", True, "setter") in setter.afkSets and ("tgt2", True, "setter") in tgt2.afkSets,
      repr((setter.afkSets, tgt2.afkSets)))
check("targeted set: legacy peer gets setter-attributed chat",
      any("has marked" in c and "tgt2" in c for c in legobs.chats), repr(legobs.chats))
check("targeted set: target forced not-ready",
      tgt2._ready is False and ("tgt2", False, False) in setter.readyBroadcasts, repr(setter.readyBroadcasts))

for w in (setter, tgt2, legobs): w.clear()
fo.setAfk(setter, False, username="tgt2")
check("targeted clear: target no longer AFK, setBy still carried",
      tgt2.isAfk() is False and ("tgt2", False, "setter") in tgt2.afkSets, repr(tgt2.afkSets))
check("targeted clear: legacy peer chat names the target",
      any("no longer AFK" in c and "tgt2" in c for c in legobs.chats), repr(legobs.chats))

# username naming yourself routes through the plain self path (no setBy on the wire)
for w in (setter, tgt2, legobs): w.clear()
fo.setAfk(setter, True, username="setter")
check("targeted set on self behaves as self-toggle (no setBy)",
      setter.isAfk() is True and ("setter", True) in tgt2.afkSets, repr(tgt2.afkSets))
fo.setAfk(setter, False)

# missing target: silent no-op (mirrors setReady), nothing broadcast
for w in (setter, tgt2, legobs): w.clear()
fo.setAfk(setter, True, username="ghost")
check("targeted set on missing user: silent no-op",
      setter.afkSets == [] and tgt2.afkSets == [] and legobs.chats == [],
      repr((setter.afkSets, legobs.chats)))

# locked plain room: non-admins are refused with a private error, admins pass
oroom.setLocked(True)
for w in (setter, tgt2, legobs): w.clear()
fo.setAfk(setter, True, username="tgt2")
check("locked room, non-admin: target unchanged", tgt2.isAfk() is False)
check("locked room, non-admin: private error to setter only",
      any("not authorised" in c for c in setter.chats) and tgt2.chats == [] and legobs.chats == [],
      repr((setter.chats, legobs.chats)))
adminW = FW("adm", {"afk": True}, admin=True)
oroom._watchers["adm"] = adminW; adminW._room = oroom
for w in (setter, tgt2, legobs, adminW): w.clear()
fo.setAfk(adminW, True, username="tgt2")
check("locked room, admin: targeted set works",
      tgt2.isAfk() is True and ("tgt2", True, "adm") in tgt2.afkSets, repr(tgt2.afkSets))
oroom.setLocked(False)
fo.setAfk(adminW, False, username="tgt2")

# /afk <name> chat command (stock-client surface): toggle resolved server-side
fchat = make_factory()
sctl = FW("sctl", {})
stgt = FW("stgt", {})
chroom = make_room("ch", (sctl, stgt))
for w in (sctl, stgt): w.clear()
fchat.sendChat(sctl, "/afk stgt")
check("/afk <name> chat: toggles the target AFK", stgt.isAfk() is True and sctl.isAfk() is False)
check("/afk <name> chat: room got setter-attributed chat",
      any("has marked" in c and "stgt" in c for c in stgt.chats), repr(stgt.chats))
fchat.sendChat(sctl, "/afk stgt")
check("/afk <name> chat: second call toggles back off", stgt.isAfk() is False)
for w in (sctl, stgt): w.clear()
fchat.sendChat(sctl, "/afk ghost")
check("/afk <unknown> chat: private not-found error, sender untouched",
      any("no user called" in c for c in sctl.chats) and stgt.chats == [] and sctl.isAfk() is False,
      repr(sctl.chats))
for w in (sctl, stgt): w.clear()
fchat.sendChat(sctl, "/afk sctl")
check("/afk <own name> chat: plain self-toggle", sctl.isAfk() is True)
fchat.sendChat(sctl, "/afk sctl")

# protocol payload shapes for the targeted variants
out3 = []
cp2 = SyncClientProtocol.__new__(SyncClientProtocol)
cp2.sendMessage = lambda m: out3.append(m)
cp2.setAfk(True, "bob")
check("client targeted setAfk payload includes username",
      out3 == [{"Set": {"afk": {"isAfk": True, "username": "bob"}}}], repr(out3))

out4 = []
sp3 = SyncServerProtocol.__new__(SyncServerProtocol)
sp3.sendMessage = lambda m: out4.append(m)
sp3.sendSetAfk("alice", True, "bob")
check("server sendSetAfk payload includes setBy",
      out4 == [{"Set": {"afk": {"username": "alice", "isAfk": True, "setBy": "bob"}}}], repr(out4))
out4.clear()
sp3.sendSetAfk("alice", True)
check("server sendSetAfk without setBy keeps the legacy shape",
      out4 == [{"Set": {"afk": {"username": "alice", "isAfk": True}}}], repr(out4))

routed2 = []
sp4 = SyncServerProtocol.__new__(SyncServerProtocol)
sp4._logged = True
sp4._watcher = FW("hs2", {"afk": True})
sp4._factory = types.SimpleNamespace(setAfk=lambda w, v, username=None: routed2.append((w.getName(), v, username)))
sp4.handleSet({"afk": {"isAfk": True, "username": "bob"}})
check("server handleSet passes username through", routed2 == [("hs2", True, "bob")], repr(routed2))

routed3 = []
ccp = SyncClientProtocol.__new__(SyncClientProtocol)
ccp._client = types.SimpleNamespace(setAfk=lambda u, a, s=None: routed3.append((u, a, s)))
ccp.handleSet({"afk": {"username": "alice", "isAfk": True, "setBy": "bob"}})
check("client handleSet passes setBy through", routed3 == [("alice", True, "bob")], repr(routed3))
routed3.clear()
ccp.handleSet({"afk": {"username": "alice", "isAfk": True}})
check("client handleSet tolerates missing setBy", routed3 == [("alice", True, None)], repr(routed3))

# client-side notification wording for targeted changes
def client_setAfk_msgs(target, setBy):
    c = SyncplayClient.__new__(SyncplayClient)
    msgs = []
    cu = SyncplayUser("me", "d")
    c.userlist = types.SimpleNamespace(currentUser=cu, isAfk=lambda n: False,
                                       setAfk=lambda n, v: None, isRoomSame=lambda r: True,
                                       getUserRoom=lambda n: "d")
    c.ui = types.SimpleNamespace(showMessage=lambda m: msgs.append(m), userListChange=lambda: None)
    c.setAfk(target, True, setBy)
    return msgs

m1 = client_setAfk_msgs("me", "adm")
check("client notification: you marked AFK by another -> names the setter",
      m1 and "marked as AFK by" in m1[0] and "adm" in m1[0], repr(m1))
m2 = client_setAfk_msgs("alice", "adm")
check("client notification: other marked AFK -> names target and setter",
      m2 and "alice" in m2[0] and "adm" in m2[0], repr(m2))
m3 = client_setAfk_msgs("alice", None)
check("client notification: no setBy keeps the classic message",
      m3 and "is now AFK" in m3[0], repr(m3))
m4 = client_setAfk_msgs("alice", "alice")
check("client notification: setBy == target treated as self-toggle wording",
      m4 and "is now AFK" in m4[0], repr(m4))

# setOthersAfk client call is gated on its own server feature
def run_setOthersAfk(features):
    c = SyncplayClient.__new__(SyncplayClient)
    c.serverVersion = "1.7.6"
    c.serverFeatures = features
    sent = []
    c._protocol = types.SimpleNamespace(setAfk=lambda v, u=None: sent.append((v, u)))
    c.ui = types.SimpleNamespace(showErrorMessage=lambda m: sent.append(("err", m)))
    c.setOthersAfk("bob", True)
    return sent

check("setOthersAfk sends targeted Set when server supports it",
      run_setOthersAfk({"afk": True, "setOthersAfk": True}) == [(True, "bob")],
      repr(run_setOthersAfk({"afk": True, "setOthersAfk": True})))
gated = run_setOthersAfk({"afk": True})  # older fork server: afk but no setOthersAfk
check("setOthersAfk refused when server lacks the flag (never a self-toggle)",
      gated and gated[0][0] == "err", repr(gated))

# feature advertisement
check("server featureList advertises setOthersAfk=True", feats.get("setOthersAfk") is True,
      repr(feats.get("setOthersAfk")))

# i18n for the new keys
for k in ["set-afk-by-other-notification", "set-not-afk-by-other-notification",
          "other-set-afk-notification", "other-set-not-afk-notification",
          "set-others-afk-chat-message", "set-others-not-afk-chat-message",
          "cannot-set-others-afk-error-chat-message", "afk-user-not-found-error-chat-message",
          "feature-setOthersAfk", "setasafk-menu-label", "setasnotafk-menu-label"]:
    check("en key: " + k, k in M.messages["en"])

# ---------- constants ----------
check("COMMANDS_AFK present", constants.COMMANDS_AFK == ["afk"])
check("AFK_COMMAND token", constants.AFK_COMMAND == "/afk")
check("USERITEM_AFK_ROLE distinct from READY role",
      constants.USERITEM_AFK_ROLE != constants.USERITEM_READY_ROLE)

fails = [x for x in RESULTS if not x[1]]
print("\n===== AFK SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
