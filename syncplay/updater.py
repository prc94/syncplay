# coding:utf8
"""Client auto-update core (fork feature — see docs/auto-update.md).

UI-agnostic: the GUI (and later the console) drive everything through this module. The blocking
network functions (checkForUpdate, downloadAndStage) must be called off the reactor thread and
their results marshalled back with reactor.callFromThread.

The overlay root layout and the crash-marker protocol are shared with the bootstrap in
syncplayClient.py, which is frozen into the executables — neither the root resolution nor the
marker semantics may change without a full release (see docs/overlay-release-guide.md).
"""

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile

import syncplay
from syncplay import constants
from syncplay.messages import getMessage
from syncplay.utils import findWorkingDir, isWindows, isMacOS

CURRENT_DIR = "current"
PREVIOUS_DIR = "previous"
PENDING_DIR = "pending"
QUARANTINE_DIR = "quarantined"
STATE_FILENAME = "state.json"
CRASH_MARKER_FILENAME = "crash-marker"
OVERLAY_META_FILENAME = "overlay.json"


class UpdateError(Exception):
    """Raised with an already-localized message."""


class UpdateKeyNotPinnedError(UpdateError):
    """A custom repo has no pinned key yet — the caller must run the trust-on-first-use flow."""

    def __init__(self, repo, publicKey):
        UpdateError.__init__(self, getMessage("update-repo-not-trusted-error").format(repo))
        self.repo = repo
        self.publicKey = publicKey


def getInstallMode():
    """'frozen' (installers/bundles — the only mode that installs overlays), 'source', 'packaged'."""
    if getattr(sys, 'frozen', ''):
        return "frozen"
    packageParent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(packageParent, ".git")):
        return "source"
    if os.path.abspath(__file__).startswith(("/usr/", "/opt/")):
        return "packaged"
    return "source"


def canInstallOverlays():
    # SYNCPLAY_UPDATE_FORCE_INSTALL is for tests and manual smoke runs from source only
    return getInstallMode() == "frozen" or os.environ.get("SYNCPLAY_UPDATE_FORCE_INSTALL") == "1"


def getOverlayRoot():
    """Per-user writable overlay directory. Mirrored by the bootstrap — do not change lightly."""
    override = os.environ.get("SYNCPLAY_OVERLAY_ROOT")
    if override:
        return override
    if isWindows():
        workingDir = findWorkingDir()
        portable = any(os.path.isfile(os.path.join(workingDir, name)) for name in constants.CONFIG_NAMES)
        if portable and os.access(workingDir, os.W_OK):
            return os.path.join(workingDir, "overlay")
        return os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "Syncplay", "overlay")
    if isMacOS():
        return os.path.expanduser("~/Library/Application Support/Syncplay/overlay")
    xdgData = os.getenv("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(xdgData, "syncplay", "overlay")


def getRunningVersions():
    """(baseRelease, overlayRelease or None). The bootstrap exports the base via env when an
    overlay is active; otherwise the running code is the base."""
    running = getattr(syncplay, "fork_release", 0)
    baseEnv = os.environ.get("SYNCPLAY_BASE_FORK_RELEASE")
    if baseEnv is not None:
        try:
            base = int(baseEnv)
        except ValueError:
            return running, None
        if running > base:
            return base, running
    return running, None


def getRunningVersionLabel():
    base, overlay = getRunningVersions()
    if overlay is not None:
        return getMessage("update-status-running-overlay").format(base, overlay)
    return getMessage("update-status-running-base").format(base)


def getQuarantinedRelease():
    """Release number quarantined by the bootstrap this boot, or None."""
    value = os.environ.get("SYNCPLAY_OVERLAY_QUARANTINED")
    return value if value else None


# --- updater state -------------------------------------------------------------------------

def _statePath():
    return os.path.join(getOverlayRoot(), STATE_FILENAME)


def loadState():
    try:
        with open(_statePath(), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def saveState(state):
    root = getOverlayRoot()
    os.makedirs(root, exist_ok=True)
    tmpPath = _statePath() + ".tmp"
    with open(tmpPath, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmpPath, _statePath())


def markStartupSuccessful():
    """Clear the bootstrap's crash marker; call once the client is demonstrably up."""
    try:
        os.remove(os.path.join(getOverlayRoot(), CRASH_MARKER_FILENAME))
    except OSError:
        pass


# --- network -------------------------------------------------------------------------------

def _apiBase():
    return os.environ.get("SYNCPLAY_UPDATE_API_BASE", constants.UPDATE_GITHUB_API_BASE)


def _httpGetBytes(url, maxSize):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Syncplay/{} (fork updater)".format(syncplay.version),
        "Accept": "application/vnd.github+json, application/octet-stream, */*",
    })
    with urllib.request.urlopen(request, timeout=constants.UPDATE_HTTP_TIMEOUT) as response:
        data = response.read(maxSize + 1)
    if len(data) > maxSize:
        raise UpdateError(getMessage("update-download-too-large-error"))
    return data


