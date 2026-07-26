"""Unit + offline-E2E suite for the overlay auto-update feature (docs/auto-update.md).

Covers: syncplay/updater.py pure logic, the bootstrap decision + full bootstrap lifecycle in
subprocesses (apply / marker counting / quarantine), an offline end-to-end check+install against
a fake GitHub API served by stdlib http.server with a real signed overlay built by
ci/build-overlay.py, key policy (default pin, TOFU, wrong key, tamper), config plumbing, and
the offscreen ConfigDialog Updates group. No network access.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import base64
import http.server
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import threading
import types

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Updater :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

def raises(fn, excType):
    try:
        fn()
        return False, "no exception"
    except excType as e:
        return True, str(e)[:80]
    except Exception as e:
        return False, "{}: {}".format(type(e).__name__, str(e)[:80])

from syncplay import updater, constants
import syncplay

# =========================================================================================
# Section A: pure logic
# =========================================================================================

check("repo validation accepts owner/repo", updater.validateRepo("prc94/syncplay"))
check("repo validation rejects junk",
      not updater.validateRepo("") and not updater.validateRepo("noslash")
      and not updater.validateRepo("a/b/c") and not updater.validateRepo("evil.com/x?y=1")
      and not updater.validateRepo(None))

os.environ.pop("SYNCPLAY_BASE_FORK_RELEASE", None)
base, overlay = updater.getRunningVersions()
check("running versions without overlay", base == syncplay.fork_release and overlay is None)
os.environ["SYNCPLAY_BASE_FORK_RELEASE"] = "0"
base, overlay = updater.getRunningVersions()
check("running versions with overlay env", base == 0 and overlay == syncplay.fork_release)
os.environ.pop("SYNCPLAY_BASE_FORK_RELEASE", None)

os.environ["SYNCPLAY_OVERLAY_ROOT"] = "/tmp/fake-overlay-root"
check("overlay root honors env override", updater.getOverlayRoot() == "/tmp/fake-overlay-root")
os.environ.pop("SYNCPLAY_OVERLAY_ROOT", None)

check("install mode detected", updater.getInstallMode() in ("frozen", "source", "packaged"))
os.environ.pop("SYNCPLAY_UPDATE_FORCE_INSTALL", None)
sourceCanInstall = updater.canInstallOverlays()
os.environ["SYNCPLAY_UPDATE_FORCE_INSTALL"] = "1"
check("source mode is check-only unless forced",
      (updater.getInstallMode() != "frozen") <= (not sourceCanInstall) and updater.canInstallOverlays())
os.environ.pop("SYNCPLAY_UPDATE_FORCE_INSTALL", None)

fp = updater.keyFingerprint(base64.b64encode(b"\x01" * 32).decode())
check("fingerprint format", len(fp.split(" ")) == 8 and all(len(g) == 4 for g in fp.split(" ")))

# bootstrap decision function, straight from the entry stub
spec = importlib.util.spec_from_file_location("syncplay_entry", os.path.join(REPO_ROOT, "syncplayClient.py"))
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
D = entry._overlayDecision
check("decision: newer overlay applies", D(1, {"fork_release": 2, "min_base": 1}, 0, 2) == "apply")
check("decision: equal overlay ignored", D(2, {"fork_release": 2, "min_base": 1}, 0, 2) == "base")
check("decision: older overlay ignored", D(3, {"fork_release": 2, "min_base": 1}, 0, 2) == "base")
check("decision: min_base above base ignored", D(1, {"fork_release": 3, "min_base": 2}, 0, 2) == "base")
check("decision: marker at threshold quarantines", D(1, {"fork_release": 2, "min_base": 1}, 2, 2) == "quarantine")
check("decision: marker below threshold applies", D(1, {"fork_release": 2, "min_base": 1}, 1, 2) == "apply")
check("decision: junk meta boots base", D(1, None, 0, 2) == "base" and D(1, {"fork_release": "x"}, 0, 2) == "base")

# =========================================================================================
# Section B: offline end-to-end with a real signed overlay + fake GitHub API
# =========================================================================================

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

def keypair():
    key = Ed25519PrivateKey.generate()
    priv = base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return priv, pub

testPriv, testPub = keypair()
_, otherPub = keypair()

with tempfile.TemporaryDirectory(prefix="suite-updater-") as tmp:
    # --- build a signed r2 overlay from a bumped copy of the real package ---
    fakeRepo = os.path.join(tmp, "repo2")
    os.makedirs(fakeRepo)
    shutil.copytree(os.path.join(REPO_ROOT, "syncplay"), os.path.join(fakeRepo, "syncplay"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    initPath = os.path.join(fakeRepo, "syncplay", "__init__.py")
    with open(initPath, encoding="utf-8") as f:
        initSource = f.read()
    with open(initPath, "w", encoding="utf-8") as f:
        f.write(re.sub(r"^fork_release\s*=\s*\d+", "fork_release = 2", initSource, flags=re.MULTILINE))
    outDir = os.path.join(tmp, "dist")
    env = dict(os.environ)
    env["OVERLAY_SIGNING_KEY"] = testPriv
    proc = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "ci", "build-overlay.py"),
                           "--repo-root", fakeRepo, "--min-base", "1", "--out", outDir],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    check("signed r2 overlay builds", proc.returncode == 0, proc.stdout[-300:] if proc.returncode else "")
    with open(os.path.join(outDir, "syncplay-overlay-r2.manifest.json"), encoding="utf-8") as f:
        manifest2 = json.load(f)
    zipBytes2 = open(os.path.join(outDir, "syncplay-overlay-r2.zip"), "rb").read()

    ok, detail = raises(lambda: updater._validateManifest({}), updater.UpdateError)
    check("manifest validation rejects empty", ok, detail)
    bad = dict(manifest2); bad["filename"] = "evil.zip"
    ok, _ = raises(lambda: updater._validateManifest(bad), updater.UpdateError)
    check("manifest validation rejects filename mismatch", ok)
    try:
        updater._validateManifest(dict(manifest2))
        check("manifest validation accepts real manifest", True)
    except Exception as e:
        check("manifest validation accepts real manifest", False, str(e))

    # --- fake GitHub API ---
    served = {"zip2": zipBytes2, "releases": None}  # mutable so tests can tamper

    def releasesJson(baseUrl):
        r2assets = [
            {"name": "syncplay-overlay-r2.manifest.json", "browser_download_url": baseUrl + "/assets/manifest2"},
            {"name": "syncplay-overlay-r2.zip", "browser_download_url": baseUrl + "/assets/zip2"},
        ]
        return [{"draft": False, "assets": r2assets}]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/repos/") and path.endswith("/releases"):
                body = json.dumps(served["releases"]).encode()
            elif path == "/assets/manifest2":
                body = json.dumps(manifest2).encode()
            elif path == "/assets/zip2":
                body = served["zip2"]
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    baseUrl = "http://127.0.0.1:{}".format(server.server_address[1])
    served["releases"] = releasesJson(baseUrl)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    overlayRoot = os.path.join(tmp, "overlayroot")
    os.environ["SYNCPLAY_UPDATE_API_BASE"] = baseUrl
    os.environ["SYNCPLAY_OVERLAY_ROOT"] = overlayRoot
    os.environ["SYNCPLAY_UPDATE_FORCE_INSTALL"] = "1"
    realDefaultKey = constants.UPDATE_DEFAULT_REPO_PUBKEY
    constants.UPDATE_DEFAULT_REPO_PUBKEY = testPub
    config = {"updateRepo": constants.UPDATE_DEFAULT_REPO, "updateRepoKey": ""}

    status, message, url, manifest = updater.checkForUpdate(config, userInitiated=True)
    check("check finds installable r2", status == "updateavailale" and manifest is not None
          and manifest["fork_release"] == 2, "{}: {}".format(status, message))

    os.environ.pop("SYNCPLAY_UPDATE_FORCE_INSTALL", None)
    if updater.getInstallMode() != "frozen":
        status2, message2, url2, manifest2b = updater.checkForUpdate(config, userInitiated=True)
        check("check-only mode offers release page instead of install",
              status2 == "updateavailale" and manifest2b is None and url2, "{} {}".format(status2, url2))
    os.environ["SYNCPLAY_UPDATE_FORCE_INSTALL"] = "1"

    updater.skipRelease(2)
    statusSkip = updater.checkForUpdate(config, userInitiated=False)[0]
    statusManual = updater.checkForUpdate(config, userInitiated=True)[0]
    check("skipped release hides from auto check only",
          statusSkip == "uptodate" and statusManual == "updateavailale")

    meta = updater.downloadAndStage(manifest, config)
    currentInit = os.path.join(overlayRoot, "current", "syncplay", "__init__.py")
    check("stage promotes overlay to current/", meta["fork_release"] == 2 and os.path.isfile(currentInit))
    check("staging cleared the skip", "skippedRelease" not in updater.loadState())
    updater.downloadAndStage(manifest, config)
    check("restage rotates current to previous/",
          os.path.isfile(os.path.join(overlayRoot, "previous", "syncplay", "__init__.py")))

    # key policy
    configCustom = {"updateRepo": "testowner/testrepo", "updateRepoKey": ""}
    ok, detail = raises(lambda: updater.downloadAndStage(manifest, configCustom), updater.UpdateKeyNotPinnedError)
    check("custom repo without pin demands trust flow", ok, detail)
    updater.pinKey("testowner/testrepo", otherPub)
    ok, _ = raises(lambda: updater.downloadAndStage(manifest, configCustom), updater.UpdateError)
    check("wrong pinned key fails verification", ok)
    updater.pinKey("testowner/testrepo", testPub)
    try:
        updater.downloadAndStage(manifest, configCustom)
        check("correctly pinned custom repo installs", True)
    except Exception as e:
        check("correctly pinned custom repo installs", False, str(e))

    goodZip = served["zip2"]
    served["zip2"] = goodZip[:-2] + b"xx"
    ok, _ = raises(lambda: updater.downloadAndStage(manifest, config), updater.UpdateError)
    check("tampered zip is refused", ok)
    served["zip2"] = goodZip

    # min_base gate: publish an r3 that needs base r3
    manifest3 = dict(manifest2)
    manifest3.update({"fork_release": 3, "min_base": 3, "filename": "syncplay-overlay-r3.zip"})
    manifestHolder = dict(manifest2)
    manifest2.clear(); manifest2.update(manifest3)  # handler serves manifest2 by reference
    served["releases"] = [{"draft": False, "assets": [
        {"name": "syncplay-overlay-r3.manifest.json", "browser_download_url": baseUrl + "/assets/manifest2"},
        {"name": "syncplay-overlay-r3.zip", "browser_download_url": baseUrl + "/assets/zip2"},
    ]}]
    status3, message3, url3, manifest3b = updater.checkForUpdate(config, userInitiated=True)
    check("min_base above running base degrades to full-install offer",
          status3 == "updateavailale" and manifest3b is None and url3, "{}: {}".format(status3, message3))
    manifest2.clear(); manifest2.update(manifestHolder)
    served["releases"] = releasesJson(baseUrl)

    # autoUpdate off: still the fork's own release page, never an install and never syncplay.pl
    manualConfig = dict(config)
    manualConfig['autoUpdate'] = False
    statusM, messageM, urlM, manifestM = updater.checkForUpdate(manualConfig, userInitiated=True)
    check("autoUpdate off reports the release without offering an install",
          statusM == "updateavailale" and manifestM is None
          and urlM == updater.getReleasePageUrl(constants.UPDATE_DEFAULT_REPO),
          "{}: {} -> {}".format(statusM, messageM, urlM))
    check("autoUpdate off never points at upstream syncplay.pl",
          "syncplay.pl" not in (urlM or ""), urlM)

    # --- bootstrap lifecycle in subprocesses against the staged r2 overlay ---
    driverPath = os.path.join(tmp, "driver.py")
    with open(driverPath, "w", encoding="utf-8") as f:
        f.write("""
