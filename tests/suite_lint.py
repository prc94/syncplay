"""Static-analysis gate for Python code (ruff, pyflakes rules only).

Catches the class of bug the runtime tests structurally cannot: an undefined name or a silently
shadowing redefinition sitting in a branch that only executes under a specific room state
(admin-locked, room-empty cleanup, non-controller revert). Those pass every suite here and then
surface as a server traceback mid-session.

Fails only on findings absent from tests/lint_baseline_ruff.txt — see tests/lint_common.py for
why a baseline rather than zero-tolerance. Skips cleanly when ruff is not installed, matching
how suite_updater.py handles a missing Qt binding.

Usage: python3 tests/suite_lint.py [--update-baseline]
"""
import os
import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)  # import the repo's syncplay, not any system-installed copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lint_common import (ToolMissing, diffAgainstBaseline, humanFor, loadBaseline, runRuff,
                         writeBaseline)

BASELINE = os.path.join(REPO_ROOT, "tests", "lint_baseline_ruff.txt")

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[{}] Lint :: {} {}".format("PASS" if cond else "FAIL", name, ("- " + detail) if detail else ""))


def main():
    update = "--update-baseline" in sys.argv
    try:
        findings, human = runRuff()
    except ToolMissing:
        print("[SKIP] Lint :: ruff not installed - install it to enable the Python static-analysis gate")
        print("\n===== LINT SUMMARY: 0 checks, 0 failed (skipped) =====")
        return 0

    if update:
        writeBaseline(BASELINE, findings, "ruff", "suite_lint.py")
        print("Wrote {} entries to {}".format(len(findings), os.path.relpath(BASELINE, REPO_ROOT)))
        return 0

    new, stale = diffAgainstBaseline(findings, loadBaseline(BASELINE))

    check("no new ruff findings", not new,
          "{} new".format(len(new)) if new else "{} findings, all baselined".format(len(findings)))
    for fingerprint in new[:20]:
        print("    NEW: {}".format(humanFor(fingerprint, findings, human)))

    # tests/ and ci/ are fork-authored and do not exist upstream, so nothing there has any excuse
    # to be baselined. Enforced separately so a stale baseline can never quietly cover fork code.
    forkOwned = [f for f in loadBaseline(BASELINE)
                 if f.startswith("tests/") or f.startswith("ci/")]
    check("no fork-owned paths in the baseline", not forkOwned,
          ", ".join(forkOwned[:5]) if forkOwned else "tests/ and ci/ are clean")

    # Informational: a stale entry means someone fixed an upstream finding or upstream did.
    # Not a failure - it just means the baseline can be regenerated.
    if stale:
        print("    note: {} baseline entries no longer occur (run --update-baseline to prune)".format(len(stale)))
        for fingerprint in stale[:10]:
            print("      stale: {}".format(fingerprint))

    fails = [r for r in RESULTS if not r[1]]
    print("\n===== LINT SUMMARY: {} checks, {} failed =====".format(len(RESULTS), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