def _httpGetJson(url, maxSize=2 * 1024 * 1024):
    return json.loads(_httpGetBytes(url, maxSize).decode("utf-8"))


def validateRepo(repo):
    return bool(re.match(constants.UPDATE_REPO_REGEX, repo or ""))


def getReleasePageUrl(repo):
    return constants.UPDATE_RELEASE_PAGE_URL.format(repo if validateRepo(repo) else constants.UPDATE_DEFAULT_REPO)


def _validateManifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("kind") != "syncplay-overlay" or manifest.get("schema") != 1:
        raise UpdateError(getMessage("update-bad-manifest-error"))
    try:
        forkRelease = int(manifest["fork_release"])
        minBase = int(manifest["min_base"])
        size = int(manifest["size"])
        sha256 = manifest["sha256"]
        filename = manifest["filename"]
    except (KeyError, TypeError, ValueError):
        raise UpdateError(getMessage("update-bad-manifest-error"))
    if (forkRelease < 1 or minBase < 1 or minBase > forkRelease
            or not (0 < size <= constants.UPDATE_MAX_DOWNLOAD_SIZE)
            or not re.match(r"^[0-9a-f]{64}$", sha256 or "")
            or filename != "syncplay-overlay-r{}.zip".format(forkRelease)):
        raise UpdateError(getMessage("update-bad-manifest-error"))


def fetchAvailableOverlay(repo):
    """Newest published overlay manifest from the repo's releases, with the zip's download URL
    attached as manifest['_zipUrl']. Returns None when the repo has no overlay releases."""
    releases = _httpGetJson(_apiBase() + constants.UPDATE_RELEASES_PATH.format(repo))
    if not isinstance(releases, list):
        raise UpdateError(getMessage("update-bad-manifest-error"))
    best = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        assets = release.get("assets") or []
        for asset in assets:
            match = re.match(constants.UPDATE_MANIFEST_ASSET_REGEX, asset.get("name", ""))
            if match and (best is None or int(match.group(1)) > best[0]):
                best = (int(match.group(1)), asset, assets)
    if best is None:
        return None
    _, manifestAsset, assets = best
    manifest = _httpGetJson(manifestAsset["browser_download_url"])
    _validateManifest(manifest)
    zipUrl = None
    for asset in assets:
        if asset.get("name") == manifest["filename"]:
            zipUrl = asset.get("browser_download_url")
    if zipUrl is None:
        raise UpdateError(getMessage("update-bad-manifest-error"))
    manifest["_zipUrl"] = zipUrl
    return manifest


def checkForUpdate(config, userInitiated=False):
    """Blocking. Returns (status, message, url, manifestOrNone) with the status strings gui.py
    already dispatches on ('uptodate' / 'updateavailale' / 'failed')."""
    repo = config.get('updateRepo') or constants.UPDATE_DEFAULT_REPO
    if not validateRepo(repo):
        return "failed", getMessage("update-invalid-repo-error").format(repo), None, None
    base, overlay = getRunningVersions()
    running = max(base, overlay or 0)
    try:
        manifest = fetchAvailableOverlay(repo)
    except UpdateError as e:
        return "failed", str(e), getReleasePageUrl(repo) if userInitiated else None, None
    except Exception as e:
        message = str(e) + "\n-----\n" + getMessage("update-check-failed-notification").format(syncplay.version)
        return "failed", message, getReleasePageUrl(repo) if userInitiated else None, None
    if manifest is None or int(manifest["fork_release"]) <= running:
        return "uptodate", getMessage("update-status-uptodate").format(getRunningVersionLabel()), None, None
    if not userInitiated and loadState().get("skippedRelease") == int(manifest["fork_release"]):
        return "uptodate", getMessage("update-status-uptodate").format(getRunningVersionLabel()), None, None
    if not config.get('autoUpdate', True):
        # In-place installs switched off: still report the fork's own releases, never send the
        # user to upstream Syncplay (that download would replace this build).
        return ("updateavailale",
                getMessage("update-available-manual-notification").format(
                    manifest["fork_release"], getRunningVersionLabel()),
                getReleasePageUrl(repo), None)
    if int(manifest["min_base"]) > base or not canInstallOverlays():
        return ("updateavailale",
                getMessage("update-needs-full-install-notification").format(manifest["fork_release"]),
                getReleasePageUrl(repo), None)
    return ("updateavailale",
            getMessage("update-available-notification").format(manifest["fork_release"], getRunningVersionLabel()),
            getReleasePageUrl(repo), manifest)


