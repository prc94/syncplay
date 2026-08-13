"""Shared baseline-diffing machinery for the ruff and luacheck gates.

Why a baseline instead of "must be zero": this repo is a fork that merges from upstream, and
every one of the findings that existed when these gates were added lives in *upstream* code
(ruff: 50, luacheck: 32). Fixing them would mean editing files upstream also edits, turning
each merge into conflict resolution for zero correctness gain. So the known set is recorded
here and the gate fails only on findings that are *not* in it — which still covers new fork
code written inside shared modules like client.py, server.py, protocols.py and syncplayintf.lua,
where most fork code actually lives.

Fingerprints deliberately carry no line numbers, so a finding does not "move" when unrelated
code is inserted above it. They are compared as a multiset, so a second copy of an
already-baselined finding is still reported as new.
"""
import collections
import os
import re
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASELINE_HEADER = """\
# {tool} baseline — findings that already existed in UPSTREAM code.
#
# DO NOT "fix" these. They live in files upstream also maintains; changing them would conflict
# on every upstream merge. They are recorded so the gate can fail on genuinely new findings.
#
# One line per occurrence: <path>|<code>|<symbol>. No line numbers, so entries survive edits
# above them. Regenerate after an upstream merge with:
#     python3 tests/{suite} --update-baseline
# and review the diff — entries appearing for fork-authored code are a real signal, not noise.
"""


class ToolMissing(Exception):
    """Raised when the linter binary is not installed, so callers can skip rather than fail."""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plainLines(text):
    """Strip SGR escapes. Both linters colour their output whenever they believe a terminal is
    attached, and ruff in particular ignores NO_COLOR here, so this runs regardless of flags."""
    return [_ANSI.sub("", line) for line in text.splitlines()]


def _firstQuoted(message, quoteChars):
    """First quoted identifier in a linter message.

    Taking only the *first* one matters: messages like "Redefinition of unused `Qt` from line 21"
    and "value assigned to 'rowsAdded' is overwritten on line 407" embed line numbers further in,
    which would make the fingerprint move whenever unrelated code shifted.
    """
    for q in quoteChars:
        match = re.search(re.escape(q[0]) + r"(.+?)" + re.escape(q[1]), message)
        if match:
            return match.group(1)
    return re.sub(r"\d+", "N", message).strip()  # no symbol in the message; digits stripped


def runRuff():
    """-> list of "<path>|<code>|<symbol>" fingerprints, plus the human-readable lines."""
    try:
        proc = subprocess.run(["ruff", "check", "--no-cache", "--color", "never",
                               "--output-format", "concise", "."],
                              cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, env=dict(os.environ, NO_COLOR="1"))
    except FileNotFoundError:
        raise ToolMissing("ruff")
    findings, human = [], []
    for line in _plainLines(proc.stdout):
        # path:line:col: CODE message
        match = re.match(r"^(\S+?):(\d+):(\d+): ([A-Z]+\d+) (.*)$", line)
        if not match:
            continue
        path, lineNo, _col, code, message = match.groups()
        findings.append("{}|{}|{}".format(path, code, _firstQuoted(message, ["``"])))
        human.append("{}:{}: {} {}".format(path, lineNo, code, message))
    return findings, human


def runLuacheck(target="syncplay/resources/syncplayintf.lua"):
    """-> list of "<path>|<code>|<symbol>" fingerprints, plus the human-readable lines."""
    try:
        proc = subprocess.run(["luacheck", "--no-cache", "--formatter", "plain", "--codes",
                               "--no-color", target],
                              cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    except FileNotFoundError:
        raise ToolMissing("luacheck")
    findings, human = [], []
    for line in _plainLines(proc.stdout):
        # path:line:col: (Wxxx) message
        match = re.match(r"^(\S+?):(\d+):(\d+): \((\w+)\) (.*)$", line)
        if not match:
            continue
        path, lineNo, _col, code, message = match.groups()
        findings.append("{}|{}|{}".format(path, code, _firstQuoted(message, ["''", '""'])))
        human.append("{}:{}: ({}) {}".format(path, lineNo, code, message))
    return findings, human


def loadBaseline(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [l.strip() for l in handle if l.strip() and not l.startswith("#")]


def writeBaseline(path, fingerprints, tool, suite):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(BASELINE_HEADER.format(tool=tool, suite=suite))
        for fingerprint in sorted(fingerprints):
            handle.write(fingerprint + "\n")


def diffAgainstBaseline(findings, baseline):
    """-> (new, stale) as sorted lists of fingerprints, compared as multisets."""
    current, known = collections.Counter(findings), collections.Counter(baseline)
    new = sorted((current - known).elements())
    stale = sorted((known - current).elements())
    return new, stale


def humanFor(fingerprint, findings, human):
    """Best-effort file:line for a fingerprint, for a readable failure message."""
    for candidate, text in zip(findings, human):
        if candidate == fingerprint:
            return text
    return fingerprint
