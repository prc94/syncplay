"""Unit suite for ci/build-overlay.py (overlay update packaging).

Validates the release tooling for the auto-update design (docs/auto-update.md): staged
contents, in-zip overlay.json, manifest integrity, byte-for-byte reproducibility, and Ed25519
signing when the cryptography package is available.
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
import base64
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile

BUILD_SCRIPT = os.path.join(REPO_ROOT, "ci", "build-overlay.py")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Overlay :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))

def runBuild(outDir, extraArgs=(), env=None):
    fullEnv = dict(os.environ)
    fullEnv.pop("OVERLAY_SIGNING_KEY", None)
    if env:
        fullEnv.update(env)
    return subprocess.run(
        [sys.executable, BUILD_SCRIPT, "--out", outDir] + list(extraArgs),
        cwd=REPO_ROOT, env=fullEnv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

with open(os.path.join(REPO_ROOT, "syncplay", "__init__.py"), encoding="utf-8") as f:
    initSource = f.read()
forkReleaseMatch = re.search(r"^fork_release\s*=\s*(\d+)", initSource, re.MULTILINE)
check("fork_release declared in syncplay/__init__.py", forkReleaseMatch is not None)
forkRelease = int(forkReleaseMatch.group(1)) if forkReleaseMatch else -1

with tempfile.TemporaryDirectory(prefix="suite-overlay-") as tmp:
    # --- unsigned build ---
    out1 = os.path.join(tmp, "out1")
    proc = runBuild(out1, ["--allow-unsigned"])
    check("unsigned build succeeds", proc.returncode == 0, proc.stdout[-500:] if proc.returncode else "")

    zipName = "syncplay-overlay-r{}.zip".format(forkRelease)
    zipPath = os.path.join(out1, zipName)
    manifestPath = os.path.join(out1, "syncplay-overlay-r{}.manifest.json".format(forkRelease))
    check("zip named after fork_release", os.path.isfile(zipPath), zipName)
    check("manifest emitted", os.path.isfile(manifestPath))

    names = []
    overlayMeta = {}
    if os.path.isfile(zipPath):
        with zipfile.ZipFile(zipPath) as zf:
            names = zf.namelist()
            if "overlay.json" in names:
                overlayMeta = json.loads(zf.read("overlay.json").decode("utf-8"))
    check("zip contains syncplay/__init__.py", "syncplay/__init__.py" in names)
    check("zip contains the lua half", "syncplay/resources/syncplayintf.lua" in names)
    check("zip contains overlay.json at root", "overlay.json" in names)
    check("no bytecode or __pycache__ in zip",
          not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))])
    check("all paths use forward slashes", not [n for n in names if "\\" in n])

    check("overlay.json fork_release matches", overlayMeta.get("fork_release") == forkRelease)
    minBaseFile = os.path.join(REPO_ROOT, "ci", "overlay-min-base")
    declaredMinBase = None
    with open(minBaseFile, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                declaredMinBase = int(line)
                break
    check("overlay.json min_base matches ci/overlay-min-base", overlayMeta.get("min_base") == declaredMinBase)

    manifest = {}
    if os.path.isfile(manifestPath):
        with open(manifestPath, encoding="utf-8") as f:
            manifest = json.load(f)
    zipBytes = open(zipPath, "rb").read() if os.path.isfile(zipPath) else b""
    check("manifest sha256 matches zip", manifest.get("sha256") == hashlib.sha256(zipBytes).hexdigest())
    check("manifest size matches zip", manifest.get("size") == len(zipBytes))
    check("manifest mirrors overlay.json versions",
          manifest.get("fork_release") == overlayMeta.get("fork_release")
          and manifest.get("min_base") == overlayMeta.get("min_base"))
    check("unsigned manifest has null signature", manifest.get("ed25519_signature") is None)

    # --- reproducibility ---
    out2 = os.path.join(tmp, "out2")
    proc2 = runBuild(out2, ["--allow-unsigned"])
    rebuiltBytes = b""
    rebuiltPath = os.path.join(out2, zipName)
    if proc2.returncode == 0 and os.path.isfile(rebuiltPath):
        rebuiltBytes = open(rebuiltPath, "rb").read()
    check("rebuild is byte-identical", rebuiltBytes == zipBytes and len(zipBytes) > 0)

    # --- refuses to build without a key unless --allow-unsigned ---
    procNoKey = runBuild(os.path.join(tmp, "out3"))
    check("refuses unsigned build without --allow-unsigned",
          procNoKey.returncode != 0 and "signing key" in procNoKey.stdout)

    # --- min_base sanity gate ---
    procBadBase = runBuild(os.path.join(tmp, "out4"), ["--allow-unsigned", "--min-base", str(forkRelease + 1)])
    check("rejects min_base above fork_release", procBadBase.returncode != 0)

    # --- signing round-trip (skipped when cryptography is unavailable) ---
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
        haveCrypto = True
    except ImportError:
        haveCrypto = False
        print("[SKIP] Overlay :: signing round-trip (cryptography not installed)")
    if haveCrypto:
        key = Ed25519PrivateKey.generate()
        privB64 = base64.b64encode(
            key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode("ascii")
        outSigned = os.path.join(tmp, "signed")
        procSigned = runBuild(outSigned, env={"OVERLAY_SIGNING_KEY": privB64})
        check("signed build succeeds", procSigned.returncode == 0,
              procSigned.stdout[-500:] if procSigned.returncode else "")
        signedManifestPath = os.path.join(outSigned, "syncplay-overlay-r{}.manifest.json".format(forkRelease))
        signedManifest = {}
        if os.path.isfile(signedManifestPath):
            with open(signedManifestPath, encoding="utf-8") as f:
                signedManifest = json.load(f)
        signedZipBytes = b""
        signedZipPath = os.path.join(outSigned, zipName)
        if os.path.isfile(signedZipPath):
            signedZipBytes = open(signedZipPath, "rb").read()
        check("manifest carries the public key",
              signedManifest.get("ed25519_public_key") == base64.b64encode(
                  key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii"))
        signatureOk = False
        try:
            key.public_key().verify(base64.b64decode(signedManifest.get("ed25519_signature") or ""), signedZipBytes)
            signatureOk = True
        except Exception:
            pass
        check("signature verifies over the zip bytes", signatureOk and len(signedZipBytes) > 0)
        tamperOk = True
        try:
            key.public_key().verify(base64.b64decode(signedManifest.get("ed25519_signature") or ""),
                                    signedZipBytes + b"x")
            tamperOk = False
        except Exception:
            pass
        check("tampered zip fails verification", tamperOk)

fails = [r for r in RESULTS if not r[1]]
print("\n===== OVERLAY SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
sys.exit(1 if fails else 0)
