"""Unit suite for server admins + room locking + client auto-auth."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import sys, time, types
import unittest.mock as mock
from syncplay import constants
from syncplay.utils import meetsMinVersion
from syncplay.server import Room, ControlledRoom, SyncFactory, Watcher
from syncplay.protocols import SyncServerProtocol, SyncClientProtocol
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Admin :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

class FW:
    """Lightweight watcher stand-in for authority/position tests."""
    def __init__(self, name, admin=False, pos=0.0, version="1.7.6", features=None):
        self._name, self._admin, self._pos = name, admin, pos
        self._version, self._features = version, features or {}
        self.chats, self.osds, self.authStatuses = [], [], []
        self._room = None
    def getName(self): return self._name
    def isAdmin(self): return self._admin
    def setAdmin(self, v): self._admin = v
    def isAfk(self): return False
    def getPosition(self): return self._pos
    def isPositionEstablished(self): return True  # settled watcher; the join guard itself is covered by suite_joinguard.py
    def getFile(self): return {"name": "f.mkv"}
    def getRoom(self): return self._room
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def isController(self):
        return self._admin or (self._room is not None and
                               "+" in self._room.getName() and self._room.canControl(self))
    def sendChatMessage(self, m, skipIfSupportsFeature=None):
        if meetsMinVersion(self._version, constants.CHAT_MIN_VERSION):
            if skipIfSupportsFeature and self.supportsFeature(skipIfSupportsFeature):
                return
            self.chats.append(m["message"])
    def sendOSDMessage(self, p): self.osds.append(p)
    def sendControlledRoomAuthStatus(self, success, username, room):
        self.authStatuses.append((success, username, room))
    def __lt__(self, other): return self.getPosition() < other.getPosition()

class StubRoomManager:
    def broadcastRoom(self, sender, l):
        for w in sender.getRoom().getWatchers():
            l(w)
    def broadcast(self, sender, l):
        self.broadcastRoom(sender, l)

def make_factory(adminPassword=None):
    f = SyncFactory.__new__(SyncFactory)
    f.adminPassword = adminPassword
    f.yapTimer = False
    f.pauseWarningAfter = 0
    f.maxChatMessageLength = 150
    f._roomManager = StubRoomManager()
    f._trackCache = {}  # per-room layout->proposal cache (setWatcherRoom reads it on room switch)
    return f

admin = FW("adm", admin=True)
user = FW("usr", admin=False)

# ---------- authority matrix ----------
plain = Room("plain", None)
locked = Room("locked", None); locked.setLocked(True)
managed = ControlledRoom("+m:ABCDEFGHIJKL", None)
managed._controllers = {"ctl": FW("ctl")}
ctl = managed._controllers["ctl"]

matrix = [
    (plain, admin, True), (plain, user, True), (plain, None, True),
    (locked, admin, True), (locked, user, False), (locked, None, False),
    (managed, admin, True), (managed, user, False), (managed, None, False), (managed, ctl, True),
]
for room, w, want in matrix:
    got = room.canControl(w)
    who = w.getName() if w else "None"
    check("canControl {}({}) == {}".format(room.getName(), who, want), got == want, "got {}".format(got))

# unlock restores
locked.setLocked(False)
check("unlock restores free-for-all", locked.canControl(user) is True)
locked.setLocked(True)

# ---------- isController via real Watcher ----------
server_mock = mock.Mock(); conn = mock.Mock(); conn.isLogged.return_value = True
rw = Watcher(server_mock, conn, "someone")
proom = Room("p2", None); proom.addWatcher(rw)
if rw._sendStateTimer and rw._sendStateTimer.running: rw._sendStateTimer.stop()
check("regular watcher in plain room: not controller", rw.isController() is False)
rw.setAdmin(True)
check("admin watcher: isController even in plain room", rw.isController() is True)
check("isAdmin getter", rw.isAdmin() is True)
cw = Watcher(server_mock, conn, "pleb")
croom = ControlledRoom("+c:ABCDEFGHIJKL", None); croom.addWatcher(cw)
if cw._sendStateTimer and cw._sendStateTimer.running: cw._sendStateTimer.stop()
check("non-controller in managed room: not controller", cw.isController() is False)
cw.setAdmin(True)
check("admin in managed room: controller without password", cw.isController() is True and croom.canControl(cw) is True)

# ---------- gated Room setters ----------
r = Room("g", None)
r.setPaused(Room.STATE_PLAYING, user)
check("unlocked: user can set playstate", r.isPlaying())
r.setLocked(True)
r.setPaused(Room.STATE_PAUSED, user)
check("locked: user pause ignored", r.isPlaying(), "room still playing")
r.setPaused(Room.STATE_PAUSED, admin)
check("locked: admin pause applies", r.isPaused())
r.setPosition(50.0, user)
check("locked: user position ignored", r._position != 50.0)
r.setPosition(50.0, admin)
check("locked: admin position applies", r._position == 50.0)
r.setPlaylist(["a.mkv"], user)
check("locked: user playlist ignored", r.getPlaylist() == [])
r.setPlaylist(["a.mkv"], admin)
check("locked: admin playlist applies", r.getPlaylist() == ["a.mkv"])
r.setPlaylistIndex(0, user)
check("locked: user playlist index ignored", r.getPlaylistIndex() is None)
r.setPlaylistIndex(0, admin)
check("locked: admin playlist index applies", r.getPlaylistIndex() == 0)

# ---------- getPosition reference pools ----------
pr = Room("pos", None)
wA = FW("a", admin=False, pos=10.0)
wB = FW("b", admin=True, pos=99.0)
pr._watchers = {"a": wA, "b": wB}
pr._lastUpdate = time.time() - 5
check("unlocked: min watcher is reference (10.0)", pr.getPosition() == 10.0)
pr.setLocked(True); pr._lastUpdate = time.time() - 5
check("locked: admin is reference despite higher pos (99.0)", pr.getPosition() == 99.0)
pr._watchers = {"a": wA}  # no admin present
pr._position = 42.0; pr._playState = Room.STATE_PAUSED; pr._lastUpdate = time.time() - 5
check("locked + no admin: extrapolates stored position", pr.getPosition() == 42.0)

cpr = ControlledRoom("+cp:ABCDEFGHIJKL", None)
adm2 = FW("adm2", admin=True, pos=7.0)
cpr._watchers = {"adm2": adm2, "usr": FW("u", pos=3.0)}
cpr._controllers = {}
cpr._lastUpdate = time.time() - 5
check("managed room: admin is position reference when no controllers", cpr.getPosition() == 7.0)

# ---------- chat dispatcher ----------
f = make_factory(adminPassword="S3cret")
droom = Room("d", None)
sender = FW("sender"); other = FW("other")
sender._room = droom; other._room = droom
droom._watchers = {"sender": sender, "other": other}

f.sendChat(sender, "/unknown hello")
check("unknown /command: private warning to sender, NOT broadcast",
      other.chats == [] and len(sender.chats) == 1 and "/unknown" in sender.chats[0], repr(sender.chats))
check("unknown /command warning omits arguments (no leak)", "hello" not in sender.chats[0], repr(sender.chats))
other.chats.clear(); sender.chats.clear()
f.sendChat(sender, "/osdx not a command")
check("/osdx (prefix collision): warned, not broadcast",
      other.chats == [] and len(sender.chats) == 1 and "/osdx" in sender.chats[0], repr(sender.chats))
other.chats.clear(); sender.chats.clear()
f.sendChat(sender, "plain chat message")
check("normal chat (no slash) still broadcasts", other.chats == ["plain chat message"], repr(other.chats))
other.chats.clear(); sender.chats.clear()

f.sendChat(sender, "/admin WRONG")
check("/admin wrong: private fail only", sender.chats == ["Wrong admin password."] and other.chats == [])
check("/admin wrong: no admin granted", sender.isAdmin() is False)
sender.chats.clear()
f.sendChat(sender, "/admin")
check("/admin with no password arg: private fail", sender.chats == ["Wrong admin password."])
sender.chats.clear()
f.sendChat(sender, "/admin S3cret")
check("/admin correct: private success", sender.chats == ["You are now a server admin."] and other.chats == [])
check("/admin correct: admin granted", sender.isAdmin() is True)
check("controller status broadcast to room", ("adm", ) not in other.authStatuses and (True, "sender", "d") in other.authStatuses,
      repr(other.authStatuses))
check("password never reached room chat", all("S3cret" not in c for c in other.chats))
sender.chats.clear(); other.chats.clear()

f2 = make_factory(adminPassword=None)
s2 = FW("s2"); s2._room = droom
droom._watchers["s2"] = s2
f2.sendChat(s2, "/admin whatever")
check("/admin with feature disabled: private notice, never falls through",
      s2.chats == ["This server has no admin password configured."] and
      all("whatever" not in c for c in other.chats), repr(s2.chats))
del droom._watchers["s2"]

# ---------- adminAuth Set branch (modded-client path) ----------
p = SyncServerProtocol.__new__(SyncServerProtocol)
p._factory = f
setw = FW("setguy"); setw._room = droom; droom._watchers["setguy"] = setw
p._watcher = setw
p._logged = True
p.handleSet({"adminAuth": {"password": "S3cret"}})
check("Set:adminAuth correct: admin granted + private success",
      setw.isAdmin() is True and setw.chats == ["You are now a server admin."])
setw.chats.clear(); setw.setAdmin(False)
p.handleSet({"adminAuth": {"password": "nope"}})
check("Set:adminAuth wrong: fail, no grant", setw.isAdmin() is False and setw.chats == ["Wrong admin password."])
p.handleSet({"adminAuth": "garbage"})
check("Set:adminAuth malformed: safe fail", setw.isAdmin() is False)
del droom._watchers["setguy"]

# ---------- /lock and /unlock ----------
other.chats.clear(); sender.chats.clear()
f.sendChat(other, "/lock")
check("/lock by non-admin: private unauthorised",
      other.chats == ["Only server admins can do that. Authenticate with /admin <password>."] and droom.isLocked() is False)
other.chats.clear()
f.sendChat(sender, "/lock")   # sender is admin from earlier
check("/lock by admin: room locked + room-wide notice",
      droom.isLocked() is True and any("locked this room" in c for c in other.chats)
      and any("locked this room" in c for c in sender.chats), repr(other.chats))
other.chats.clear(); sender.chats.clear()
f.sendChat(sender, "/unlock")
check("/unlock: room unlocked + notice", droom.isLocked() is False and any("unlocked" in c for c in other.chats))
other.chats.clear(); sender.chats.clear()

# ---------- /togglelock (Ctrl+L keybind) ----------
assert droom.isLocked() is False
f.sendChat(sender, "/togglelock")   # sender is admin; room currently unlocked
check("/togglelock by admin (unlocked->locked): room locked + notice",
      droom.isLocked() is True and any("locked this room" in c for c in other.chats))
other.chats.clear(); sender.chats.clear()
f.sendChat(sender, "/togglelock")   # now locked -> unlock
check("/togglelock by admin (locked->unlocked): room unlocked + notice",
      droom.isLocked() is False and any("unlocked" in c for c in other.chats))
other.chats.clear(); sender.chats.clear()
f.sendChat(other, "/togglelock")   # non-admin
check("/togglelock by non-admin: private unauthorised, no change",
      other.chats == ["Only server admins can do that. Authenticate with /admin <password>."]
      and droom.isLocked() is False)
other.chats.clear(); sender.chats.clear()

mroom = ControlledRoom("+mm:ABCDEFGHIJKL", None)
sender._room = mroom; mroom._watchers = {"sender": sender}
f.sendChat(sender, "/lock")
check("/lock in managed room: private already-managed notice",
      sender.chats == ["This room is already managed - /lock only applies to plain rooms."] and mroom.isLocked() is False)
sender.chats.clear()
f.sendChat(sender, "/togglelock")
check("/togglelock in managed room: private already-managed notice",
      sender.chats == ["This room is already managed - /lock only applies to plain rooms."] and mroom.isLocked() is False)
sender._room = droom; sender.chats.clear()

# ---------- /osd by admin in plain room ----------
f.pauseWarningAfter = 0
f.sendChat(sender, "/osd dur=3 Admin announcement")
check("admin /osd in plain room: broadcast (fallback chat)", other.chats == ["Admin announcement"], repr(other.chats))
other.chats.clear()
f.sendChat(other, "/osd nope")
check("non-admin /osd in plain room still unauthorised",
      other.chats == ["Only room operators and server admins can use /osd."])
other.chats.clear()

# ---------- setWatcherRoom re-broadcast hook ----------
swf = make_factory(adminPassword="x")
swf.roomsDbFile = None
class RM2(StubRoomManager):
    def moveWatcher(self, watcher, roomName):
        room = Room(roomName, None)
        room._watchers = {watcher.getName(): watcher}
        watcher._room = room
swf._roomManager = RM2()
mover = FW("mover", admin=True); mover.isReady = lambda: None
watcherB = FW("obs")
swf.sendJoinMessage = lambda w: None
swf.sendRoomSwitchMessage = lambda w: None
mover.setPlaylist = lambda *a: None
mover.setPlaylistIndex = lambda *a: None
swf.setWatcherRoom(mover, "newroom")
check("room switch re-broadcasts admin status", (True, "mover", "newroom") in mover.authStatuses, repr(mover.authStatuses))

# ---------- client: auto-auth hook + protocol ----------
from syncplay.client import SyncplayClient
stub = types.SimpleNamespace()
sent = []
stub._config = {"adminPassword": "pw"}
stub.serverFeatures = {"serverAdmin": True}
stub._protocol = types.SimpleNamespace(sendAdminAuth=lambda pw: sent.append(pw))
SyncplayClient._autoAuthAdmin(stub)
check("auto-auth fires with password + serverAdmin feature", sent == ["pw"])
sent.clear()
stub.serverFeatures = {"serverAdmin": False}
SyncplayClient._autoAuthAdmin(stub)
check("auto-auth suppressed without serverAdmin feature", sent == [])
stub.serverFeatures = {"serverAdmin": True}; stub._config = {"adminPassword": None}
SyncplayClient._autoAuthAdmin(stub)
check("auto-auth suppressed without password", sent == [])
stub._config = {}
SyncplayClient._autoAuthAdmin(stub)
check("auto-auth safe with key entirely absent", sent == [])

cp = SyncClientProtocol.__new__(SyncClientProtocol)
out = []
cp.sendMessage = lambda m: out.append(m)
cp.sendAdminAuth("pw123")
check("sendAdminAuth payload shape", out == [{"Set": {"adminAuth": {"password": "pw123"}}}], repr(out))

# ---------- client-side command forwarding (consoleUI.executeCommand) ----------
from syncplay.ui.consoleUI import ConsoleUI
cui = ConsoleUI.__new__(ConsoleUI)
cui._syncplayClient = mock.Mock()
cui.showMessage = lambda *a, **k: None
cui.executeCommand("lock")  # GUI path: slash already stripped
check("client forwards unknown command to server as /command",
      cui._syncplayClient.sendChat.call_args is not None and cui._syncplayClient.sendChat.call_args[0] == ("/lock",),
      repr(cui._syncplayClient.sendChat.call_args))
cui._syncplayClient.reset_mock()
cui.executeCommand("/admin S3cret")  # console path: leading slash kept, args preserved
check("client normalises a single slash + preserves args",
      cui._syncplayClient.sendChat.call_args[0] == ("/admin S3cret",), repr(cui._syncplayClient.sendChat.call_args))
cui._syncplayClient.reset_mock()
cui.executeCommand("t")  # known client command
check("known client command handled locally, not forwarded",
      cui._syncplayClient.toggleReady.called and not cui._syncplayClient.sendChat.called)
cui._syncplayClient.reset_mock()
cui.executeCommand("help")  # help stays local
check("help stays local, not forwarded", not cui._syncplayClient.sendChat.called)

# ---------- Ctrl+L room-lock: mpv marker -> client.toggleRoomLock -> /togglelock chat ----------
from syncplay.players.mpv import MpvPlayer
mpvL = MpvPlayer.__new__(MpvPlayer)
routed = []
mpvL.reactor = types.SimpleNamespace(callFromThread=lambda fn, *a: fn(*a))
mpvL._client = types.SimpleNamespace(toggleRoomLock=lambda: routed.append("lock"))
mpvL._listener = types.SimpleNamespace(sendLine=lambda l: None)
mpvL._handleUnknownLine("<SyncplayToggleLock></SyncplayToggleLock>")
check("mpv <SyncplayToggleLock> routes to client.toggleRoomLock", routed == ["lock"], repr(routed))
lockStub = types.SimpleNamespace()
lockSent = []
lockStub.sendChat = lambda m: lockSent.append(m)
SyncplayClient.toggleRoomLock(lockStub)
check("client.toggleRoomLock sends the /togglelock command",
      lockSent == [constants.TOGGLE_LOCK_COMMAND], repr(lockSent))

# ---------- client config plumbing ----------
from syncplay.ui.ConfigurationGetter import ConfigurationGetter as ClientCG
cg = ClientCG()
check("client config default adminPassword=None", cg._config.get("adminPassword", "MISSING") is None)
check("ini server_data includes adminPassword", "adminPassword" in cg._iniStructure["server_data"])
ns = types.SimpleNamespace(admin_password="clipw")
cg._overrideConfigWithArgs(ns)
check("CLI --admin-password maps to adminPassword", cg._config.get("adminPassword") == "clipw")

# ---------- server feature flag ----------
pf = make_factory(adminPassword="x")
pf.isolateRooms = False; pf.roomsDbFile = None; pf.disableChat = False; pf.disableReady = False
pf.maxUsernameLength = 16
feats = SyncFactory.getFeatures(pf)
check("featureList advertises serverAdmin=True when enabled", feats.get("serverAdmin") is True)
pf.adminPassword = None
check("featureList serverAdmin=False when disabled", SyncFactory.getFeatures(pf).get("serverAdmin") is False)

# ---------- locked rooms: pause/unpause in the player toggles readiness ----------
class FWL(FW):
    """FW that records the Set:roomLock messages it is sent."""
    def __init__(self, *a, **kw):
        FW.__init__(self, *a, **kw)
        self.roomLocks = []
    def sendRoomLock(self, roomName, locked, setBy=None):
        self.roomLocks.append((roomName, locked, setBy))

lf = make_factory(adminPassword="pw")
lf.roomsDbFile = None
lockAdmin = FWL("lockadm", admin=True, features={"roomLock": True})
modded = FWL("modded", features={"roomLock": True})
stock = FWL("stock")
lroom = Room("lockme", None)
for w in (lockAdmin, modded, stock):
    w._room = lroom
lroom._watchers = {w.getName(): w for w in (lockAdmin, modded, stock)}

lf.sendChat(lockAdmin, "/lock")
check("/lock: capable clients get Set:roomLock(locked)",
      modded.roomLocks == [("lockme", True, "lockadm")], repr(modded.roomLocks))
check("/lock: stock client gets chat only, no Set:roomLock",
      stock.roomLocks == [] and any("locked this room" in c for c in stock.chats), repr(stock.chats))
lf.sendChat(lockAdmin, "/unlock")
check("/unlock: capable clients get Set:roomLock(unlocked)",
      modded.roomLocks[-1] == ("lockme", False, "lockadm"), repr(modded.roomLocks))

lroom.setLocked(True)
modded.roomLocks.clear(); stock.roomLocks.clear()
lf.sendRoomStateToWatcher(modded)
check("join/room switch: current lock state pushed to a capable client",
      modded.roomLocks == [("lockme", True, None)], repr(modded.roomLocks))
lf.sendRoomStateToWatcher(stock)
check("join: nothing pushed to a client that cannot use it", stock.roomLocks == [])
lroom.setLocked(False)
modded.roomLocks.clear()
lf.sendRoomStateToWatcher(modded)
check("join into an unlocked room still says locked=False (clears a stale lock)",
      modded.roomLocks == [("lockme", False, None)], repr(modded.roomLocks))

# Server-side fallback: a stock client's rejected pause becomes a readiness toggle.
fbServer = mock.Mock(); fbServer.disableReady = False
fbConn = mock.Mock(); fbConn.isLogged.return_value = True; fbConn.getFeatures.return_value = {}
fbw = Watcher(fbServer, fbConn, "stocky")
if fbw._sendStateTimer and fbw._sendStateTimer.running: fbw._sendStateTimer.stop()
fbRoom = Room("fb", None); fbRoom._watchers = {"stocky": fbw}; fbw._room = fbRoom
fbw._positionEstablished = True

def roomPlaystate(paused):
    fbRoom.setLocked(False)  # set the room's own state without an admin watcher in the way
    fbRoom.setPaused(Room.STATE_PAUSED if paused else Room.STATE_PLAYING, user)
    fbRoom.setLocked(True)
    fbw._lockedPauseLatch = None

roomPlaystate(paused=True)
fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)   # "I pressed play while the room is paused"
check("locked + stock client: rejected unpause toggles readiness",
      fbServer.setReady.call_args is not None and fbServer.setReady.call_args[0] == (fbw, True),
      repr(fbServer.setReady.call_args))
check("locked + stock client: the pause itself is still not the room's",
      fbRoom.isPaused() is True)

fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)   # same rejected state repeated before the revert lands
check("repeat of the same rejected pause does not flip readiness again",
      fbServer.setReady.call_args is None, repr(fbServer.setReady.call_args))

fbw.updateState(None, True, False, 0)    # client accepted the revert: agrees with the room again
fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)
check("a fresh keypress after agreeing with the room toggles again",
      fbServer.setReady.call_args is not None, repr(fbServer.setReady.call_args))

roomPlaystate(paused=False)
fbServer.setReady.reset_mock()
fbw.updateState(None, True, False, 0)    # "I pressed pause while the room is playing"
check("locked + stock client: rejected pause toggles readiness too",
      fbServer.setReady.call_args is not None, repr(fbServer.setReady.call_args))

roomPlaystate(paused=True)
fbConn.getFeatures.return_value = {"roomLock": True}
fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)
check("client that tracks the lock itself gets no server-side toggle",
      fbServer.setReady.call_args is None, repr(fbServer.setReady.call_args))
fbConn.getFeatures.return_value = {}

roomPlaystate(paused=True)
fbw._positionEstablished = False
fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)
check("(re)joining watcher: no toggle until its position is established",
      fbServer.setReady.call_args is None, repr(fbServer.setReady.call_args))
fbw._positionEstablished = True

roomPlaystate(paused=True)
fbw._fileChangedAt = time.time()
fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)
check("file just loaded: player noise is not a keypress",
      fbServer.setReady.call_args is None, repr(fbServer.setReady.call_args))
fbw._fileChangedAt = None

fbRoom.setLocked(False)
fbRoom.setPaused(Room.STATE_PAUSED, user)
fbw._lockedPauseLatch = None
fbServer.setReady.reset_mock()
fbw.updateState(None, False, False, 0)
check("unlocked room: pause is a real pause, never a readiness toggle",
      fbServer.setReady.call_args is None and fbRoom.isPlaying(), repr(fbServer.setReady.call_args))
fbRoom.setLocked(True)

# Client: the lock reaches canControl, which is what re-routes the keypress.
from syncplay.client import SyncplayClient, SyncplayUser, SyncplayUserlist
lu = SyncplayUser("me", "r")
check("client user: unlocked plain room can be controlled", lu.canControl() is True)
lu.setRoomLocked(True)
check("client user: locked room cannot be controlled", lu.canControl() is False)
lu.setControllerStatus(True)
check("client user: an admin/operator still controls a locked room", lu.canControl() is True)

ull = SyncplayUserlist.__new__(SyncplayUserlist)
ull.currentUser = SyncplayUser("me", "r")
ull._users = {}
ull._roomUsersChanged = False
ull.ui = types.SimpleNamespace(userListChange=lambda: None, showMessage=lambda *a, **k: None)
ull._client = types.SimpleNamespace(autoplayCheck=lambda: None)
ull.addUser("alice", "r", None, noMessage=True)
ull.addUser("bob", "elsewhere", None, noMessage=True)
ull.setRoomLocked("r", True)
check("userlist: lock stamps only the users in that room",
      ull.currentUser.isRoomLocked() and ull._users["alice"].isRoomLocked()
      and not ull._users["bob"].isRoomLocked())
ull.addUser("carol", "r", None, noMessage=True)
check("userlist: someone joining a locked room is stamped", ull._users["carol"].isRoomLocked())
ull.modUser("carol", "elsewhere", None)
check("userlist: leaving a locked room clears the stamp", ull._users["carol"].isRoomLocked() is False)
ull.setRoomLocked("r", False)
check("userlist: unlock clears the stamps",
      not ull.currentUser.isRoomLocked() and not ull._users["alice"].isRoomLocked())
ull.setRoomLocked("r", True)
ull.clearRoomLocks()
check("userlist: reconnect clears every lock",
      not ull.isRoomLocked("r") and not ull.currentUser.isRoomLocked())

cprot = SyncClientProtocol.__new__(SyncClientProtocol)
routedLocks = []
cprot._client = types.SimpleNamespace(setRoomLocked=lambda *a: routedLocks.append(a))
cprot.handleSet({"roomLock": {"room": "r", "locked": True, "setBy": "adm"}})
check("client handleSet routes roomLock", routedLocks == [("r", True, "adm")], repr(routedLocks))
cprot.handleSet({"roomLock": {"room": "r"}})
check("client handleSet roomLock without 'locked' is safely False",
      routedLocks[-1] == ("r", False, None), repr(routedLocks))

# Client: _toggleReady in a locked room - both directions, and only for real keypresses.
def run_locked_toggle(globalPaused, playerPaused, ready=False, isController=False,
                      rewoundAt=None, connectedAt=None, fileAt=None, locked=True):
    # playerPaused is what the player has just reported - updatePlayerStatus has already stored it
    # and passes the same value in, so the two always agree on entry.
    c = SyncplayClient.__new__(SyncplayClient)
    cu = SyncplayUser("me", "r")
    cu.setReady(ready); cu.setRoomLocked(locked); cu.setControllerStatus(isController)
    c.userlist = types.SimpleNamespace(currentUser=cu)
    rec = []
    c._player = types.SimpleNamespace(setPaused=lambda v: rec.append(("player.setPaused", v)))
    c.ui = types.SimpleNamespace(showMessage=lambda m, *a, **k: rec.append(("msg", m)),
                                 showDebugMessage=lambda m: None)
    c.toggleReady = lambda manuallyInitiated=True: rec.append(("toggleReady", manuallyInitiated))
    c.changeReadyState = lambda s, manuallyInitiated=True: rec.append(("changeReadyState", s))
    c._config = {"unpauseAction": constants.UNPAUSE_ALWAYS_MODE}
    c.serverFeatures = {"readiness": True}
    c._globalPaused = globalPaused
    c._playerPaused = playerPaused
    c._lastPlayerUpdate = time.time()
    c._lastGlobalUpdate = time.time()
    c.lastRewindTime = rewoundAt
    c.lastUpdatedFileTime = fileAt
    c.lastConnectTime = connectedAt if connectedAt is not None else time.time() - 3600
    c.lastAdvanceTime = None
    c.lastPausedOnLeaveTime = None
    out = c._toggleReady(True, playerPaused)
    return rec, out, c

# room paused, user presses play: the point of the whole feature
rec, out, c = run_locked_toggle(globalPaused=True, playerPaused=False)
check("locked room: play while paused toggles readiness",
      ("toggleReady", True) in rec, repr(rec))
check("locked room: the player is put back to the room's state",
      ("player.setPaused", True) in rec and c._playerPaused is True, repr(rec))
check("locked room: the pause change is never sent on", out is False)
check("locked room: not-ready user is told they are now ready",
      ("msg", M.messages["en"]["set-as-ready-notification"]) in rec, repr(rec))

# room playing, user presses pause
rec, out, c = run_locked_toggle(globalPaused=False, playerPaused=True, ready=True)
check("locked room: pause while playing toggles readiness",
      ("toggleReady", True) in rec and c._playerPaused is False, repr(rec))
check("locked room: ready user is told they are now not ready",
      ("msg", M.messages["en"]["set-as-not-ready-notification"]) in rec, repr(rec))

# noise: nothing the user did
for label, kwargs in (("just rewound", {"rewoundAt": time.time()}),
                      ("just connected", {"connectedAt": time.time()}),
                      ("file just loaded", {"fileAt": time.time()})):
    rec, out, c = run_locked_toggle(globalPaused=True, playerPaused=False, **kwargs)
    check("locked room: {} is player noise, not a keypress".format(label),
          not any(e[0] == "toggleReady" for e in rec) and not any(e[0] == "msg" for e in rec), repr(rec))
    check("locked room: {} still snaps the player back".format(label),
          ("player.setPaused", True) in rec, repr(rec))

# an admin in a locked room keeps ordinary control (no readiness re-routing)
rec, out, c = run_locked_toggle(globalPaused=True, playerPaused=False, isController=True)
check("locked room: an admin takes the ordinary control path, not the readiness one",
      not any(e[0] == "toggleReady" for e in rec) and not any(e[0] == "player.setPaused" for e in rec),
      repr(rec))

# and an unlocked room is untouched by any of this
rec, out, c = run_locked_toggle(globalPaused=False, playerPaused=True, locked=False)
check("unlocked room: upstream readiness handling is unchanged",
      not any(e[0] == "toggleReady" for e in rec) and ("changeReadyState", False) in rec, repr(rec))

# ---------- i18n ----------
keys = ["server-admin-password-argument", "client-admin-password-argument",
        "admin-login-success-chat-message", "admin-login-fail-chat-message",
        "admin-not-enabled-chat-message", "admin-unauthorised-chat-message",
        "room-locked-chat-message", "room-unlocked-chat-message",
        "room-already-managed-chat-message", "admin-password-label", "adminpassword-tooltip"]
for k in keys:
    check("en key: " + k, k in M.messages["en"])
bad = [l for l in M.getMissingStrings().splitlines() if "Unused" in l and ("admin" in l.lower() or "room-lock" in l.lower())]
check("no admin keys leaked to non-English dicts", not bad, repr(bad))

# ---------- GUI structural + offscreen construction ----------
src = open(os.path.join(REPO_ROOT, "syncplay", "ui", "GuiConfiguration.py")).read()
check("GUI: adminpass widgets created", "self.adminpassTextbox = QLineEdit(self)" in src)
check("GUI: masked input", "self.adminpassTextbox.setEchoMode(QLineEdit.Password)" in src)
check("GUI: manual-save marker objectName", 'constants.LOAD_SAVE_MANUALLY_MARKER + "adminPassword"' in src)
check("GUI: manual save line", "self.config['adminPassword'] = self.adminpassTextbox.text()" in src)
check("GUI: grid row present", "addWidget(self.adminpassTextbox, 2, 1)" in src)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
gui_ok, gui_detail = False, ""
try:
    from syncplay.vendor.Qt import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])
    from syncplay.ui.GuiConfiguration import ConfigDialog
    cg2 = ClientCG()
    cfg = dict(cg2._config)
    cfg.update({"adminPassword": "guiPW", "debug": False, "host": "localhost:8999", "name": "t",
                "room": "r", "password": None, "playerPath": "", "playerArgs": [],
                "publicServers": []})
    dlg = ConfigDialog(cfg, [], None, dict(cfg))
    gui_ok = dlg.adminpassTextbox.text() == "guiPW" and dlg.adminpassTextbox.echoMode() == QtWidgets.QLineEdit.Password
    gui_ok = gui_ok and hasattr(dlg, "receiveServerTrustedDomainsCheckbox") \
        and dlg.receiveServerTrustedDomainsCheckbox.objectName() == "receiveServerTrustedDomains"
    gui_detail = "field text + echo mode verified live"
    # live save round-trip
    dlg.adminpassTextbox.setText("changedPW")
    dlg.config['adminPassword'] = dlg.adminpassTextbox.text()
    gui_ok = gui_ok and dlg.config['adminPassword'] == "changedPW"
except Exception as e:
    gui_detail = "offscreen construction failed: {}: {}".format(type(e).__name__, e)
check("GUI: offscreen dialog constructs with working admin field", gui_ok, gui_detail)

fails = [x for x in RESULTS if not x[1]]
print("\n===== ADMIN SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