def skipRelease(forkRelease):
    state = loadState()
    state["skippedRelease"] = int(forkRelease)
    saveState(state)


# --- signature policy ----------------------------------------------------------------------

def keyFingerprint(publicKeyB64):
    digest = hashlib.sha256(base64.b64decode(publicKeyB64)).hexdigest()[:32]
    return " ".join(digest[i:i + 4] for i in range(0, len(digest), 4))


def pinKey(repo, publicKeyB64):
    """Persist a trust-on-first-use decision for a custom repo (state.json survives the session
    immediately, unlike the ini which only saves from the config dialog)."""
    state = loadState()
    state.setdefault("pinnedKeys", {})[repo] = publicKeyB64
    saveState(state)


def _expectedPublicKey(config, manifest):
    repo = config.get('updateRepo') or constants.UPDATE_DEFAULT_REPO
    if repo == constants.UPDATE_DEFAULT_REPO:
        if not constants.UPDATE_DEFAULT_REPO_PUBKEY:
            raise UpdateError(getMessage("update-no-signing-key-error"))
        return constants.UPDATE_DEFAULT_REPO_PUBKEY  # config can never override the baked-in key
    pinnedKeys = loadState().get("pinnedKeys") or {}
    pinned = pinnedKeys.get(repo) or config.get('updateRepoKey') or ""
    if not pinned:
        raise UpdateKeyNotPinnedError(repo, manifest.get("ed25519_public_key") or "")
    return pinned


def _verifySignature(zipBytes, signatureB64, publicKeyB64):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise UpdateError(getMessage("update-no-signing-key-error"))
    try:
        publicKey = Ed25519PublicKey.from_public_bytes(base64.b64decode(publicKeyB64))
        publicKey.verify(base64.b64decode(signatureB64 or ""), zipBytes)
    except (InvalidSignature, ValueError, TypeError):
        raise UpdateError(getMessage("update-bad-signature-error"))


# --- install -------------------------------------------------------------------------------

def _safeExtract(zipPath, destination):
    with zipfile.ZipFile(zipPath) as archive:
        for name in archive.namelist():
            normalized = os.path.normpath(name)
            if normalized.startswith("..") or os.path.isabs(normalized) or "\\" in name:
                raise UpdateError(getMessage("update-bad-manifest-error"))
        archive.extractall(destination)


def downloadAndStage(manifest, config):
    """Blocking; call off the reactor thread. Downloads, verifies, and promotes the overlay to
    current/. Returns the in-zip overlay metadata. Raises UpdateError (UpdateKeyNotPinnedError
    when a custom repo needs the trust flow first)."""
    base, _ = getRunningVersions()
    expectedKey = _expectedPublicKey(config, manifest)
    zipBytes = _httpGetBytes(manifest["_zipUrl"], constants.UPDATE_MAX_DOWNLOAD_SIZE)
    if len(zipBytes) != int(manifest["size"]) or hashlib.sha256(zipBytes).hexdigest() != manifest["sha256"]:
        raise UpdateError(getMessage("update-bad-signature-error"))
    _verifySignature(zipBytes, manifest.get("ed25519_signature"), expectedKey)

    root = getOverlayRoot()
    pendingDir = os.path.join(root, PENDING_DIR)
    shutil.rmtree(pendingDir, ignore_errors=True)
    os.makedirs(pendingDir)
    zipPath = os.path.join(pendingDir, manifest["filename"])
    with open(zipPath, "wb") as f:
        f.write(zipBytes)
    extractedDir = os.path.join(pendingDir, "extracted")
    _safeExtract(zipPath, extractedDir)

    metaPath = os.path.join(extractedDir, OVERLAY_META_FILENAME)
    try:
        with open(metaPath, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        raise UpdateError(getMessage("update-bad-manifest-error"))
    if (int(meta.get("fork_release", -1)) != int(manifest["fork_release"])
            or int(meta.get("min_base", 0)) > base
            or not os.path.isfile(os.path.join(extractedDir, "syncplay", "__init__.py"))):
        raise UpdateError(getMessage("update-bad-manifest-error"))

    currentDir = os.path.join(root, CURRENT_DIR)
    previousDir = os.path.join(root, PREVIOUS_DIR)
    shutil.rmtree(previousDir, ignore_errors=True)
    if os.path.isdir(currentDir):
        os.replace(currentDir, previousDir)
    os.replace(extractedDir, currentDir)
    shutil.rmtree(pendingDir, ignore_errors=True)

    markStartupSuccessful()  # a fresh overlay gets a clean slate
    state = loadState()
    state.pop("skippedRelease", None)
    saveState(state)
    return meta
