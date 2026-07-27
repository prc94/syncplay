#!/usr/bin/env python3

import sys

# libpath

try:
    if (sys.version_info.major != 3) or (sys.version_info.minor < 4):
        raise Exception("You must run Syncplay with Python 3.4 or newer!")
except AttributeError:
    import warnings
    warnings.warn("You must run Syncplay with Python 3.4 or newer!")


def _overlayDecision(baseRelease, meta, markedBoots, quarantineThreshold):
    """Fork overlay bootstrap decision (pure — imported by tests/suite_updater.py).
    Returns 'apply', 'quarantine', or 'base'."""
    try:
        if not isinstance(meta, dict):
            return "base"
        if int(markedBoots) >= int(quarantineThreshold):
            return "quarantine"
        if int(meta.get("fork_release", 0)) > int(baseRelease) >= int(meta.get("min_base", 0)) > 0:
            return "apply"
        return "base"
    except Exception:
        return "base"


def applyOverlay():
    """Fork auto-update bootstrap (docs/auto-update.md): if a verified overlay staged by
    syncplay/updater.py is strictly newer than the frozen base, load the client from it.
    This function is frozen into the executables — keep it tiny, and never let it raise."""
    try:
        import json
        import os
        import shutil
        import syncplay
        from syncplay import constants, updater

        baseRelease = getattr(syncplay, "fork_release", 0)
        overlayRoot = updater.getOverlayRoot()
        currentDir = os.path.join(overlayRoot, updater.CURRENT_DIR)
        metaPath = os.path.join(currentDir, updater.OVERLAY_META_FILENAME)
        if not os.path.isfile(metaPath):
            return
        with open(metaPath, encoding="utf-8") as f:
            meta = json.load(f)

        markerPath = os.path.join(overlayRoot, updater.CRASH_MARKER_FILENAME)
        markedBoots = 0
        if os.path.isfile(markerPath):
            try:
                with open(markerPath, encoding="utf-8") as f:
                    markedBoots = int(f.read().strip() or 0)
            except (OSError, ValueError):
                markedBoots = 1

        decision = _overlayDecision(baseRelease, meta, markedBoots,
                                    constants.UPDATE_CRASH_QUARANTINE_THRESHOLD)
        if decision == "quarantine":
            quarantineDir = os.path.join(overlayRoot, updater.QUARANTINE_DIR)
            shutil.rmtree(quarantineDir, ignore_errors=True)
            os.replace(currentDir, quarantineDir)
            os.remove(markerPath)
            os.environ["SYNCPLAY_OVERLAY_QUARANTINED"] = str(meta.get("fork_release", "?"))
            return
        if decision != "apply":
            return

        with open(markerPath, "w", encoding="utf-8") as f:
            f.write(str(markedBoots + 1))
        for moduleName in [m for m in sys.modules if m == "syncplay" or m.startswith("syncplay.")]:
            del sys.modules[moduleName]
        sys.path.insert(0, currentDir)
        os.environ["SYNCPLAY_BASE_FORK_RELEASE"] = str(baseRelease)
    except Exception:
        pass  # any bootstrap problem means: boot the shipped base, never brick the client


if __name__ == '__main__':
    applyOverlay()
    from syncplay import ep_client
    ep_client.main()
