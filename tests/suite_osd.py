"""Unit suite for the generic OSD message channel."""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import sys, json
import unittest.mock as mock
from syncplay import constants
from syncplay.utils import meetsMinVersion
from syncplay.server import Room, SyncFactory
import syncplay.messages as M
M.setLanguage("en")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] OSD :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

P = SyncFactory._parseOSDCommand

# ---- parser matrix ----
o, t = P("/osd hello world")
check("bare text", o == {} and t == "hello world", repr((o, t)))
o, t = P("/osd dur=10 colour=#FF0000 pos=top size=70 Big red banner")
check("all options", o == {"duration": "10", "colour": "#FF0000", "position": "top", "size": "70"} and t == "Big red banner", repr(o))
o, t = P("/osd ass=1 {\\b1}bold{\\b0}")
check("ass=1 flag", o == {"assFormatting": True} and t == "{\\b1}bold{\\b0}", repr((o, t)))
o, t = P("/osd ass=0 plain")
check("ass=0 -> False", o == {"assFormatting": False} and t == "plain")
o, t = P("/osd color=#00FF00 american spelling works")
check("color= alias", o == {"colour": "#00FF00"} and t == "american spelling works")
o, t = P("/osd duration=7 text")
check("duration= alias", o == {"duration": "7"} and t == "text")
o, t = P("/osd 2+2=4 is true")
check("'=' inside body text not eaten", o == {} and t == "2+2=4 is true", repr((o, t)))
o, t = P("/osd dur=5")
check("options only -> empty text (usage)", o == {"duration": "5"} and t == "")
o, t = P("/osd")
check("bare command -> empty text", o == {} and t == "")
o, t = P("/osd size=60 note the size=60 in text")
check("later key=value tokens stay in text", o == {"size": "60"} and t == "note the size=60 in text", repr(t))

# ---- stripOSDTags ----
S = SyncFactory.stripOSDTags
check("strip single block", S("{\\b1}bold{\\b0}") == "bold")
check("strip multiple + \\N", S("{\\an5}line one\\Nline two {\\i1}it{\\i0}") == "line one line two it")
check("no tags untouched", S("plain text") == "plain text")
check("unclosed brace left alone (regex needs })", S("weird { text") == "weird { text")

# ---- sendOSDMessage normalization + routing ----
class FakeWatcher:
    def __init__(self, name, version, features):
        self._name, self._version, self._features = name, version, features
        self.chats, self.osds = [], []
    def getName(self): return self._name
    def isAfk(self): return False
    def supportsFeature(self, ft): return self._features.get(ft, False)
    def sendChatMessage(self, m, skipIfSupportsFeature=None):
        if meetsMinVersion(self._version, constants.CHAT_MIN_VERSION):
            if skipIfSupportsFeature and self.supportsFeature(skipIfSupportsFeature):
                return
            self.chats.append(m["message"])
    def sendOSDMessage(self, payload): self.osds.append(payload)

f = SyncFactory.__new__(SyncFactory)
room = Room("r", None)
cap = FakeWatcher("cap", "1.7.6", {"osdMessages": True})
fb = FakeWatcher("fb", "1.6.0", {})
old = FakeWatcher("old", "1.4.0", {})
room._watchers = {"cap": cap, "fb": fb, "old": old}

f.sendOSDMessage(room, "{\\b1}Hi{\\b0} there", "op", colour="#FF0000", position="bottom-left",
                 size=70, duration=10, assFormatting=True)
check("capable got Set payload", len(cap.osds) == 1 and cap.chats == [])
p = cap.osds[0]
check("payload shape", p == {"text": "{\\b1}Hi{\\b0} there", "ass": True, "colour": "#FF0000",
                             "position": "bottom-left", "size": 70, "duration": 10.0}, repr(p))
check("fallback got stripped chat", fb.chats == ["Hi there"] and fb.osds == [], repr(fb.chats))
check("pre-1.5.0 got nothing", old.chats == [] and old.osds == [])

cap.osds.clear(); fb.chats.clear()
f.sendOSDMessage(room, "x" * 2000, "op", colour="red", position="nowhere", size="huge",
                 duration=9999, assFormatting=False)
p = cap.osds[0]
check("bad colour -> default", p["colour"] == constants.OSD_MESSAGE_DEFAULT_COLOUR)
check("bad position -> default", p["position"] == constants.OSD_MESSAGE_DEFAULT_POSITION)
check("bad size -> default", p["size"] == constants.OSD_MESSAGE_DEFAULT_SIZE)
check("duration clamped to max", p["duration"] == constants.OSD_MESSAGE_MAX_DURATION)
check("text capped at 1000", len(p["text"]) == constants.OSD_MESSAGE_MAX_LENGTH)
cap.osds.clear(); fb.chats.clear()
f.sendOSDMessage(room, "line1\nline2\rrest", "op")
check("real newlines stripped", cap.osds[0]["text"] == "line1line2rest")
f.sendOSDMessage(None, "x", "op"); f.sendOSDMessage(room, "", "op")
check("None room / empty text safe no-ops", len(cap.osds) == 1)

# string size/duration from chat parser coerce
cap.osds.clear()
f.sendOSDMessage(room, "t", "op", size="70", duration="7.5")
check("string size/duration coerced", cap.osds[0]["size"] == 70 and cap.osds[0]["duration"] == 7.5)

# ---- authorization + command handling ----
class AuthWatcher(FakeWatcher):
    def __init__(self, name, controller, room_):
        FakeWatcher.__init__(self, name, "1.7.6", {})
        self._controller, self._room = controller, room_
    def isController(self): return self._controller
    def getRoom(self): return self._room

