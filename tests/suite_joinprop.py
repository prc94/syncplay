"""Unit suite for join-time propagation of room state (trusted domains + track proposals).

Regression cover for the "new joiners sometimes don't get the domains/tracks" bug, which had two
independent causes:
  1. the server pushed Set:trustedDomains during addWatcher, i.e. *before* its Hello, and the
     client wiped its session-only copy while handling that Hello;
  2. a track proposal arriving before the player finished starting was dropped on the floor.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy

from twisted.internet import defer
from syncplay import constants
from syncplay.server import Room, SyncFactory
from syncplay.client import SyncplayClient, UiManager
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] JoinProp :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


# ---------------- client: the Hello no longer wipes join-time domains ----------------
class FakeUI:
    def __init__(self):
        self.messages = []
    def showMessage(self, m, *a, **k): self.messages.append(m)
    def showDebugMessage(self, *a, **k): pass
    def showErrorMessage(self, *a, **k): pass
    def setFeatures(self, *a, **k): pass

def makeClient(ownDomains=None, receive=True):
    c = SyncplayClient.__new__(SyncplayClient)
    c._serverTrustedDomains = []
    c._player = None
    c._protocol = None
    c._SyncplayClient__playerReady = defer.Deferred()
    c._config = {"receiveServerTrustedDomains": receive,
                 "trustedDomains": list(ownDomains or ["mine.example"])}
    c.serverVersion = "1.7.6"
    c.ui = FakeUI()
    c.fileSwitchFoundFiles = lambda: None
    c._autoAuthAdmin = lambda: None
    return c

# The exact on-the-wire order a joiner sees now: Hello first, then the room state.
c = makeClient()
c.setServerVersion("1.7.6", {"chat": True, "featureList": True})
c.setServerTrustedDomains({"domains": ["example.com", "cdn.test"], "by": "adm"})
check("domains delivered after Hello survive", c._serverTrustedDomains == ["example.com", "cdn.test"],
      repr(c._serverTrustedDomains))
check("effective list merges own + server domains",
      c.effectiveTrustedDomains() == ["mine.example", "example.com", "cdn.test"],
      repr(c.effectiveTrustedDomains()))

# The old (buggy) order must ALSO survive now, so an older server that still sends the Set before
# its Hello keeps working - checkForFeatureSupport must not clear the overlay.
c = makeClient()
c.setServerTrustedDomains({"domains": ["example.com"], "by": "adm"})
c.setServerVersion("1.7.6", {"chat": True, "featureList": True})
check("domains delivered before Hello are NOT wiped by it (the regression)",
      c._serverTrustedDomains == ["example.com"], repr(c._serverTrustedDomains))

# initProtocol owns the reset instead, so a reconnect still starts clean.
c = makeClient()
c.setServerTrustedDomains({"domains": ["stale.example"], "by": "adm"})
c.initProtocol(object())
check("initProtocol drops the previous connection's domains", c._serverTrustedDomains == [],
      repr(c._serverTrustedDomains))
check("initProtocol still records the protocol", c._protocol is not None)

# destroyProtocol keeps clearing too (belt and braces; nothing carries across a disconnect).
c = makeClient()
c.setServerTrustedDomains({"domains": ["a.example"], "by": "adm"})
c._protocol = None
c.destroyProtocol()
check("destroyProtocol clears the overlay", c._serverTrustedDomains == [])

# Opt-out still respected.
c = makeClient(receive=False)
c.setServerVersion("1.7.6", {"chat": True, "featureList": True})
c.setServerTrustedDomains({"domains": ["example.com"], "by": "adm"})
check("opt-out keeps raw list but excludes it from effective",
      c._serverTrustedDomains == ["example.com"] and c.effectiveTrustedDomains() == ["mine.example"],
      repr(c.effectiveTrustedDomains()))


# ---------------- client: track proposals queue until the player is ready ----------------
class FakePlayer:
    chatOSDSupported = True
    def __init__(self):
        self.proposals = []
    def setTrackProposal(self, p): self.proposals.append(p)
    def displayChatMessage(self, u, m): pass

def makeUi(withPlayer=False):
    c = makeClient()
    c.autoplayConditionsMet = lambda: False
    c.autoplayTimerIsRunning = lambda: False
    c._config["chatOutputEnabled"] = True
    ui = UiManager(c, FakeUI())
    c.ui = ui
    if withPlayer:
        c._player = FakePlayer()
    return c, ui

PROPOSAL = {"audioId": 2, "subId": "no", "by": "adm", "signature": "audio:2:jpn"}

# Player already up: straight through, nothing queued.
c, ui = makeUi(withPlayer=True)
ui.setTrackProposal(dict(PROPOSAL))
check("proposal goes straight to a ready player", len(c._player.proposals) == 1 and not ui._pendingTrackProposals)

# Player not up yet: queued, then flushed on player-ready.
c, ui = makeUi()
ui.setTrackProposal(dict(PROPOSAL))
check("proposal queued while the player is starting", len(ui._pendingTrackProposals) == 1)
check("queued proposal is still logged for the user",
      any("adm" in m for m in ui._UiManager__ui.messages), repr(ui._UiManager__ui.messages))
player = FakePlayer()
c._player = player
ui._flushTrackProposals()
check("queued proposal reaches the player once it is up",
      [p["audioId"] for p in player.proposals] == [2], repr(player.proposals))
check("queue emptied after flush", ui._pendingTrackProposals == [] and not ui._pendingTrackProposalsArmed)

# Several proposals queue in order and all arrive.
c, ui = makeUi()
for i in (1, 2, 3):
    ui.setTrackProposal({"audioId": i, "by": "adm", "signature": "sig{}".format(i)})
c._player = FakePlayer()
ui._flushTrackProposals()
check("all queued proposals flush in arrival order",
      [p["audioId"] for p in c._player.proposals] == [1, 2, 3], repr(c._player.proposals))

# The queue is bounded like the server-side cache.
c, ui = makeUi()
for i in range(constants.TRACK_CACHE_MAX_ENTRIES + 5):
    ui.setTrackProposal({"audioId": 1, "by": "adm", "signature": "sig{}".format(i)})
check("pending queue bounded by TRACK_CACHE_MAX_ENTRIES",
      len(ui._pendingTrackProposals) == constants.TRACK_CACHE_MAX_ENTRIES, str(len(ui._pendingTrackProposals)))
check("bounded queue keeps the newest layouts",
      ui._pendingTrackProposals[-1]["signature"] == "sig{}".format(constants.TRACK_CACHE_MAX_ENTRIES + 4))

# Flush with the player still absent must not explode and must not lose the armed flag silently.
c, ui = makeUi()
ui.setTrackProposal(dict(PROPOSAL))
ui._flushTrackProposals()
check("flush without a player is a no-op, not a crash", ui._pendingTrackProposals == [])

# Only one player-ready callback is registered no matter how many proposals queue.
c, ui = makeUi()
registered = []
c.addPlayerReadyCallback = lambda cb: registered.append(cb)
for i in range(4):
    ui.setTrackProposal({"audioId": 1, "by": "adm", "signature": "s{}".format(i)})
check("player-ready callback armed exactly once", len(registered) == 1, str(len(registered)))

# Non-dict payloads are still rejected outright.
c, ui = makeUi()
ui.setTrackProposal("garbage")
check("non-dict proposal ignored", ui._pendingTrackProposals == [])


# ---------------- client: join-time chat must not crash without a player ----------------
c, ui = makeUi()  # _player is None
crashed = None
try:
    ui.showChatMessage("adm", "adm shared 2 trusted domain(s): example.com")
except Exception as e:
    crashed = e
check("showChatMessage survives a None player (join-time fallback chat)", crashed is None, repr(crashed))
check("chat still reaches the log with no player",
      any("adm shared" in m for m in ui._UiManager__ui.messages), repr(ui._UiManager__ui.messages))

c, ui = makeUi(withPlayer=True)
ui.showChatMessage("adm", "hello there")
check("showChatMessage still routes to a ready player's OSD",
      any("hello there" in m for m in ui._UiManager__ui.messages))


# ---------------- server: room state is deferred on join, immediate on room switch ----------------
class FW:
    def __init__(self, name, features=None, file_=None):
        self._name, self._features, self._file = name, features or {}, file_
        self.chats, self.domains, self.proposals = [], [], []
        self._room = None
        self._lastTrackProposalAnnouncedFile = None
    def getName(self): return self._name
    def isAdmin(self): return False
    def isController(self): return False
    def isAfk(self): return False
    def getPosition(self): return 0.0
    def isPositionEstablished(self): return True  # settled watcher; see suite_joinguard.py
    def getRoom(self): return self._room
    def getFile(self): return self._file
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def sendChatMessage(self, m, skipIfSupportsFeature=None): self.chats.append(m["message"])
    def sendTrustedDomains(self, p): self.domains.append(p)
    def sendTrackProposal(self, p): self.proposals.append(p)

def makeFactory():
    f = SyncFactory.__new__(SyncFactory)
    f._trackCache = {}
    f._domainCache = {}
    f.maxChatMessageLength = 200
    return f

CAPABLE = {"trackProposals": True, "trustedDomains": True}

# Capable watcher gets the whole cache.
f = makeFactory()
room = Room("r", None)
f._trackCache["r"] = {"sigA": {"audioId": 1, "signature": "sigA", "by": "adm"},
                      "sigB": {"audioId": 2, "signature": "sigB", "by": "adm"}}
room.setTrackProposal({"audioId": 2, "signature": "sigB", "by": "adm"})
room.setTrustedDomains({"domains": ["example.com"], "by": "adm"})
w = FW("cap", CAPABLE); w._room = room
f.sendRoomStateToWatcher(w)
check("capable watcher gets every cached layout", len(w.proposals) == 2, repr(w.proposals))
check("capable watcher gets the domains", w.domains == [{"domains": ["example.com"], "by": "adm"}])
check("capable watcher gets no fallback chat", w.chats == [])

# An uncached (signature-less) latest proposal must still be delivered alongside the cache.
f = makeFactory()
room = Room("r", None)
f._trackCache["r"] = {"sigA": {"audioId": 1, "signature": "sigA", "by": "adm"}}
room.setTrackProposal({"audioId": 9, "by": "adm"})  # no signature -> never cached
w = FW("cap", CAPABLE); w._room = room
f.sendRoomStateToWatcher(w)
check("uncached latest proposal is sent too (not masked by the cache)",
      sorted(p["audioId"] for p in w.proposals) == [1, 9], repr(w.proposals))

# ...and is not duplicated when it IS the cached one.
f = makeFactory()
room = Room("r", None)
latest = {"audioId": 1, "signature": "sigA", "by": "adm"}
f._trackCache["r"] = {"sigA": latest}
room.setTrackProposal(latest)
w = FW("cap", CAPABLE); w._room = room
f.sendRoomStateToWatcher(w)
check("cached latest proposal is not sent twice", len(w.proposals) == 1, repr(w.proposals))

# Fallback watcher gets chat for the latest only.
f = makeFactory()
room = Room("r", None)
room.setTrackProposal({"audioId": 2, "audioName": "jpn", "signature": "sigB", "by": "adm"})
room.setTrustedDomains({"domains": ["example.com"], "by": "adm"})
w = FW("fb", {}, file_={"name": "ep1.mkv"}); w._room = room
f.sendRoomStateToWatcher(w)
check("fallback watcher gets no Set proposals", w.proposals == [])
check("fallback watcher gets chat for tracks and domains", len(w.chats) == 2, repr(w.chats))

# Nothing published -> nothing sent.
f = makeFactory()
room = Room("r", None)
w = FW("cap", CAPABLE); w._room = room
f.sendRoomStateToWatcher(w)
check("empty room state sends nothing", not w.proposals and not w.domains and not w.chats)

# No room (watcher already gone) -> no crash.
f = makeFactory()
w = FW("cap", CAPABLE)
f.sendRoomStateToWatcher(w)
check("roomless watcher handled without crashing", not w.proposals and not w.domains)


# ---------------- server: room state that outlives the room ----------------
# Both per-room caches deliberately OUTLIVE the room (docs/server-admins.md): remembering an
# admin's layouts and trusted domains across sessions is the point; only a restart clears them.
# The room's "latest proposal" pointer is session state and is dropped.
f = makeFactory()
f._trackCache["r"] = {"sigA": {"audioId": 1, "signature": "sigA", "by": "adm"}}
f._domainCache["r"] = {"domains": ["example.com"], "by": "adm"}
room = Room("r", None)
room.setTrackProposal({"audioId": 1, "signature": "sigA", "by": "adm"})
room.setTrackProposal(None)  # mimic removeWatcher's empty-room cleanup block
check("emptied room drops its latest proposal", room.getTrackProposal() is None)
check("emptied room KEEPS its cached layouts", len(f._cachedTrackProposals("r")) == 1,
      repr(f._cachedTrackProposals("r")))
check("emptied room KEEPS its cached domains", f._domainCache.get("r") is not None)

# An ordinary Room object is destroyed once empty, so the next session gets a brand new one -
# setWatcherRoom is what restores the domains onto it from the cache.
fresh = Room("r", None)
check("recreated room starts with no domains of its own", fresh.getTrustedDomains() is None)
if fresh.getTrustedDomains() is None and "r" in f._domainCache:
    fresh.setTrustedDomains(f._domainCache["r"])
w = FW("newcomer", CAPABLE); w._room = fresh
f.sendRoomStateToWatcher(w)
check("next session's joiner still gets the remembered layouts", len(w.proposals) == 1, repr(w.proposals))
check("next session's joiner still gets the remembered domains",
      w.domains == [{"domains": ["example.com"], "by": "adm"}], repr(w.domains))

# A room nobody ever published for stays empty-handed.
f2 = makeFactory()
other = Room("never", None)
w2 = FW("nobody", CAPABLE); w2._room = other
f2.sendRoomStateToWatcher(w2)
check("unpublished room hands out nothing", not w2.proposals and not w2.domains)


# ---------------- source-level guards ----------------
serverSrc = open(os.path.join(REPO_ROOT, "syncplay", "server.py")).read()
protoSrc = open(os.path.join(REPO_ROOT, "syncplay", "protocols.py")).read()
clientSrc = open(os.path.join(REPO_ROOT, "syncplay", "client.py")).read()

check("handleHello sends room state after sendHello",
      protoSrc.index("self.sendHello(version)") < protoSrc.index("sendRoomStateToWatcher"))
check("setWatcherRoom no longer pushes room state on join",
      "if not asJoin:" in serverSrc and "self.sendRoomStateToWatcher(watcher)" in serverSrc)
featureSupportBody = clientSrc[clientSrc.index("def checkForFeatureSupport"):]
featureSupportBody = featureSupportBody[:featureSupportBody.index("\n    def ", 1)]
check("checkForFeatureSupport does not reset the domain overlay",
      "_serverTrustedDomains = []" not in featureSupportBody)
check("initProtocol resets the domain overlay instead",
      "_serverTrustedDomains = []" in clientSrc[clientSrc.index("def initProtocol"):clientSrc.index("def destroyProtocol")])
check("removeWatcher evicts neither cache",
      "self._trackCache.pop(" not in serverSrc and "self._domainCache.pop(" not in serverSrc)
check("removeWatcher no longer clears the room's own domains",
      "room.setTrustedDomains(None)" not in serverSrc)
check("setWatcherRoom restores domains from the cache",
      "room.setTrustedDomains(self._domainCache[roomName])" in serverSrc)
check("publishing records the domains in the cache",
      "self._domainCache[room.getName()] = proposal" in serverSrc)

fails = [x for x in RESULTS if not x[1]]
print("\n===== JOINPROP SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
