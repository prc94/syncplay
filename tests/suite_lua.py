"""Validation of syncplayintf.lua: real parse + lint gates, plus structural and simulated checks.

The parse gate is the highest-value check here. A syntax error in this file does not degrade
gracefully — mpv fails to load the script and the *entire* overlay goes with it: chat, the yap
timer, the pause warning and track proposals all vanish at once, presenting as "the feature
didn't show up" rather than as an error.

It is checked against Lua 5.1 *and* 5.2 because mpv embeds different versions on different
platforms (LuaJIT/5.1 on Windows and several distros, 5.2 elsewhere), and a newer host `luac`
will happily accept 5.3+ syntax such as `//` or bitwise operators that every mpv build rejects.
Checking only against whatever `luac` happens to be installed is worse than not checking.

The structural and simulation checks below cover what no linter can: OSD render order, blink
duty cycle, timeout-vs-State-cadence maths, and constants mirrored from constants.py.

Parse and lint gates skip cleanly when the lua toolchain is absent.
"""
import os
import subprocess
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re, sys
from lint_common import (ToolMissing, diffAgainstBaseline, humanFor, loadBaseline, runLuacheck,
                         writeBaseline)
from syncplay import constants  # cache-size mirror assertions

LUA_PATH = os.path.join("syncplay", "resources", "syncplayintf.lua")
LUACHECK_BASELINE = os.path.join(REPO_ROOT, "tests", "lint_baseline_luacheck.txt")
src = open(os.path.join(REPO_ROOT, "syncplay", "resources", "syncplayintf.lua")).read()
lines = src.splitlines()
ok_all = True
RESULTS = []
def check(name, cond, detail=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    RESULTS.append((name, bool(cond)))
    print("[{}] Lua :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

def line_of(pat):
    for i, l in enumerate(lines, 1):
        if pat in l:
            return i
    return None

if "--update-baseline" in sys.argv:
    try:
        fingerprints, _ = runLuacheck(LUA_PATH)
    except ToolMissing:
        print("luacheck is not installed - cannot regenerate the baseline")
        sys.exit(1)
    writeBaseline(LUACHECK_BASELINE, fingerprints, "luacheck", "suite_lua.py")
    print("Wrote {} entries to {}".format(len(fingerprints), os.path.relpath(LUACHECK_BASELINE, REPO_ROOT)))
    sys.exit(0)

# 0. real parse gate against every lua version mpv is known to embed
COMPILERS = [("5.1", "luac5.1"), ("5.2", "luac5.2")]
found_any = False
for version, binary in COMPILERS:
    try:
        proc = subprocess.run([binary, "-p", LUA_PATH], cwd=REPO_ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        print("[SKIP] Lua :: parses under Lua {} ({} not installed)".format(version, binary))
        continue
    found_any = True
    check("parses under Lua {}".format(version), proc.returncode == 0,
          proc.stdout.strip()[:200] if proc.returncode else "clean")
if not found_any:
    # Deliberately not falling back to a bare `luac`: an unversioned one is typically newer than
    # anything mpv embeds, so a pass would be meaningless while looking reassuring.
    print("[SKIP] Lua :: parse gate - install lua5.1 and lua5.2 to enable it")

# 1. declaration-before-use ordering (file-scope locals must precede closures using them)
for name in ("pausewarning_osd", "last_pausewarning_osd_time", "PAUSEWARNING_OSD_TIMEOUT",
             "PAUSEWARNING_BLINK_CYCLE", "PAUSEWARNING_BLINK_ON_TIME", "PAUSEWARNING_TEXT_COLOUR",
             "yaptimer_osd", "last_yaptimer_osd_time", "YAPTIMER_OSD_TIMEOUT", "YAPTIMER_TEXT_COLOUR",
             "bufferhold_osd", "last_bufferhold_osd_time", "BUFFERHOLD_OSD_TIMEOUT",
             "BUFFERHOLD_TEXT_COLOUR",
             "osd_messages", "MAX_OSD_MESSAGES"):
    decl = line_of("local " + name)
    uses = [i for i, l in enumerate(lines, 1) if name in l and not l.strip().startswith("local " + name)]
    check("local '{}' declared before first use".format(name),
          decl is not None and (not uses or decl < min(uses)),
          "decl@{} first-use@{}".format(decl, min(uses) if uses else "-"))

# 2. wiring: setter, processor, render-loop call, handler registration
check("set_pausewarning_osd defined", line_of("function set_pausewarning_osd") is not None)
check("process_pausewarning_osd defined", line_of("function process_pausewarning_osd") is not None)
in_update = re.search(r"function chat_update\(\).*?\nend\n", src, re.S).group(0)
check("chat_update calls process_pausewarning_osd", "process_pausewarning_osd()" in in_update)
check("chat_update calls process_yaptimer_osd", "process_yaptimer_osd()" in in_update)
check("chat_update calls process_bufferhold_osd", "process_bufferhold_osd()" in in_update)
order = [in_update.find(s) for s in ("process_alert_osd()", "process_notification_osd(", "process_chat_item(", "process_yaptimer_osd()", "process_pausewarning_osd()", "process_bufferhold_osd()")]
check("render order: alert < notification < chat < yaptimer < pausewarning < bufferhold",
      all(x >= 0 for x in order) and order == sorted(order), str(order))
check("handler 'pausewarning-osd' registered", "mp.register_script_message('pausewarning-osd'" in src)
check("handler 'yaptimer-osd' registered", "mp.register_script_message('yaptimer-osd'" in src)
check("handler 'osd-message' registered", "mp.register_script_message('osd-message'" in src)
check("handler 'bufferhold-osd' registered", "mp.register_script_message('bufferhold-osd'" in src)
check("set_bufferhold_osd defined", line_of("function set_bufferhold_osd") is not None)
check("process_bufferhold_osd defined", line_of("function process_bufferhold_osd") is not None)
check("bufferhold setter defined before registration",
      line_of("function set_bufferhold_osd") < line_of("mp.register_script_message('bufferhold-osd'"))
# The status poll carries the cache state Syncplay's own detection reads (see docs/buffer-pause.md).
check("status poll reports paused-for-cache", 'mp.get_property_native("paused-for-cache")' in src)
check("status poll reports cache-buffering-state", 'mp.get_property_native("cache-buffering-state")' in src)
# --- track proposals ---
check("handler 'set-track-proposal' registered", "mp.register_script_message('set-track-proposal'" in src)
check("handler 'publish-tracks' registered", "mp.register_script_message('publish-tracks'" in src)
check("hotkey binding registered", "mp.add_key_binding(\"Ctrl+t\", \"syncplay_publish_tracks\", publish_tracks)" in src)
check("file-loaded apply registration", 'mp.register_event("file-loaded"' in src and "apply_track_proposal(true)" in src)
tp_apply = re.search(r"function apply_track_proposal\(.*?\nend\n", src, re.S).group(0)
check("apply: layout gate via cache lookup", "track_layout_signature()" in tp_apply
      and "proposal_for_current_file()" in tp_apply and 'return "mismatch"' in tp_apply)
check("apply: nil-guards (empty cache / idle player / no match)", "#track_proposals == 0" in tp_apply
      and "signature == nil" in tp_apply and "proposal == nil" in tp_apply)
tp_pub = re.search(r"function publish_tracks\(.*?\nend\n", src, re.S).group(0)
check("publish: no-file guard", "signature == nil" in tp_pub)
check("publish: marker emit", "<SyncplayTrackProposal>" in tp_pub and "commandv" in tp_pub)
check("publish: in-function require", "require 'mp.utils'" in tp_pub)
check("receipt path applies silently", "apply_track_proposal(false)" in src)
handler = re.search(r"mp.register_script_message\('set-track-proposal'.*?\nend\)", src, re.S).group(0)
check("receipt notice: applied vs pending branches", '"Applied "' in handler and "recommends" in handler
      and "applies when a matching file loads" in handler)
sto = re.search(r"function show_track_osd\(.*?\nend\n", src, re.S).group(0)
check("track OSD at bottom-center (an=2) for 6s", "an = 2" in sto and "mp.get_time() + 6" in sto)
check("track OSD text ass-escaped", "ass_escape(text)" in sto)
check("file-loaded catch-up uses applied wording", 'show_track_osd("Applied "' in src)
check("apply keybind registered", 'mp.add_key_binding("Alt+t", "syncplay_apply_tracks", apply_tracks_keybind)' in src)
check("apply-tracks script-message registered", "mp.register_script_message('apply-tracks'" in src)
check("domains keybind + handler registered",
      'mp.add_key_binding("Ctrl+d", "syncplay_publish_domains", publish_domains)' in src
      and "mp.register_script_message('publish-domains'" in src
      and "<SyncplayPublishDomains>" in src)
check("AFK keybind + handler registered",
      'mp.add_key_binding("Ctrl+a", "syncplay_toggle_afk", toggle_afk)' in src
      and "mp.register_script_message('toggle-afk'" in src
      and "<SyncplayToggleAfk>" in src)
check("room-lock keybind + handler registered",
      'mp.add_key_binding("Ctrl+l", "syncplay_toggle_room_lock", toggle_room_lock)' in src
      and "mp.register_script_message('toggle-room-lock'" in src
      and "<SyncplayToggleLock>" in src)
kb = re.search(r"function apply_tracks_keybind\(.*?\nend\n", src, re.S).group(0)
check("keybind explains none/idle/mismatch", all(s in kb for s in ('"none"', '"idle"', '"mismatch"')))
check("apply returns status strings", all('return "%s"' % s in tp_apply for s in ("none", "idle", "mismatch", "applied")) or
      ('return "none"' in tp_apply and 'return "idle"' in tp_apply and 'return "mismatch"' in tp_apply and '"applied" or "empty"' in tp_apply))
check("local track_proposals cache declared before use",
      0 <= src.find("local track_proposals = {}") < src.find("function store_track_proposal"))
# --- track proposal cache (store_track_proposal / proposal_for_current_file) ---
check("cache max mirrors constants.TRACK_CACHE_MAX_ENTRIES",
      "local TRACK_CACHE_MAX = {}".format(constants.TRACK_CACHE_MAX_ENTRIES) in src)
store_body = re.search(r"function store_track_proposal\(.*?\nend\n", src, re.S).group(0)
check("store: de-dupes by signature", "track_proposals[i].signature == payload.signature" in store_body
      and "table.remove(track_proposals, i)" in store_body)
check("store: FIFO eviction past cap", "> TRACK_CACHE_MAX" in store_body and "table.remove(track_proposals, 1)" in store_body)
pfcf = re.search(r"function proposal_for_current_file\(.*?\nend\n", src, re.S).group(0)
check("lookup: newest cached match wins (reverse scan)", "for i = #track_proposals, 1, -1 do" in pfcf
      and "track_proposals[i].signature == signature" in pfcf)

# Python port of the cache: de-dupe, FIFO eviction, newest-match-wins
def make_cache():
    return []
def store(cache, payload):
    # Mirror lua store_track_proposal: signature-less payloads skip de-dupe but are still appended,
    # and .signature comparisons are nil-safe (lua reads a missing field as nil, never errors).
    if payload.get("signature") is not None:
        cache[:] = [p for p in cache if p.get("signature") != payload["signature"]]
    cache.append(payload)
    while len(cache) > constants.TRACK_CACHE_MAX_ENTRIES:
        cache.pop(0)
    return cache
def lookup(cache, signature):
    if signature is None:
        return None
    for p in reversed(cache):  # newest wins
        if p.get("signature") == signature:
            return p
    return None
c = make_cache()
store(c, {"signature": "A", "audioId": 1})
store(c, {"signature": "B", "audioId": 2})
store(c, {"signature": "A", "audioId": 9})  # re-publish A
check("port: re-publish same layout de-dupes, newest kept",
      len(c) == 2 and lookup(c, "A")["audioId"] == 9, repr(c))
for i in range(constants.TRACK_CACHE_MAX_ENTRIES + 5):
    store(c, {"signature": "L%d" % i, "audioId": 1})
check("port: cache bounded + oldest evicted first",
      len(c) == constants.TRACK_CACHE_MAX_ENTRIES and lookup(c, "A") is None and lookup(c, "L0") is None
      and lookup(c, "L%d" % (constants.TRACK_CACHE_MAX_ENTRIES + 4)) is not None, str(len(c)))
check("port: idle player (nil signature) never matches", lookup(c, None) is None)
store(c, {"audioId": 7})  # signature-less payload (lua appends these too); must not break lookup scans
check("port: nil-safe scan past a signature-less entry",
      lookup(c, "L%d" % (constants.TRACK_CACHE_MAX_ENTRIES + 4)) is not None)

# Python port of track_layout_signature + apply decision
def sig(tracks):
    if not tracks:
        return None
    parts = ["{}:{}:{}".format(t["type"], t["id"], (t.get("lang") or "").lower())
             for t in tracks if t["type"] in ("audio", "sub")]
    return "|".join(parts) if parts else None
def would_apply(local_tracks, proposal_sig):
    s = sig(local_tracks)
    return s is not None and proposal_sig is not None and s == proposal_sig
LAYOUT = [{"type": "video", "id": 1}, {"type": "audio", "id": 1, "lang": "ENG"},
          {"type": "audio", "id": 2, "lang": "jpn"}, {"type": "sub", "id": 1, "lang": "eng"}]
psig = sig(LAYOUT)
check("sim: same layout, different filename -> applies", would_apply(list(LAYOUT), psig))
check("sim: different layout -> stored, untouched", not would_apply(LAYOUT[:2], psig))
check("sim: idle player (no tracks) -> no error, no apply", not would_apply(None, psig) and not would_apply([], psig))
check("sim: next episode same layout -> applies on file-loaded", would_apply([dict(t) for t in LAYOUT], psig))
nolang = [{"type": "audio", "id": 1}, {"type": "sub", "id": 1}]
check("sim: missing lang tags both sides -> match", would_apply(nolang, sig(nolang)))
check("sim: lang present vs missing -> mismatch (strict)", not would_apply(nolang, psig))
check("sim: video-only file -> None signature, no apply", sig([{"type": "video", "id": 1}]) is None)
body = re.search(r"function add_osd_message\(.*?\nend\n", src, re.S).group(0)
check("plain mode ass_escapes text", "ass_escape(text)" in body and "payload.ass ~= true" in body)
check("json parse pcall-guarded", "pcall" in body and "parse_json" in body)
check("in-function require (file-scope utils local declared later)", "require 'mp.utils'" in body)
check("chat_update appends osd_messages_ass", "ass:append(osd_messages_ass())" in src)
conv = lambda c: c[5:7] + c[3:5] + c[1:3]
check("colour conversion RRGGBB->BGR (python port)", conv("#FF8800") == "0088FF" and conv("#123456") == "563412")
mo = re.search(r'return colour:sub\(6, 7\) \.\. colour:sub\(4, 5\) \.\. colour:sub\(2, 3\)', src)
check("lua conversion slices match python port", mo is not None)
reg = line_of("mp.register_script_message('pausewarning-osd'")
deff = line_of("function set_pausewarning_osd")
check("setter defined before registration", deff < reg, "{} < {}".format(deff, reg))

# 3. block balance inside the two new functions (function+if openers == end closers)
for fname in ("process_pausewarning_osd", "process_yaptimer_osd", "set_pausewarning_osd", "set_yaptimer_osd",
              "process_bufferhold_osd", "set_bufferhold_osd", "state_paused_and_position",
              "add_osd_message", "osd_messages_ass", "rrggbb_to_bgr",
              "track_layout_signature", "apply_track_proposal", "publish_tracks", "set_track_selection",
              "apply_tracks_keybind", "show_track_osd", "track_proposal_description"):
    m = re.search(r"function {}\([^)]*\).*?\nend\n".format(fname), src, re.S)
    body = m.group(0)
    openers = len(re.findall(r"\bfunction\b", body)) + len(re.findall(r"\bthen\b", body)) - len(re.findall(r"\belseif\b", body)) + len(re.findall(r"\bdo\b", body))
    closers = len(re.findall(r"\bend\b", body))
    check("block balance in {}".format(fname), openers == closers, "{} openers vs {} ends".format(openers, closers))

# 4. blink math simulation (port of the lua expression)
CYCLE = float(re.search(r"PAUSEWARNING_BLINK_CYCLE = ([\d.]+)", src).group(1))
ON = float(re.search(r"PAUSEWARNING_BLINK_ON_TIME = ([\d.]+)", src).group(1))
samples = [ (t % CYCLE) < ON for t in [i * 0.01 for i in range(2000)] ]  # 20s @10ms
duty = sum(samples) / len(samples)
toggles = sum(1 for a, b in zip(samples, samples[1:]) if a != b)
check("blink: duty cycle ~50%", 0.48 < duty < 0.52, "measured {:.1%}".format(duty))
check("blink: 0.5 Hz cycle (1 dip / 2s)", 19 <= toggles <= 21, "{} toggles / 20s".format(toggles))
check("blink: 1s dip per 2s cycle", abs((CYCLE - ON) - 1.0) < 0.01 and abs(CYCLE - 2.0) < 0.01, "off {}s per {}s cycle".format(CYCLE - ON, CYCLE))

# 5. timeout constants sane vs the 1s server refresh cadence
to = float(re.search(r"PAUSEWARNING_OSD_TIMEOUT = ([\d.]+)", src).group(1))
check("PW timeout {}s > 1s State cadence (won't flicker off)".format(to), to > 1.5)
yto = float(re.search(r"YAPTIMER_OSD_TIMEOUT = ([\d.]+)", src).group(1))
check("yap timeout {}s > 1s State cadence".format(yto), yto > 1.5)

# 6. whole-file rough sanity: quotes and braces pairing unchanged vs git HEAD version
head = subprocess.run(["git", "-C", REPO_ROOT, "show", "HEAD:syncplay/resources/syncplayintf.lua"],
                      capture_output=True, text=True).stdout
def sig(s):
    return (s.count("("), s.count(")"), s.count('"') % 2, len(re.findall(r"\bfunction\b", s)), len(re.findall(r"\bend\b", s)))
s_now, s_head = sig(src), sig(head)
check("whole-file paren balance", s_now[0] == s_now[1], "{} vs {}".format(s_now[0], s_now[1]))
check("whole-file even quote count", s_now[2] == 0)
check("committed HEAD version matches working tree", src == head, "identical" if src == head else "DIFFERS")

# 7. luacheck gate — scope/liveness analysis over every identifier in the file, which is a
# superset of the hand-maintained declaration-order list in section 1 (that list only protects
# names someone remembered to add; this covers new OSD elements automatically).
# Baselined: every finding present when this gate was added is in upstream's repl.lua ancestry.
try:
    lc_findings, lc_human = runLuacheck(LUA_PATH)
except ToolMissing:
    print("[SKIP] Lua :: luacheck gate (luacheck not installed)")
else:
    lc_new, lc_stale = diffAgainstBaseline(lc_findings, loadBaseline(LUACHECK_BASELINE))
    check("no new luacheck findings", not lc_new,
          "{} new".format(len(lc_new)) if lc_new else "{} findings, all baselined".format(len(lc_findings)))
    for fingerprint in lc_new[:20]:
        print("    NEW: {}".format(humanFor(fingerprint, lc_findings, lc_human)))
    if lc_stale:
        print("    note: {} baseline entries no longer occur (run --update-baseline to prune)".format(len(lc_stale)))

print("\n===== LUA SUMMARY: {} checks, {} failed =====".format(len(RESULTS), sum(1 for _, ok in RESULTS if not ok)))
sys.exit(0 if ok_all else 1)
