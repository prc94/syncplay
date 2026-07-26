#!/usr/bin/env python3

"""Local fake GitHub release server for hands-on overlay auto-update testing.

Builds a signed overlay from the current checkout with a bumped ``fork_release``,
then serves it over a fake GitHub releases API so a real client can find, verify,
download and apply it without touching github.com. See docs/auto-update.md.

Signed with the production key by default (``~/syncplay-overlay-signing.key``), so
the client verifies it against the baked-in ``UPDATE_DEFAULT_REPO_PUBKEY`` exactly
like a real release -- no patching of constants, no --allow-unsigned.

    python3 tests/manual_overlay_server.py

It prints the env vars to launch the client with, then serves until Ctrl-C.
"""

import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KEY_FILE = os.path.expanduser("~/syncplay-overlay-signing.key")


def readForkRelease(repoRoot):
    with open(os.path.join(repoRoot, "syncplay", "__init__.py"), encoding="utf-8") as f:
        match = re.search(r"^fork_release\s*=\s*(\d+)", f.read(), flags=re.MULTILINE)
    if not match:
        sys.exit("could not parse fork_release from syncplay/__init__.py")
    return int(match.group(1))


def buildOverlay(repoRoot, release, minBase, keyFile, workDir):
    """Copy the package with fork_release bumped to `release` and build a signed overlay."""
    bumped = os.path.join(workDir, "repo")
    os.makedirs(bumped)
    shutil.copytree(os.path.join(repoRoot, "syncplay"), os.path.join(bumped, "syncplay"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    initPath = os.path.join(bumped, "syncplay", "__init__.py")
    with open(initPath, encoding="utf-8") as f:
        source = f.read()
    with open(initPath, "w", encoding="utf-8") as f:
        f.write(re.sub(r"^fork_release\s*=\s*\d+", "fork_release = {}".format(release),
                       source, flags=re.MULTILINE))
    outDir = os.path.join(workDir, "dist")
    command = [sys.executable, os.path.join(repoRoot, "ci", "build-overlay.py"),
               "--repo-root", bumped, "--out", outDir, "--min-base", str(minBase),
               "--key-file", keyFile]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        sys.exit("overlay build failed")
    return outDir


def makeHandler(assets):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/repos/") and path.endswith("/releases"):
                body = json.dumps(assets["releases"]).encode()
            elif path.startswith("/assets/") and path[len("/assets/"):] in assets["files"]:
                body = assets["files"][path[len("/assets/"):]]
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            sys.stderr.write("  [api] {}\n".format(fmt % args))

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", type=int, default=None,
                        help="fork_release to publish (default: checkout's + 1)")
    parser.add_argument("--min-base", type=int, default=None,
                        help="min_base for the built overlay (default: checkout's fork_release)")
    parser.add_argument("--port", type=int, default=8123, help="port for the fake API")
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE,
                        help="base64 Ed25519 private key file (default: %(default)s)")
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument("--overlay-root", default=os.path.join(tempfile.gettempdir(),
                                                               "syncplay-manual-overlay"),
                        help="overlay root to hand the client (default: %(default)s)")
    args = parser.parse_args()

    if not os.path.isfile(args.key_file):
        sys.exit("no signing key at {} -- pass --key-file, or generate one with "
                 "ci/build-overlay.py --generate-key (a throwaway key needs "
                 "constants.UPDATE_DEFAULT_REPO_PUBKEY patched to match)".format(args.key_file))

    base = readForkRelease(args.repo_root)
    release = args.release if args.release is not None else base + 1
    minBase = args.min_base if args.min_base is not None else base
    if release <= base:
        print("warning: r{} is not newer than the checkout's base r{} -- the client will "
              "correctly refuse to apply it".format(release, base))

    with tempfile.TemporaryDirectory(prefix="manual-overlay-") as workDir:
        outDir = buildOverlay(args.repo_root, release, minBase, args.key_file, workDir)
        zipName = "syncplay-overlay-r{}.zip".format(release)
        manifestName = "syncplay-overlay-r{}.manifest.json".format(release)
        files = {name: open(os.path.join(outDir, name), "rb").read()
                 for name in (zipName, manifestName)}
        baseUrl = "http://127.0.0.1:{}".format(args.port)
        assets = {"files": files, "releases": [{"draft": False, "assets": [
            {"name": name, "browser_download_url": "{}/assets/{}".format(baseUrl, name)}
            for name in (manifestName, zipName)]}]}

        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), makeHandler(assets))
        print("\nserving overlay r{} (min_base r{}) for base r{} at {}".format(
            release, minBase, base, baseUrl))
        print("\nlaunch the client with:\n")
        print("  SYNCPLAY_UPDATE_API_BASE={} \\\n  SYNCPLAY_OVERLAY_ROOT={} \\\n"
              "  SYNCPLAY_UPDATE_FORCE_INSTALL=1 \\\n  PYTHONPATH={} DISPLAY=:0 "
              "python3 syncplayClient.py --no-store\n".format(
                  baseUrl, args.overlay_root, args.repo_root))
        print("then: Misc tab -> Updates -> Check now. Ctrl-C here when done.\n")
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