r2 = Room("r2", None)
sender = AuthWatcher("boss", True, r2)
other = FakeWatcher("other", "1.6.0", {})
r2._watchers = {"boss": sender, "other": other}
f._handleOSDChatCommand(sender, "/osd dur=3 Announcement!")
check("controller command broadcast to room", other.chats == ["Announcement!"], repr(other.chats))
check("sender (fallback-capable) also received", sender.chats == ["Announcement!"], repr(sender.chats))

pleb = AuthWatcher("pleb", False, r2)
r2._watchers["pleb"] = pleb
other.chats.clear(); sender.chats.clear()
f._handleOSDChatCommand(pleb, "/osd hi")
check("non-controller: private error only", len(pleb.chats) == 1 and "operators" in pleb.chats[0]
      and other.chats == [] and sender.chats == [], repr(pleb.chats))

sender.chats.clear(); other.chats.clear()
f._handleOSDChatCommand(sender, "/osd dur=5")
check("options-only: private usage only", len(sender.chats) == 1 and "Usage" in sender.chats[0]
      and other.chats == [], repr(sender.chats))

# ---- sendChat interception (pre-truncation) ----
f.maxChatMessageLength = 20
f._roomManager = mock.Mock()
long_cmd = "/osd ass=1 " + "{\\b1}Very long ASS announcement far exceeding chat limits{\\b0}"
sender.chats.clear(); other.chats.clear()
f.sendChat(sender, long_cmd)
check("interception before 20-char chat truncation",
      other.chats == ["Very long ASS announcement far exceeding chat limits"], repr(other.chats))
check("normal chat still routed to broadcast", (f.sendChat(sender, "hello"), f._roomManager.broadcastRoom.called)[1])

# ---- client pipeline: handleSet -> UiManager -> player payload ----
from syncplay.protocols import SyncClientProtocol
from syncplay.client import UiManager
from syncplay.players.mpv import MpvPlayer
from syncplay.players.basePlayer import BasePlayer
from syncplay.players.vlc import VlcPlayer

check("capability: mpv genericOSDSupported", MpvPlayer.genericOSDSupported is True)
check("capability: base/vlc off", BasePlayer.genericOSDSupported is False and getattr(VlcPlayer, "genericOSDSupported", False) is False)

class FakePlayer:
    def __init__(self): self.calls = []
    def showGenericOSD(self, *a): self.calls.append(a)
class FakeClient:
    def __init__(self): self._player = FakePlayer()
fp_ui = mock.Mock()
ui = UiManager(FakeClient(), fp_ui)
ui.showGenericOSD({"text": "{\\i1}styled{\\i0} msg", "ass": True, "colour": "#00FF00",
                   "position": "bottom-right", "size": 40, "duration": 8})
call = ui._client._player.calls[0]
check("UiManager passes ASS text unmangled", call[0] == "{\\i1}styled{\\i0} msg" and call[1] is True, repr(call))
check("position -> \\an3, colour/size/duration intact", call[2] == 3 and call[3] == "#00FF00" and call[4] == 40 and call[5] == 8.0, repr(call))
check("log line tag-stripped", fp_ui.showMessage.call_args[0][0] == "styled msg", repr(fp_ui.showMessage.call_args))

ui._client._player.calls.clear()
ui.showGenericOSD({"text": "t", "size": 9999, "duration": -5, "colour": "junk", "position": "junk"})
call = ui._client._player.calls[0]
check("client re-clamps hostile wire values", call[2] == 8 and call[3] == constants.OSD_MESSAGE_DEFAULT_COLOUR
      and call[4] == constants.OSD_MESSAGE_MAX_SIZE and call[5] == 0.5, repr(call))
ui.showGenericOSD({"no": "text"}); ui.showGenericOSD("garbage"); ui.showGenericOSD({"text": 42})
check("malformed values safe no-ops", len(ui._client._player.calls) == 1)

# protocol dispatch
cp = SyncClientProtocol.__new__(SyncClientProtocol)
got = []
cli = mock.Mock(); cli.ui.showGenericOSD = lambda v: got.append(v)
cp._client = cli
cp.handleSet({"osdMessage": {"text": "hi", "duration": 3}})
check("handleSet dispatches osdMessage", got == [{"text": "hi", "duration": 3}])

# mpv payload JSON (no sanitize mangling)
mpv = MpvPlayer.__new__(MpvPlayer)
class FakeListener:
    def __init__(self): self.lines = []
    def sendLine(self, l): self.lines.append(l)
mpv._listener = FakeListener()
mpv.showGenericOSD("{\\b1}RAW{\\b0}", True, 8, "#FF0000", 60, 12.0)
line = mpv._listener.lines[0]
check("mpv routes to osd-message", line[:3] == ["script-message-to", "syncplayintf", "osd-message"])
decoded = json.loads(line[3])
check("mpv JSON payload intact (braces unescaped)", decoded == {"text": "{\\b1}RAW{\\b0}", "ass": True,
      "an": 8, "colour": "#FF0000", "size": 60, "duration": 12.0}, repr(decoded))
mpv2 = MpvPlayer.__new__(MpvPlayer)
mpv2.showGenericOSD("x", False, 8, "#FFFFFF", 50, 5)  # no listener -> safe
check("mpv safe without listener", True)

# ---- i18n ----
for k in ("osd-command-unauthorised-chat-message", "osd-command-usage-chat-message"):
    check("en key present: " + k, k in M.messages["en"])
bad = [l for l in M.getMissingStrings().splitlines() if "Unused" in l and "osd-command" in l]
check("no osd keys leaked to non-English dicts", not bad, repr(bad))

fails = [x for x in RESULTS if not x[1]]
print("\n===== OSD SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
