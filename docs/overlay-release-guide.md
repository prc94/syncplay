# Overlay release guide (maintainers)

How to decide whether a change can ship as an **overlay update** (small signed zip of the
`syncplay/` package, auto-installed by clients — see `docs/auto-update.md`) or needs a **full
release** (installers/bundles via `.github/workflows/build.yml`), and how to cut an overlay
release with `ci/build-overlay.py`.

The rule of thumb: **an overlay can replace code the frozen base *loads*, never anything the
frozen base *is*.** The base is: the Python interpreter, every third-party library frozen at
build time, the exe/app stubs (including the overlay bootstrap), and everything the installer
itself does to the system.

## Decision checklist

Go through this for the diff since the last release. Any ❌ hit ⇒ full release (overlay may
still ship alongside it for whatever parts are overlay-safe, but a full build must be published
and users prompted toward it).

### ✅ Overlay-distributable

- Any pure-Python change under `syncplay/**` — client, server, protocols, players, ui,
  `messages_*.py`, `constants.py`, `syncplay/vendor/` (it is pure Python and ships inside the
  package archive).
- `syncplay/resources/**` consumed **at runtime** through `utils.findResourcePath` /
  `resourcespath` — `syncplayintf.lua`, runtime images, `.rtf`/`.html` shown in the client.
- `fork_release` / version bumps in `syncplay/__init__.py`.
- Everything the standard fork feature recipe touches (CLAUDE.md checklist items 1–8) — by
  construction, fork features are exactly this surface.

### ❌ Full release required

1. **New or upgraded third-party dependency** — any change to `requirements*.txt`, or a new
   `import` of a third-party package, even one that "happens to be installed" on your machine.
   The frozen base only contains what was frozen.
2. **New imports outside `syncplay.*` that the frozen base may not contain** — py2exe/py2app
   bundle only the modules the freezer's dependency walk saw *at build time*. A new
   `import xml.dom.minidom` or `from PySide6 import QtSvg` that no frozen module previously
   imported will `ImportError` on frozen builds even though it is "stdlib"/"already shipped".
   When in doubt: grep the diff for new `import`/`from … import` statements and check them
   against the base's bundled-module list (see "Base module manifests" below), or test the
   overlay against a real frozen build. If the import is genuinely needed and missing from old
   bases: full release, and bump `min_base` for subsequent overlays that rely on it.
3. **Python syntax or stdlib behavior newer than the oldest supported base interpreter.** The
   overlay runs on whatever interpreter each base froze — write for the oldest one still allowed
   by `min_base` (record interpreter versions in the base table below).
4. **Entry stubs and the bootstrap** — `syncplayClient.py`, `syncplayServer.py`, and the overlay
   bootstrap logic itself are compiled into the frozen executables. Overlays cannot touch them.
   This is why the bootstrap must stay minimal.
5. **Packaging/installer changes** — `buildPy2exe.py`, `buildPy2app.py`, NSIS template, `ci/*.sh`,
   icons/file associations/registry keys, `.desktop` files, `Info.plist`, Dockerfile.
6. **Binary components** — Qt/PySide, Twisted, cryptography, certifi upgrades; anything with a
   C extension.
7. **Resources consumed by the installer or OS** (installer icons, license shown by NSIS) rather
   than loaded by running client code.

### Grey areas

- **Server-only changes**: they ride along in the overlay (the archive is the whole package) and
  that is harmless, but deployed servers (deb/docker/systemd) do **not** consume overlays —
  server updates are a separate, out-of-scope channel. Don't count "servers get it too" as
  shipped.
- **A change that *works* on new bases but degrades on old ones** (e.g. uses a module only newer
  bases bundle, behind a try/except): allowed without a `min_base` bump only if the degraded
  path is genuinely acceptable; say so in the release notes.

## `min_base` policy

`ci/overlay-min-base` (checked in, reviewed like code) holds the minimum base `fork_release`
the *next* overlay supports. Bump it when overlay code starts **requiring** something only newer
bases provide (a bundled module, a bootstrap behavior, a dependency version). Never lower it.
Clients whose base is older than `min_base` get the "new full installation required" flow
instead of the overlay — so every `min_base` bump should coincide with a full release users can
move to.

Keep a base table here as full releases ship:

| base `fork_release` | full build | interpreter (win/mac) | notes |
|---|---|---|---|
| 1 | *(first bootstrap-capable release — pending)* | | first base that can consume overlays |

### Base module manifests (planned CI guard)

When cutting a full release, dump the frozen module list (py2exe's compile-time module set /
`library.zip` listing) to `ci/base-manifests/base-r<N>.txt`. A CI step can then verify that
every import in the overlay resolves against the manifest of base `min_base` — turning
checklist item 2 from folklore into a failing check. Until that exists, item 2 is a manual
review step: treat every new import line in a diff as suspect.

## Cutting an overlay release

1. Land the changes on the release branch; confirm the checklist above says ✅.
2. Bump `fork_release` in `syncplay/__init__.py` (monotonic, never reuse). Adjust
   `ci/overlay-min-base` if this release starts requiring a newer base.
3. Run the suites: `python3 tests/run_all.py`.
4. Build locally to sanity-check: `python3 ci/build-overlay.py --allow-unsigned --out /tmp/ovl`
   — inspect the printed summary (file count, size, versions).
5. Tag and push: `git tag overlay-r<N> && git push origin overlay-r<N>` — the workflow below
   builds, signs, and attaches the assets to a GitHub release. Clients see it on their next
   check.

Release assets per overlay release (names are load-bearing — the client looks for them):

- `syncplay-overlay-r<N>.zip` — the package archive (`syncplay/` + `overlay.json` at zip root)
- `syncplay-overlay-r<N>.manifest.json` — metadata + sha256 + Ed25519 signature + public key

## Signing keys

- Generate once: `python3 ci/build-overlay.py --generate-key` → prints a base64 private key and
  the matching public key.
- Private key → repository secret **`OVERLAY_SIGNING_KEY`** (never committed; treat leakage as
  full compromise — rotating it requires a full release, since the pinned public key ships in
  `constants.py`).
- Public key → `constants.UPDATE_DEFAULT_REPO_PUBKEY` (phase-2 implementation) and the manifest
  (for other operators' trust-on-first-use flow).
- `--allow-unsigned` exists for local testing only; the client must refuse unsigned overlays.

## GitHub Actions integration

Add alongside `build.yml` (only fires on `overlay-r*` tags, so it is inert until one is pushed):

```yaml
name: Overlay release
on:
  push:
    tags: ['overlay-r*']

jobs:
  overlay:
    runs-on: ubuntu-latest
    permissions:
      contents: write        # create the release + upload assets
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.8'   # keep == oldest interpreter in the base table
      - run: pip install cryptography
      - name: Build and sign overlay
        run: python3 ci/build-overlay.py --out dist/overlay
        env:
          OVERLAY_SIGNING_KEY: ${{ secrets.OVERLAY_SIGNING_KEY }}
      - name: Attach to release
        uses: softprops/action-gh-release@v2
        with:
          files: dist/overlay/*
          generate_release_notes: true
```

The `python-version` pin doubles as a compile gate: `build-overlay.py` byte-compiles every file,
so syntax newer than the oldest supported base interpreter fails the build instead of failing on
users' machines.

## Full releases

A full release (installers via `build.yml`) additionally: bumps `fork_release` like any release,
adds a row to the base table above, dumps a base module manifest (once that guard exists), and —
because the stub/bootstrap or dependencies changed — is the moment to raise
`ci/overlay-min-base` if pending overlay work needs the new base.
