"""Unit suite for admin track proposals."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import sys, json, types
import unittest.mock as mock
from syncplay import constants
from syncplay.utils import meetsMinVersion
from syncplay.server import Room, SyncFactory
from syncplay.protocols import SyncServerProtocol, SyncClientProtocol
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Tracks :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

class FW:
    def __init__(self, name, admin=False, version="1.7.6", features=None):
        self._name, self._admin = name, admin
        self._version, self._features = version, features or {}
        self.chats, self.proposals = [], []
        self._room = None
        self._lastTrackProposalAnnouncedFile = None
    def getName(self): return self._name
    def isAdmin(self): return self._admin
    def getRoom(self): return self._room
    def getFile(self): return getattr(self, "file", {"name": "ep1.mkv"})
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def sendChatMessage(self, m, skipIfSupportsFeature=None):
        if meetsMinVersion(self._version, constants.CHAT_MIN_VERSION):
            self.chats.append(m["message"])
    def sendTrackProposal(self, p): self.proposals.append(p)

f = SyncFactory.__new__(SyncFactory)
room = Room("r", None)
adm = FW("adm", admin=True, features={"trackProposals": True}); adm._room = room
cap = FW("cap", features={"trackProposals": True}); cap._room = room
fb = FW("fb", version="1.6.0"); fb._room = room
old = FW("old", version="1.4.0"); old._room = room
room._watchers = {"adm": adm, "cap": cap, "fb": fb, "old": old}

GOOD = {"audioId": 2, "subId": "no", "audioName": "#2 eng (DTS)", "subName": "off",
        "signature": "audio:1:eng|audio:2:jpn|sub:1:eng"}

# ---------- authorization ----------
f.setTrackProposal(cap, dict(GOOD))
check("non-admin publish: private error only", cap.chats == ["Only server admins can publish track recommendations."]
      and room.getTrackProposal() is None and fb.chats == [])
cap.chats.clear()

# ---------- publish + routing ----------
f.setTrackProposal(adm, dict(GOOD))
p = room.getTrackProposal()
check("proposal stored with attribution", p is not None and p["by"] == "adm", repr(p))
check("capable watcher got Set payload", len(cap.proposals) == 1 and cap.proposals[0]["audioId"] == 2
      and cap.proposals[0]["subId"] == "no" and cap.proposals[0]["signature"] == GOOD["signature"], repr(cap.proposals))
check("capable watcher got NO chat", cap.chats == [])
check("fallback got chat with descriptions", fb.chats == ["adm recommends tracks - audio: #2 eng (DTS), subtitles: off"], repr(fb.chats))
check("pre-1.5.0 got nothing", old.chats == [] and old.proposals == [])
check("publisher got Set (capable) + private ack", len(adm.proposals) == 1
      and "Track recommendation published to the room." in adm.chats, repr(adm.chats))
adm.chats.clear(); fb.chats.clear(); cap.proposals.clear(); adm.proposals.clear()

# ---------- validation matrix ----------
cases = [
    ({"audioId": True, "subId": 3, "signature": "s"}, lambda p: "audioId" not in p and p["subId"] == 3, "bool id rejected, int kept"),
    ({"audioId": 500, "subId": 0, "signature": "s"}, None, "out-of-range ids -> nothing usable"),
    ({"audioId": "nope", "subId": "junk"}, None, "junk strings -> nothing usable"),
    ({"audioId": 1, "audioName": "x" * 500, "signature": "s"}, lambda p: len(p["audioName"]) <= constants.TRACK_PROPOSAL_MAX_NAME_LENGTH, "name truncated"),
    ({"audioId": 1, "signature": "x" * 5000}, lambda p: len(p["signature"]) == constants.TRACK_PROPOSAL_MAX_SIGNATURE_LENGTH, "signature capped"),
    ({"audioId": 1, "signature": 12345}, lambda p: "signature" not in p, "non-string signature dropped"),
]
for payload, pred, why in cases:
    room.setTrackProposal(None)
    for w in room.getWatchers(): w.chats.clear(); w.proposals.clear()
    f.setTrackProposal(adm, payload)
    stored = room.getTrackProposal()
    if pred is None:
        check("validation: " + why, stored is None, repr(stored))
    else:
        check("validation: " + why, stored is not None and pred(stored), repr(stored))
f.setTrackProposal(adm, "garbage")
check("validation: non-dict payload safe", True)
for w in room.getWatchers(): w.chats.clear(); w.proposals.clear()

# ---------- per-watcher file-change reminders ----------
room.setTrackProposal(None)
fb._lastTrackProposalAnnouncedFile = None
f.setTrackProposal(adm, dict(GOOD))
check("publish records fallback's current file", fb._lastTrackProposalAnnouncedFile == "ep1.mkv")
for w in room.getWatchers(): w.chats.clear(); w.proposals.clear()
f._remindTrackProposalOnFileChange(fb)
check("same file: no reminder", fb.chats == [])
fb.file = {"name": "ep2.mkv"}
f._remindTrackProposalOnFileChange(fb)
check("fallback file change: reminded once", len(fb.chats) == 1 and "recommends tracks" in fb.chats[0], repr(fb.chats))
fb.chats.clear()
f._remindTrackProposalOnFileChange(fb)
check("repeat same new file: silent", fb.chats == [])
cap.file = {"name": "ep2.mkv"}
f._remindTrackProposalOnFileChange(cap)
check("capable file change: no traffic", cap.chats == [] and cap.proposals == [])
nofile = FW("nofile", version="1.6.0"); nofile._room = room; nofile.file = None
f._sendTrackProposalToWatcher(nofile, room.getTrackProposal())
check("fileless fallback at delivery: deferred (no stale chat)", nofile.chats == [])
nofile.file = {"name": "ep2.mkv"}
f._remindTrackProposalOnFileChange(nofile)
check("deferred fallback reminded on first file", len(nofile.chats) == 1)
loner = FW("loner", version="1.6.0"); loner._room = Room("empty", None)
f._remindTrackProposalOnFileChange(loner)
loner._room = None
f._remindTrackProposalOnFileChange(loner)
check("no proposal / None room: safe no-ops", loner.chats == [])

# ---------- late joiner delivery ----------
lateCap = FW("lateCap", features={"trackProposals": True})
lateFb = FW("lateFb", version="1.6.0"); lateFb.file = {"name": "ep2.mkv"}
f._sendTrackProposalToWatcher(lateCap, room.getTrackProposal())
f._sendTrackProposalToWatcher(lateFb, room.getTrackProposal())
check("late joiner capable: Set", len(lateCap.proposals) == 1 and lateCap.chats == [])
check("late joiner fallback: chat", len(lateFb.chats) == 1 and lateFb.proposals == [])

# ---------- room-empty cleanup (direct semantics) ----------
room.setTrackProposal(None)
check("cleanup clears proposal state", room.getTrackProposal() is None)

# ---------- /tracks legacy server notice + dispatcher ----------
f2 = SyncFactory.__new__(SyncFactory)
f2.adminPassword = "x"; f2.maxChatMessageLength = 150
class RM:
    def broadcastRoom(self, sender, l):
        for w in sender.getRoom().getWatchers(): l(w)
f2._roomManager = RM()
r2 = Room("r2", None)
legacy = FW("legacy", version="1.6.0"); legacy._room = r2
peer = FW("peer", version="1.6.0"); peer._room = r2
r2._watchers = {"legacy": legacy, "peer": peer}
f2.sendChat(legacy, "/tracks")
check("/tracks from legacy: private notice only",
      legacy.chats == ["Track recommendations are published by server admins from a compatible player (Ctrl+T in mpv, or type /tracks there)."]
      and peer.chats == [], repr(legacy.chats))
legacy.chats.clear()
f2.sendChat(legacy, "/tracksfoo bar")
check("/tracksfoo falls through as chat", peer.chats == ["/tracksfoo bar"])

# ---------- server protocol Set branch ----------
sp = SyncServerProtocol.__new__(SyncServerProtocol)
sp._factory = mock.Mock()
sp._watcher = adm
sp._logged = True
sp.handleSet({"trackProposal": {"audioId": 1}})
check("server handleSet dispatches trackProposal", sp._factory.setTrackProposal.call_args[0] == (adm, {"audioId": 1}))

# ---------- client side ----------
from syncplay.client import SyncplayClient, UiManager
from syncplay.players.basePlayer import BasePlayer
from syncplay.players.mpv import MpvPlayer
from syncplay.players.vlc import VlcPlayer

check("capability: mpv trackProposalsSupported", MpvPlayer.trackProposalsSupported is True)
check("capability: base/vlc off", BasePlayer.trackProposalsSupported is False
      and getattr(VlcPlayer, "trackProposalsSupported", False) is False)
BasePlayer().setTrackProposal({"x": 1}); BasePlayer().requestTrackPublish()
check("base no-ops safe", True)

# requestTrackPublish routing
stub = types.SimpleNamespace()
calls = {"pub": 0, "err": []}
player = types.SimpleNamespace(trackProposalsSupported=True, requestTrackPublish=lambda: calls.__setitem__("pub", calls["pub"] + 1))
stub._player = player
stub.ui = types.SimpleNamespace(showErrorMessage=lambda m: calls["err"].append(m))
SyncplayClient.requestTrackPublish(stub)
check("requestTrackPublish -> player when supported", calls["pub"] == 1 and calls["err"] == [])
stub._player = types.SimpleNamespace(trackProposalsSupported=False)
SyncplayClient.requestTrackPublish(stub)
check("requestTrackPublish -> error when unsupported", len(calls["err"]) == 1)
stub._player = None
SyncplayClient.requestTrackPublish(stub)
check("requestTrackPublish -> error when no player", len(calls["err"]) == 2)

# publishTrackProposal guard
sent = []
stub2 = types.SimpleNamespace(_protocol=types.SimpleNamespace(logged=True, sendTrackProposal=lambda p: sent.append(p)))
SyncplayClient.publishTrackProposal(stub2, {"audioId": 1})
check("publishTrackProposal forwards", sent == [{"audioId": 1}])
stub2._protocol = None
SyncplayClient.publishTrackProposal(stub2, {"audioId": 1})
stub2._protocol = types.SimpleNamespace(logged=False, sendTrackProposal=lambda p: sent.append(p))
SyncplayClient.publishTrackProposal(stub2, {"audioId": 1})
SyncplayClient.publishTrackProposal(types.SimpleNamespace(_protocol=types.SimpleNamespace(logged=True, sendTrackProposal=lambda p: sent.append(p))), "junk")
check("publishTrackProposal guards (no protocol/not logged/non-dict)", sent == [{"audioId": 1}])

# client protocol sender
cp = SyncClientProtocol.__new__(SyncClientProtocol)
out = []
cp.sendMessage = lambda m: out.append(m)
cp.sendTrackProposal({"audioId": 2})
check("client sendTrackProposal payload", out == [{"Set": {"trackProposal": {"audioId": 2}}}])

# client handleSet dispatch
cp2 = SyncClientProtocol.__new__(SyncClientProtocol)
got = []
cp2._client = mock.Mock()
cp2._client.ui.setTrackProposal = lambda v: got.append(v)
cp2.handleSet({"trackProposal": {"audioId": 3}})
check("client handleSet dispatches trackProposal", got == [{"audioId": 3}])

# UiManager.setTrackProposal: log + generic OSD + forward
class FakePlayer:
    def __init__(self): self.osd, self.props = [], []
    def showGenericOSD(self, *a): self.osd.append(a)
    def setTrackProposal(self, p): self.props.append(p)
fp = FakePlayer()
ui_mock = mock.Mock()
ui = UiManager(types.SimpleNamespace(_player=fp), ui_mock)
proposal = {"by": "adm", "audioId": 2, "audioName": "#2 eng", "subId": "no", "subName": "off", "signature": "s"}
ui.setTrackProposal(proposal)
check("UiManager: no client-side OSD (lua shows the status-aware notice)", fp.osd == [], repr(fp.osd))
check("UiManager: payload forwarded to player", fp.props == [proposal])
check("UiManager: logged to UI", ui_mock.showMessage.called)
ui2 = UiManager(types.SimpleNamespace(_player=None), mock.Mock())
ui2.setTrackProposal(proposal); ui2.setTrackProposal("junk")
check("UiManager: no-player + junk guards", True)

# consoleUI command dispatch
from syncplay.ui.consoleUI import ConsoleUI
con = ConsoleUI.__new__(ConsoleUI)
ccalls = []
con._syncplayClient = types.SimpleNamespace(requestTrackPublish=lambda: ccalls.append(1))
con.executeCommand("tracks")
check("consoleUI 'tracks' command dispatches", ccalls == [1])

# mpv methods + back-channel parse
mpv = MpvPlayer.__new__(MpvPlayer)
class FakeListener:
    def __init__(self): self.lines = []
    def sendLine(self, l): self.lines.append(l)
mpv._listener = FakeListener()
mpv.setTrackProposal(proposal)
check("mpv setTrackProposal routes JSON", mpv._listener.lines[0][:3] == ["script-message-to", "syncplayintf", "set-track-proposal"]
      and json.loads(mpv._listener.lines[0][3]) == proposal)
mpv.requestTrackPublish()
check("mpv requestTrackPublish routes", mpv._listener.lines[1] == ["script-message-to", "syncplayintf", "publish-tracks"])
MpvPlayer.__new__(MpvPlayer).setTrackProposal(proposal)
MpvPlayer.__new__(MpvPlayer).requestTrackPublish()
check("mpv methods safe without listener", True)

# _handleUnknownLine marker parse (thread-mirrored via reactor.callFromThread)
mpv2 = MpvPlayer.__new__(MpvPlayer)
published = []
mpv2.reactor = types.SimpleNamespace(callFromThread=lambda fn, *a: fn(*a))
mpv2._client = types.SimpleNamespace(publishTrackProposal=lambda p: published.append(p),
                                     ui=types.SimpleNamespace(showDebugMessage=lambda m: None))
mpv2._listener = FakeListener()
wire = json.dumps({"audioId": 2, "subId": "no", "signature": "audio:1:eng"})
mpv2._handleUnknownLine("<SyncplayTrackProposal>" + wire + "</SyncplayTrackProposal>")
check("marker line parse round-trip", published == [{"audioId": 2, "subId": "no", "signature": "audio:1:eng"}], repr(published))
mpv2._handleUnknownLine("<SyncplayTrackProposal>not json</SyncplayTrackProposal>")
mpv2._handleUnknownLine("<SyncplayTrackProposal>[1,2]</SyncplayTrackProposal>")
check("malformed marker lines ignored", len(published) == 1)

# ---------- i18n ----------
keys = ["track-proposal-chat-message", "track-proposal-osd-message", "track-proposal-published-chat-message",
        "track-proposal-unauthorised-chat-message", "tracks-command-notice-chat-message",
        "tracks-not-supported-by-player-error"]
for k in keys:
    check("en key: " + k, k in M.messages["en"])
bad = [l for l in M.getMissingStrings().splitlines() if "Unused" in l and "track" in l.lower()]
check("no track keys leaked to non-English dicts", not bad, repr(bad))

fails = [x for x in RESULTS if not x[1]]
print("\n===== TRACKS SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
