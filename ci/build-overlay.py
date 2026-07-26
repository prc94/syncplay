#!/usr/bin/env python3
"""Build a signed Syncplay overlay update package.

Produces the release assets the auto-updater consumes (see docs/auto-update.md and
docs/overlay-release-guide.md):

    syncplay-overlay-r<N>.zip            the syncplay/ package + overlay.json, deterministic
    syncplay-overlay-r<N>.manifest.json  metadata + sha256 + Ed25519 signature + public key

Usage:
    python3 ci/build-overlay.py [--out DIR] [--min-base N] [--allow-unsigned | --key-file PATH]
    python3 ci/build-overlay.py --generate-key

Signing: the private key is 32 raw Ed25519 bytes, base64-encoded, taken from the
OVERLAY_SIGNING_KEY environment variable (CI secret) or --key-file. --allow-unsigned is for
local testing only — clients must refuse unsigned overlays.

The zip is byte-for-byte reproducible for a given tree (sorted entries, fixed timestamps and
permissions), so CI runs the build under the oldest supported base interpreter and the
byte-compile pass doubles as a syntax gate.
"""

import argparse
import base64
import hashlib
import json
import os
import py_compile
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_FILES = {".DS_Store"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
REQUIRED_STAGED_FILES = (
    "syncplay/__init__.py",
    "syncplay/client.py",
    "syncplay/server.py",
    "syncplay/resources/syncplayintf.lua",
)
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def fail(message):
    print("ERROR: {}".format(message), file=sys.stderr)
    sys.exit(1)


def parseVersionInfo(repoRoot):
    """Text-parse syncplay/__init__.py (never import it — must not depend on the build env)."""
    initPath = os.path.join(repoRoot, "syncplay", "__init__.py")
    try:
        with open(initPath, encoding="utf-8") as f:
            source = f.read()
    except OSError as e:
        fail("cannot read {}: {}".format(initPath, e))
    info = {}
    for key, pattern in (
        ("version", r"^version\s*=\s*'([^']+)'"),
        ("milestone", r"^milestone\s*=\s*'([^']+)'"),
        ("release_number", r"^release_number\s*=\s*'([^']+)'"),
        ("fork_release", r"^fork_release\s*=\s*(\d+)"),
    ):
        match = re.search(pattern, source, re.MULTILINE)
        if not match:
            fail("could not parse {} from {}".format(key, initPath))
        info[key] = match.group(1)
    info["fork_release"] = int(info["fork_release"])
    return info


def readMinBase(repoRoot):
    path = os.path.join(repoRoot, "ci", "overlay-min-base")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    return int(line)
    except (OSError, ValueError) as e:
        fail("cannot read min base from {}: {}".format(path, e))
    fail("no value found in {}".format(path))


def stagePackage(repoRoot, stagingDir):
    def ignore(directory, names):
        ignored = set()
        for name in names:
            if name in EXCLUDED_DIRS or name in EXCLUDED_FILES or name.endswith(EXCLUDED_SUFFIXES):
                ignored.add(name)
        return ignored

    shutil.copytree(os.path.join(repoRoot, "syncplay"), os.path.join(stagingDir, "syncplay"), ignore=ignore)
    for required in REQUIRED_STAGED_FILES:
        if not os.path.isfile(os.path.join(stagingDir, required)):
            fail("staged tree is missing {}".format(required))


def compileCheck(stagingDir):
    """Byte-compile every staged .py under the running interpreter; any failure aborts."""
    errors = []
    with tempfile.TemporaryDirectory(prefix="syncplay-overlay-pyc-") as pycDir:
        scratchPyc = os.path.join(pycDir, "check.pyc")
        for dirpath, _, filenames in os.walk(stagingDir):
            for filename in filenames:
                if filename.endswith(".py"):
                    path = os.path.join(dirpath, filename)
                    try:
                        py_compile.compile(path, cfile=scratchPyc, doraise=True)
                    except py_compile.PyCompileError as e:
                        errors.append(str(e))
    if errors:
        fail("byte-compile check failed:\n" + "\n".join(errors))


def writeOverlayMeta(stagingDir, versionInfo, minBase):
    # The client trusts this in-zip copy after signature verification; the manifest's copies
    # are advisory (pre-download decisions only).
    meta = {
        "fork_release": versionInfo["fork_release"],
        "min_base": minBase,
        "upstream_version": versionInfo["version"],
        "release_number": versionInfo["release_number"],
    }
    with open(os.path.join(stagingDir, "overlay.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    return meta


def buildZip(stagingDir, zipPath):
    entries = []
    for dirpath, dirnames, filenames in os.walk(stagingDir):
        dirnames.sort()
        for filename in sorted(filenames):
            fullPath = os.path.join(dirpath, filename)
            arcName = os.path.relpath(fullPath, stagingDir).replace(os.sep, "/")
            entries.append((arcName, fullPath))
    entries.sort()
    with zipfile.ZipFile(zipPath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcName, fullPath in entries:
            zinfo = zipfile.ZipInfo(arcName, date_time=ZIP_DATE_TIME)
            zinfo.external_attr = 0o644 << 16
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            with open(fullPath, "rb") as f:
                zf.writestr(zinfo, f.read(), compresslevel=9)
    return len(entries)


def extractPrivateKeyLine(text):
    """Accept either a bare base64 key or verbatim --generate-key output.

    The labelled form holds the public key too, so pick by label rather than by
    "first thing that decodes" — signing with the public half would silently
    produce signatures no client can verify.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    for index, line in enumerate(lines):
        if line.lower().startswith("private key") and index + 1 < len(lines):
            return lines[index + 1]
    return " ".join(lines)


def loadSigningKey(args):
    encoded = None
    if args.key_file:
        try:
            with open(args.key_file, encoding="utf-8") as f:
                encoded = extractPrivateKeyLine(f.read())
        except OSError as e:
            fail("cannot read key file: {}".format(e))
    else:
        encoded = os.environ.get("OVERLAY_SIGNING_KEY", "").strip() or None
    if encoded is None:
        if args.allow_unsigned:
            return None
        fail("no signing key (set OVERLAY_SIGNING_KEY or --key-file; --allow-unsigned for local testing)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        fail("signing key is not valid base64")
    if len(raw) != 32:
        fail("signing key must be 32 raw Ed25519 bytes ({} after base64 decode)".format(len(raw)))
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        fail("the 'cryptography' package is required for signing (pip install cryptography)")
    return Ed25519PrivateKey.from_private_bytes(raw)


def signZip(zipBytes, privateKey):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    signature = privateKey.sign(zipBytes)
    publicRaw = privateKey.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(signature).decode("ascii"), base64.b64encode(publicRaw).decode("ascii")


def generateKey():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
    except ImportError:
        fail("the 'cryptography' package is required (pip install cryptography)")
    key = Ed25519PrivateKey.generate()
    privateRaw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    publicRaw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    print("private key (-> OVERLAY_SIGNING_KEY secret, keep safe):")
    print("  " + base64.b64encode(privateRaw).decode("ascii"))
    print("public key  (-> constants.UPDATE_DEFAULT_REPO_PUBKEY):")
    print("  " + base64.b64encode(publicRaw).decode("ascii"))


def main():
    parser = argparse.ArgumentParser(description="Build a signed Syncplay overlay update package.")
    parser.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--out", default=os.path.join("dist", "overlay"), help="output directory")
    parser.add_argument("--min-base", type=int, default=None, help="override ci/overlay-min-base")
    parser.add_argument("--allow-unsigned", action="store_true", help="local testing only")
    parser.add_argument("--key-file", default=None, help="file with the base64 private key")
    parser.add_argument("--generate-key", action="store_true", help="generate an Ed25519 keypair and exit")
    args = parser.parse_args()

    if args.generate_key:
        generateKey()
        return

    repoRoot = os.path.abspath(args.repo_root)
    versionInfo = parseVersionInfo(repoRoot)
    minBase = args.min_base if args.min_base is not None else readMinBase(repoRoot)
    if minBase > versionInfo["fork_release"]:
        fail("min_base {} exceeds fork_release {}".format(minBase, versionInfo["fork_release"]))
    privateKey = loadSigningKey(args)

    os.makedirs(args.out, exist_ok=True)
    baseName = "syncplay-overlay-r{}".format(versionInfo["fork_release"])
    zipPath = os.path.join(args.out, baseName + ".zip")
    manifestPath = os.path.join(args.out, baseName + ".manifest.json")

    with tempfile.TemporaryDirectory(prefix="syncplay-overlay-") as stagingDir:
        stagePackage(repoRoot, stagingDir)
        compileCheck(stagingDir)
        overlayMeta = writeOverlayMeta(stagingDir, versionInfo, minBase)
        fileCount = buildZip(stagingDir, zipPath)

    with open(zipPath, "rb") as f:
        zipBytes = f.read()
    sha256 = hashlib.sha256(zipBytes).hexdigest()
    signature, publicKey = signZip(zipBytes, privateKey) if privateKey else (None, None)

    manifest = {
        "schema": 1,
        "kind": "syncplay-overlay",
        "filename": os.path.basename(zipPath),
        "size": len(zipBytes),
        "sha256": sha256,
        "ed25519_signature": signature,
        "ed25519_public_key": publicKey,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest.update(overlayMeta)
    with open(manifestPath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    print("built {} ({} files, {} bytes)".format(zipPath, fileCount, len(zipBytes)))
    print("  fork_release={fork_release} min_base={min_base} upstream={upstream_version}".format(**overlayMeta))
    print("  sha256={}".format(sha256))
    print("  signed={}".format("yes, public key {}".format(publicKey) if signature
                                else "NO (--allow-unsigned)"))
    print("  manifest: {}".format(manifestPath))


if __name__ == "__main__":
    main()
