"""Unit suite for admin-published trusted domains."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy

import types
import unittest.mock as mock
from syncplay import constants
from syncplay.utils import meetsMinVersion
from syncplay.server import Room, SyncFactory
from syncplay.protocols import SyncServerProtocol, SyncClientProtocol
from syncplay.client import SyncplayClient
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Domains :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

# ---------------- server side ----------------
class FW:
    def __init__(self, name, admin=False, controller=False, version="1.7.6", features=None):
        self._name, self._admin, self._controller = name, admin, controller
        self._version, self._features = version, features or {}
        self.chats, self.domains = [], []
        self._room = None
    def getName(self): return self._name
    def isAdmin(self): return self._admin
    def isController(self): return self._admin or self._controller
    def isAfk(self): return False
    def getRoom(self): return self._room
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def sendChatMessage(self, m, skipIfSupportsFeature=None):
        if meetsMinVersion(self._version, constants.CHAT_MIN_VERSION):
            self.chats.append(m["message"])
    def sendTrustedDomains(self, p): self.domains.append(p)

f = SyncFactory.__new__(SyncFactory)
f.maxChatMessageLength = 150
room = Room("r", None)
adm = FW("adm", admin=True, features={"trustedDomains": True}); adm._room = room
cap = FW("cap", features={"trustedDomains": True}); cap._room = room
legacy = FW("legacy", version="1.6.0"); legacy._room = room
pre = FW("pre", version="1.4.0"); pre._room = room
room._watchers = {"adm": adm, "cap": cap, "legacy": legacy, "pre": pre}

# authorization
f.setTrustedDomains(cap, {"domains": ["vimeo.com"]})
check("non-admin/non-controller publish: private error, nothing stored",
      cap.chats == ["Only server admins or room controllers can publish trusted domains."] and room.getTrustedDomains() is None)
cap.chats.clear()

# a room controller (not a server admin) is authorised too - controllers are equated to admins here
ctrl = FW("ctrl", controller=True, features={"trustedDomains": True}); ctrl._room = room
room._watchers["ctrl"] = ctrl
f.setTrustedDomains(ctrl, {"domains": ["ctrl.example"]})
check("room controller publish: authorised + stored",
      room.getTrustedDomains() is not None and room.getTrustedDomains()["by"] == "ctrl"
      and "Only server admins" not in "".join(ctrl.chats), repr((room.getTrustedDomains(), ctrl.chats)))
room.setTrustedDomains(None)
del room._watchers["ctrl"]
for w in room.getWatchers(): w.chats.clear(); w.domains.clear()

# publish + routing + normalization
f.setTrustedDomains(adm, {"domains": ["Vimeo.com", " CDN.example ", "vimeo.com", "x" * 400, ""]})
stored = room.getTrustedDomains()
check("stored + attributed", stored is not None and stored["by"] == "adm", repr(stored))
check("normalized: lowercase, trimmed, deduped, capped, empty dropped",
      stored["domains"] == ["vimeo.com", "cdn.example", "x" * constants.TRUSTED_DOMAINS_MAX_LENGTH], repr(stored["domains"]))
check("capable watcher got Set", len(cap.domains) == 1 and cap.domains[0]["domains"] == stored["domains"] and cap.domains == cap.domains)
check("capable watcher got NO chat", cap.chats == [])
check("legacy got informational chat", len(legacy.chats) == 1 and "shared trusted domains" in legacy.chats[0]
      and "vimeo.com" in legacy.chats[0], repr(legacy.chats))
check("pre-1.5.0 got nothing", pre.chats == [] and pre.domains == [])
check("publisher got count ack", adm.chats == ["Published 3 trusted domain(s) to the room."], repr(adm.chats))
for w in room.getWatchers(): w.chats.clear(); w.domains.clear()

# validation guards
room.setTrustedDomains(None)
f.setTrustedDomains(adm, {"domains": "notalist"})
check("non-list domains ignored", room.getTrustedDomains() is None)
f.setTrustedDomains(adm, "garbage")
check("non-dict payload safe", room.getTrustedDomains() is None)
f.setTrustedDomains(adm, {"domains": ["", "  ", 123, None]})
check("all-empty/invalid entries -> empty notice, nothing stored",
      room.getTrustedDomains() is None and adm.chats == ["Your trusted domains list is empty - nothing to publish."], repr(adm.chats))
adm.chats.clear()
big = ["d{}.example".format(i) for i in range(constants.TRUSTED_DOMAINS_MAX_COUNT + 25)]
f.setTrustedDomains(adm, {"domains": big})
check("count capped", len(room.getTrustedDomains()["domains"]) == constants.TRUSTED_DOMAINS_MAX_COUNT)
for w in room.getWatchers(): w.chats.clear(); w.domains.clear()

# late-join delivery
lateCap = FW("lateCap", features={"trustedDomains": True})
lateLegacy = FW("lateLegacy", version="1.6.0")
f._sendTrustedDomainsToWatcher(lateCap, room.getTrustedDomains())
f._sendTrustedDomainsToWatcher(lateLegacy, room.getTrustedDomains())
check("late joiner capable: Set", len(lateCap.domains) == 1 and lateCap.chats == [])
check("late joiner legacy: chat", len(lateLegacy.chats) == 1 and lateLegacy.domains == [])

# room-empty cleanup semantics
room.setTrustedDomains(None)
check("cleanup clears domains", room.getTrustedDomains() is None)

# /domains legacy notice + dispatcher fall-through
f.adminPassword = "x"
class RM:
    def broadcastRoom(self, sender, l):
        for w in sender.getRoom().getWatchers(): l(w)
f._roomManager = RM()
r2 = Room("r2", None)
leg = FW("leg", version="1.6.0"); leg._room = r2
peer = FW("peer", version="1.6.0"); peer._room = r2
r2._watchers = {"leg": leg, "peer": peer}
f.sendChat(leg, "/domains")
check("/domains from legacy: private notice only",
      leg.chats == ["Trusted domains are published by server admins from an updated client (Ctrl+D in mpv, or type /domains there)."]
      and peer.chats == [], repr(leg.chats))
leg.chats.clear()
f.sendChat(leg, "/domainsfoo bar")
check("/domainsfoo (unknown): private warning, not broadcast",
      peer.chats == [] and len(leg.chats) == 1 and "/domainsfoo" in leg.chats[0], repr(leg.chats))

# server handleSet dispatch
sp = SyncServerProtocol.__new__(SyncServerProtocol)
sp._factory = mock.Mock(); sp._watcher = adm; sp._logged = True
sp.handleSet({"trustedDomains": {"domains": ["a.example"]}})
check("server handleSet dispatches trustedDomains", sp._factory.setTrustedDomains.call_args[0] == (adm, {"domains": ["a.example"]}))

# ---------------- client side ----------------
def client_stub(user_domains, server_domains, opt_in=True, only_trusted=True):
    stub = types.SimpleNamespace()
    stub._config = {"trustedDomains": list(user_domains), "receiveServerTrustedDomains": opt_in,
                    "onlySwitchToTrustedDomains": only_trusted}
    stub._serverTrustedDomains = list(server_domains)
    return stub

# effectiveTrustedDomains: union / order / dedupe / opt-out
s = client_stub(["youtube.com", "youtu.be"], ["vimeo.com", "youtube.com", "cdn.example"])
check("effective: order-preserving union, deduped",
      SyncplayClient.effectiveTrustedDomains(s) == ["youtube.com", "youtu.be", "vimeo.com", "cdn.example"],
      repr(SyncplayClient.effectiveTrustedDomains(s)))
s2 = client_stub(["youtube.com"], ["vimeo.com"], opt_in=False)
check("effective: opt-out -> user list only", SyncplayClient.effectiveTrustedDomains(s2) == ["youtube.com"])
s3 = client_stub([], ["vimeo.com"])
check("effective: empty user list + accepted server", SyncplayClient.effectiveTrustedDomains(s3) == ["vimeo.com"])

# _isURITrustableAndTrusted end to end
def trust(stub, uri):
    stub.effectiveTrustedDomains = lambda: SyncplayClient.effectiveTrustedDomains(stub)
    return SyncplayClient._isURITrustableAndTrusted(stub, uri)
s = client_stub(["youtube.com"], ["vimeo.com"])
check("server-only domain trusted when accepted", trust(s, "https://vimeo.com/123") == (True, True))
s = client_stub(["youtube.com"], ["vimeo.com"], opt_in=False)
check("server-only domain NOT trusted when opted out", trust(s, "https://vimeo.com/123") == (True, False))
s = client_stub(["youtube.com"], [])
check("domain not sent -> not trusted", trust(s, "https://vimeo.com/123") == (True, False))
s = client_stub(["youtube.com"], ["vimeo.com"])
check("non-http scheme never trustable", trust(s, "ftp://vimeo.com/x") == (False, False))
s = client_stub(["youtube.com"], ["vimeo.com"], only_trusted=False)
check("only-trusted off -> all trustable trusted", trust(s, "https://anything.example/x") == (True, True))
s = client_stub(["youtube.com"], ["cdn.example"])
check("user's own domain still trusted alongside server ones", trust(s, "https://www.youtube.com/watch") == (True, True))

# setServerTrustedDomains: store + notify + opt-out
def recv_stub(opt_in=True):
    stub = types.SimpleNamespace()
    stub._config = {"receiveServerTrustedDomains": opt_in, "trustedDomains": []}
    stub._serverTrustedDomains = []
    stub.switched = []
    stub.msgs = []
    stub.fileSwitchFoundFiles = lambda: stub.switched.append(1)
    stub.ui = types.SimpleNamespace(showMessage=lambda m: stub.msgs.append(m))
    return stub
r = recv_stub()
SyncplayClient.setServerTrustedDomains(r, {"domains": ["Vimeo.com", "vimeo.com", " cdn.example "], "by": "adm"})
check("recv: stored normalized + deduped", r._serverTrustedDomains == ["vimeo.com", "cdn.example"], repr(r._serverTrustedDomains))
check("recv: re-evaluated file switch + notified", r.switched == [1] and len(r.msgs) == 1 and "cdn.example" not in r.msgs[0]
      and "2" in r.msgs[0], repr(r.msgs))
r = recv_stub(opt_in=False)
SyncplayClient.setServerTrustedDomains(r, {"domains": ["vimeo.com"], "by": "adm"})
check("recv opted-out: stored but not applied/notified", r._serverTrustedDomains == ["vimeo.com"] and r.switched == [] and r.msgs == [])
r = recv_stub()
SyncplayClient.setServerTrustedDomains(r, {"domains": "junk"})
SyncplayClient.setServerTrustedDomains(r, "junk")
SyncplayClient.setServerTrustedDomains(r, {"domains": []})
check("recv malformed/empty: safe no-ops", r._serverTrustedDomains in ([], ["vimeo.com"]) and r.switched == [], repr(r._serverTrustedDomains))

# publishTrustedDomains guards + payload
sent = []
p = types.SimpleNamespace(_protocol=types.SimpleNamespace(logged=True, sendTrustedDomains=lambda x: sent.append(x)),
                          _config={"trustedDomains": ["a.example", "b.example"]},
                          getUsername=lambda: "adm")
SyncplayClient.publishTrustedDomains(p)
check("publish: payload from own config + username", sent == [{"domains": ["a.example", "b.example"], "by": "adm"}], repr(sent))
p.getUsername = lambda: "adm"; p._config = {"trustedDomains": None}
sent.clear(); SyncplayClient.publishTrustedDomains(p)
check("publish: empty config -> empty list", sent == [{"domains": [], "by": "adm"}])
p._protocol = None; sent.clear(); SyncplayClient.publishTrustedDomains(p)
p._protocol = types.SimpleNamespace(logged=False, sendTrustedDomains=lambda x: sent.append(x))
SyncplayClient.publishTrustedDomains(p)
check("publish: guarded when no protocol / not logged", sent == [])

# client handleSet dispatch
cp = SyncClientProtocol.__new__(SyncClientProtocol)
got = []
cp._client = mock.Mock(); cp._client.setServerTrustedDomains = lambda v: got.append(v)
cp.handleSet({"trustedDomains": {"domains": ["x.example"]}})
check("client handleSet dispatches trustedDomains", got == [{"domains": ["x.example"]}])

# client protocol sender
cp2 = SyncClientProtocol.__new__(SyncClientProtocol)
out = []
cp2.sendMessage = lambda m: out.append(m)
cp2.sendTrustedDomains({"domains": ["y.example"]})
check("client sendTrustedDomains payload", out == [{"Set": {"trustedDomains": {"domains": ["y.example"]}}}])

# mpv marker parse -> publish
from syncplay.players.mpv import MpvPlayer
mpv = MpvPlayer.__new__(MpvPlayer)
published = []
mpv.reactor = types.SimpleNamespace(callFromThread=lambda fn, *a: fn(*a))
mpv._client = types.SimpleNamespace(publishTrustedDomains=lambda: published.append(1))
mpv._listener = types.SimpleNamespace()
mpv.mpvErrorCheck = lambda line: None
mpv._handleUnknownLine("<SyncplayPublishDomains>")
check("mpv marker triggers publish", published == [1])

# consoleUI command dispatch
from syncplay.ui.consoleUI import ConsoleUI
con = ConsoleUI.__new__(ConsoleUI)
ccalls = []
con._syncplayClient = types.SimpleNamespace(publishTrustedDomains=lambda: ccalls.append(1))
con.executeCommand("domains")
con.executeCommand("trustdomains")
check("consoleUI /domains + /trustdomains dispatch", ccalls == [1, 1])

# setTrustedDomains: auto-share-on-update session flag
def makeClient(flag, controller, trusted=None):
    c = SyncplayClient.__new__(SyncplayClient)
    c._config = {"trustedDomains": trusted if trusted is not None else ["old.example"]}
    c._shareTrustedDomainsOnUpdate = flag
    c.userlist = types.SimpleNamespace(currentUser=types.SimpleNamespace(isController=lambda: controller))
    c.fileSwitchFoundFiles = lambda: None
    c.ui = types.SimpleNamespace(showMessage=lambda *a, **k: None)
    pub = []
    c.publishTrustedDomains = lambda: pub.append(1)
    return c, pub

with mock.patch("syncplay.ui.ConfigurationGetter.ConfigurationGetter") as CG:
    CG.return_value.setConfigOption = lambda *a, **k: None
    # flag on + controller: publishes on a changed list
    c, pub = makeClient(True, True); SyncplayClient.setTrustedDomains(c, ["new.example"])
    check("update: flag on + admin, changed list -> publish", pub == [1])
    # flag on + controller: publishes even when the list is unchanged ("share now")
    c, pub = makeClient(True, True, trusted=["same.example"]); SyncplayClient.setTrustedDomains(c, ["same.example"])
    check("update: flag on + admin, unchanged list -> still publish", pub == [1])
    # flag off: never auto-publishes
    c, pub = makeClient(False, True); SyncplayClient.setTrustedDomains(c, ["new.example"])
    check("update: flag off -> no publish", pub == [])
    # flag on but not controller: guarded (server would reject)
    c, pub = makeClient(True, False); SyncplayClient.setTrustedDomains(c, ["new.example"])
    check("update: flag on but not admin -> no publish", pub == [])

# session accessors default off + round-trip
acc = SyncplayClient.__new__(SyncplayClient); acc._shareTrustedDomainsOnUpdate = False
check("accessor: default off", SyncplayClient.getShareTrustedDomainsOnUpdate(acc) is False)
SyncplayClient.setShareTrustedDomainsOnUpdate(acc, True)
check("accessor: set/get round-trip", SyncplayClient.getShareTrustedDomainsOnUpdate(acc) is True)

# ---------------- i18n ----------------
keys = ["domains-unauthorised-chat-message", "domains-published-chat-message", "domains-empty-chat-message",
        "domains-shared-chat-message", "domains-command-notice-chat-message", "server-trusted-domains-notification",
        "receiveservertrusteddomains-label", "receiveservertrusteddomains-tooltip",
        "sharetrusteddomains-checkbox-label", "sharetrusteddomains-checkbox-tooltip"]
for k in keys:
    check("en key: " + k, k in M.messages["en"])
bad = [l for l in M.getMissingStrings().splitlines() if "Unused" in l and ("domain" in l.lower() or "receiveserver" in l.lower())]
check("no domain keys leaked to non-English dicts", not bad, repr(bad))

fails = [x for x in RESULTS if not x[1]]
print("\n===== DOMAINS SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