import importlib.util, json, os, sys
repo = sys.argv[1]
sys.path.insert(0, repo)
spec = importlib.util.spec_from_file_location("syncplay_entry", os.path.join(repo, "syncplayClient.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.applyOverlay()
import syncplay
print(json.dumps({"release": syncplay.fork_release,
                  "base": os.environ.get("SYNCPLAY_BASE_FORK_RELEASE"),
                  "quarantined": os.environ.get("SYNCPLAY_OVERLAY_QUARANTINED")}))
""")

    def bootOnce():
        bootEnv = dict(os.environ)
        bootEnv["SYNCPLAY_OVERLAY_ROOT"] = overlayRoot
        bootEnv.pop("SYNCPLAY_BASE_FORK_RELEASE", None)
        bootEnv.pop("SYNCPLAY_OVERLAY_QUARANTINED", None)
        out = subprocess.run([sys.executable, driverPath, REPO_ROOT], env=bootEnv,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    markerPath = os.path.join(overlayRoot, updater.CRASH_MARKER_FILENAME)
    boot1 = bootOnce()
    check("bootstrap applies staged r2", boot1["release"] == 2 and boot1["base"] == "1", str(boot1))
    check("bootstrap wrote crash marker", open(markerPath).read().strip() == "1")
    updater.markStartupSuccessful()
    check("startup-ok clears the marker", not os.path.isfile(markerPath))
    bootOnce()   # marker -> 1 again
    boot3 = bootOnce()  # marker was 1 -> still applies, marker -> 2
    check("marker below threshold still applies", boot3["release"] == 2, str(boot3))
    boot4 = bootOnce()  # marker at threshold -> quarantine, boot base
    check("marker at threshold quarantines and boots base",
          boot4["release"] == 1 and boot4["quarantined"] == "2", str(boot4))
    check("quarantined overlay moved aside",
          not os.path.isdir(os.path.join(overlayRoot, "current"))
          and os.path.isdir(os.path.join(overlayRoot, updater.QUARANTINE_DIR)))

    server.shutdown()

    # =====================================================================================
    # Section C: config plumbing + offscreen GUI
    # =====================================================================================

    from syncplay.ui.ConfigurationGetter import ConfigurationGetter as ClientCG
    cg = ClientCG()
    check("config defaults", cg._config.get("autoUpdate") is True
          and cg._config.get("autoInstallUpdates") is False
          and cg._config.get("updateRepo") == constants.UPDATE_DEFAULT_REPO
          and cg._config.get("updateRepoKey") == "")
    check("ini general section carries the keys",
          all(k in cg._iniStructure["general"] for k in ("autoUpdate", "autoInstallUpdates", "updateRepo", "updateRepoKey")))
    check("booleans registered", "autoUpdate" in cg._boolean and "autoInstallUpdates" in cg._boolean)
    cg._overrideConfigWithArgs(types.SimpleNamespace(no_auto_update=True, update_repo="someone/fork",
                                                     auto_install_updates=True))
    check("CLI mappings", cg._config["autoUpdate"] is False and cg._config["updateRepo"] == "someone/fork"
          and cg._config["autoInstallUpdates"] is True)

    # the GUI must not reach the upstream syncplay.pl channel any more
    guiSource = open(os.path.join(REPO_ROOT, "syncplay", "ui", "gui.py"), encoding="utf-8").read()
    check("gui never calls the upstream version check",
          "_syncplayClient.checkForUpdate(" not in guiSource)
    check("gui never offers the upstream download URL",
          "SYNCPLAY_DOWNLOAD_URL" not in guiSource)

    # every update-* message key referenced in code exists in English
    from syncplay.messages_en import en
    referenced = set()
    for rel in ("syncplay/updater.py", "syncplay/ui/gui.py", "syncplay/ui/GuiConfiguration.py", "syncplay/client.py"):
        src = open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
        referenced.update(re.findall(r'getMessage\("((?:update|autoupdate|autoinstallupdates|updaterepo|updates)[a-z0-9\-]*)"\)', src))
    missing = [k for k in referenced if k not in en]
    check("all referenced i18n keys exist", not missing, str(missing))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    gui_ok, gui_detail = False, ""
    try:
        from syncplay.vendor.Qt import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])
        from syncplay.ui.GuiConfiguration import ConfigDialog
        cg2 = ClientCG()
        cfg = dict(cg2._config)
        cfg.update({"adminPassword": None, "debug": False, "host": "localhost:8999", "name": "t",
                    "room": "r", "password": None, "playerPath": "", "playerArgs": [],
                    "publicServers": []})
        dlg = ConfigDialog(cfg, [], None, dict(cfg))
        gui_ok = (dlg.autoupdateCheckbox.objectName() == "autoUpdate"
                  and dlg.autoupdateCheckbox.isChecked()
                  and dlg.autoinstallupdatesCheckbox.objectName() == "autoInstallUpdates"
                  and not dlg.autoinstallupdatesCheckbox.isChecked()
                  and dlg.updaterepoTextbox.text() == constants.UPDATE_DEFAULT_REPO
                  and dlg.subitems.get("autoUpdate") == ["autoInstallUpdates", "updateRepo"]
                  and "base r" in dlg.updateStatusLabel.text()
                  and dlg.updateCheckButton.isEnabled())
        gui_detail = "widgets live: status={!r}".format(dlg.updateStatusLabel.text())
        # save round-trip incl. repo validation revert
        dlg.updaterepoTextbox.setText("someone/else")
        dlg.processWidget(dlg, lambda w: dlg.saveValues(w))
        gui_ok = gui_ok and dlg.config["updateRepo"] == "someone/else"
        gui_ok = gui_ok and not updater.validateRepo("bad repo!")
    except Exception as e:
        import traceback
        gui_detail = "offscreen construction failed: {}: {}".format(type(e).__name__, e)
    check("GUI: Updates group constructs and binds", gui_ok, gui_detail)

    constants.UPDATE_DEFAULT_REPO_PUBKEY = realDefaultKey
    for envKey in ("SYNCPLAY_UPDATE_API_BASE", "SYNCPLAY_OVERLAY_ROOT", "SYNCPLAY_UPDATE_FORCE_INSTALL"):
        os.environ.pop(envKey, None)

fails = [r for r in RESULTS if not r[1]]
print("\n===== UPDATER SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
for n, ok, d in fails:
    print("  FAILED: {} {}".format(n, d))
sys.exit(1 if fails else 0)
