#!/usr/bin/env python3
"""Run every fork test suite and summarize.

Usage: python3 tests/run_all.py [--unit-only | --e2e-only]

Unit suites exercise server/client/protocol classes directly; E2E suites boot real
syncplayServer.py processes and drive them over sockets (they need free ports 19001-19061
and take ~2 minutes). The lua suite is static analysis + Python-ported simulations.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

UNIT = ["suite_unit.py", "suite_cap.py", "suite_osd.py", "suite_admin.py", "suite_tracks.py", "suite_domains.py", "suite_afk.py", "suite_joinguard.py", "suite_joinprop.py", "suite_lua.py", "suite_overlay.py"]
E2E = ["suite_e2e.py", "suite_e2e2.py", "suite_osd_e2e.py", "suite_admin_e2e.py", "suite_tracks_e2e.py", "suite_domains_e2e.py", "suite_afk_e2e.py", "suite_joinguard_e2e.py", "suite_joinprop_e2e.py"]

def main():
    suites = UNIT + E2E
    if "--unit-only" in sys.argv:
        suites = UNIT
    elif "--e2e-only" in sys.argv:
        suites = E2E
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")  # suite_admin constructs the real Qt dialog
    results = []
    for suite in suites:
        print("=== {} ===".format(suite), flush=True)
        proc = subprocess.run([sys.executable, os.path.join(HERE, suite)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        summary = [l for l in proc.stdout.splitlines() if "SUMMARY" in l]
        print(summary[-1] if summary else proc.stdout[-2000:])
        results.append((suite, proc.returncode))
    print("\n===== OVERALL =====")
    failed = [s for s, rc in results if rc != 0]
    for suite, rc in results:
        print("  {}  {}".format("PASS" if rc == 0 else "FAIL", suite))
    # suite_lua exits 1 only for real failures; its HEAD-vs-worktree check is informational
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
