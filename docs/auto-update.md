# Client auto-update (overlay updates)

**Status: phases 1–2 implemented** (GitHub check, overlay pipeline, config-dialog UI, bootstrap,
restart); phase 3 (git-pull mode for source checkouts, console `update` command) is still
pending — on source checkouts the updater is check-only. Implementation lives in
`syncplay/updater.py` (core), `syncplayClient.py` (bootstrap), `syncplay/ui/GuiConfiguration.py`
+ `syncplay/ui/gui.py` (UI); tests in `tests/suite_updater.py`. Release tooling:
`ci/build-overlay.py`, `ci/overlay-min-base`, and the maintainer decision guide in
`docs/overlay-release-guide.md`.

Implementation notes that refine the original design:

- **Key pinning storage**: trust-on-first-use pins land in the updater's own
  `state.json` (`pinnedKeys`), which persists immediately mid-session; the `updateRepoKey`
  config field acts as a pre-seeded pin (useful for operators shipping preconfigured builds).
  The default repo still always verifies against the key baked into `constants.py`.
- **Crash guard is count-based**: the bootstrap increments a marker on every overlay boot and
  quarantines only at `UPDATE_CRASH_QUARANTINE_THRESHOLD` (2) consecutive marked boots, so a
  quickly-closed healthy client doesn't get its overlay quarantined. The client clears the
  marker `UPDATE_STARTUP_OK_DELAY` (5 s) after startup.
- **With `autoUpdate` on, the syncplay.pl check is not made**, so the public-server list
  refresh it piggybacked is skipped (the server dropdown keeps its saved entries). Turning
  `autoUpdate` off restores the stock behavior.
- **Testing hooks** (env vars, testing only): `SYNCPLAY_UPDATE_API_BASE` (fake GitHub API),
  `SYNCPLAY_OVERLAY_ROOT` (overlay root override), `SYNCPLAY_UPDATE_FORCE_INSTALL=1` (allow
  installs from a source checkout).
- **How to test it**: `python3 tests/run_all.py --unit-only` covers the whole pipeline offline
  (`tests/suite_updater.py`, `tests/suite_overlay.py`). For hands-on testing with a real client,
  `python3 tests/manual_overlay_server.py` builds a signed overlay one release ahead of the
  checkout, serves it over a fake GitHub releases API, and prints the env vars to launch the
  client with — then Misc tab → Updates → Check now.

## The model in one paragraph

The fork's client code is pure Python plus resources. Frozen builds (py2exe, py2app, AppImage)
load that code from an archive whose location is resolved through `sys.path` — so a newer copy
of the `syncplay/` package placed in a **per-user writable overlay directory** and prepended to
`sys.path` before `import syncplay` shadows the shipped code everywhere, without touching the
installation, the app bundle, or anything a package manager owns. Updates are downloaded from a
GitHub repository's releases (default: this fork), verified against an Ed25519 signature, staged
atomically, and applied by relaunching the client — seconds of downtime, after which the client
reconnects using its saved config. That is the honest version of "on the fly": hot-reloading a
live Twisted reactor + Qt application is not realistically achievable.

Two update strategies are planned; only the first is specced here:

- **Overlay (this document)** — for frozen builds (Windows installed + portable, macOS app,
  AppImage). Covers everything a normal fork release changes; see `docs/overlay-release-guide.md`
  for what it *cannot* ship.
- **Git pull (future)** — for source checkouts (a `.git` directory next to `syncplay/`):
  `git pull --ff-only` + relaunch. Mode detection should be designed so this slots in later.
  Until then, the updater detects a source checkout and disables itself (check-only).

Distro-packaged installs (path owned by dpkg/rpm) are **notify-only**: the updater never writes
an overlay that would shadow `apt upgrade`.

## Versioning

- `syncplay/__init__.py` gains **`fork_release`** — a monotonically increasing integer, bumped on
  every fork release (overlay *or* full). It is the only number the updater compares. The
  upstream `version`/`milestone`/`release_number` fields keep their meaning and format (build
  scripts text-parse them).
- A running client is described as **base rB + overlay rN**: rB is the `fork_release` frozen into
  the installation, rN the active overlay's (absent if none). The overlay manifest carries
  `min_base`: the oldest base its code still runs on (see the release guide for bump policy).
- **The overlay activates only when strictly newer than the base** (`overlay > base`). This one
  rule makes stale overlays self-disable when the user installs a newer full build, lets
  `apt upgrade` win on packaged installs, and makes downgrades-for-testing behave.

## UI/UX specification

### Config dialog

A new **"Updates"** group box on the Misc tab, directly below "Syncplay internals" (the existing
`checkForUpdatesAutomatically` checkbox moves from the internals group into it — same key, same
behavior, just regrouped):

```
┌─ Updates ────────────────────────────────────────────────────────┐
│ [x] Check for updates automatically      (checkForUpdatesAutomatically) │
│ [x] Install updates in place, without reinstalling  (autoUpdate) │
│ [ ] Install updates without asking             (autoInstallUpdates)│
│ Update source:  [prc94/syncplay             ]  (updateRepo)      │
│                                                                  │
│ Running: base r3 + overlay r5 — up to date        [Check now]    │
└──────────────────────────────────────────────────────────────────┘
```

- **"Check for updates automatically"** (`checkForUpdatesAutomatically`, upstream's key,
  default **on**) — governs *when* checks happen, for both mechanisms: it is the early return in
  `gui.py:automaticUpdateCheck`. Off means nothing is checked or fetched unless the user presses
  Check now (or Help → check for updates).
- **"Install updates in place, without reinstalling"** (`autoUpdate`, default **on**) — governs
  *what a check does*. On: query the update source, offer to install the overlay and restart.
  Off: upstream behavior — query syncplay.pl, notify, offer the download page. It is not a
  kill switch for already-installed overlays: an overlay installed earlier keeps running (the
  bootstrap reads no config by design), and Check now still works. What it stops is this client
  acquiring new code by itself. Reverting to the shipped base is a separate action — delete the
  overlay directory, or let the crash guard quarantine it.
- **"Install updates without asking"** (`autoInstallUpdates`, default **off**) — with it on, a
  found update is downloaded, verified, and staged silently; the client applies it at the next
  launch, or offers an immediate restart when idle (see flows). This is the "almost on the fly"
  mode. Disabled (greyed) while `autoUpdate` is off.
- **"Update source"** (`updateRepo`, default `constants.UPDATE_DEFAULT_REPO = "prc94/syncplay"`)
  — a GitHub `owner/repo`. Our builds ship with the default pointing at this fork; other fork
  operators can point their users at their own repo. Changing it triggers the trust flow below.
  Validation: must match `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`; invalid input reverts with a
  tooltip-style error, it is never sent anywhere.
- **Status line + [Check now]** — shows `base rB + overlay rN` and the result of the last check
  (`up to date` / `update rM available` / `check failed: …`). [Check now] runs a check in a
  background thread (never block the dialog), then swaps in an **[Update & restart]** button
  when an installable update exists.

All widgets follow the fork GUI conventions: `objectName` = config key (auto-bound), tooltips
looked up by lowercased objectName + `-tooltip` in `messages_en.py`, every string an i18n key.

### Message keys (messages_en.py)

`updates-title`, `autoupdate-label`/`-tooltip`, `autoinstallupdates-label`/`-tooltip`,
`updaterepo-label`/`-tooltip`, `update-check-button`, `update-apply-button`,
`update-status-uptodate`, `update-status-available`, `update-status-failed`,
`update-available-notification`, `update-staged-notification`, `update-restart-prompt`,
`update-needs-full-install-notification`, `update-repo-trust-prompt`,
`update-overlay-disabled-after-crash-notification`, `update-invalid-repo-error`.

### Flows

1. **Automatic check** — reuses the existing cadence (`gui.py:automaticUpdateCheck`, gated on
   `checkForUpdatesAutomatically`). With `autoUpdate` on, the check queries the GitHub releases
   API of `updateRepo` instead of syncplay.pl and compares `fork_release`.
2. **Update found, `autoInstallUpdates` off** — non-modal notification in the main window:
   *"Syncplay update r5 is available."* with **[Update & restart]**, **[Skip this version]**
   (persisted in updater state), **[Later]**. No modal interruptions mid-playback, ever.
3. **Update found, `autoInstallUpdates` on** — download + verify + stage silently. If the client
   is idle (not in a room, or in a room but paused with no file open), prompt to restart now;
   otherwise show a passive notice (chat/OSD area): *"Update r5 installed — applies on next
   launch."* Nothing steals focus during playback.
4. **Manual** — [Check now] in the config dialog; same pipeline, verbose errors.
5. **Update & restart** — after staging succeeds: clean disconnect, save config, relaunch
   (`os.execv` of `sys.executable` + original argv on POSIX; spawn-new-process-then-exit on
   frozen Windows), auto-reconnect from saved config. If the user was in a room, they rejoin it.
6. **`min_base` not met** — the manifest's `min_base` exceeds the running base: the update
   cannot ship as an overlay. Notification: *"This update requires a new full installation."*
   with a button opening the release page. Never attempt a partial install.
7. **Custom repo trust flow** — see Security below.
8. **Crash fallback** — see Robustness below.

### Console / `--no-gui`

Deferred, but reserved: a client-side `update` command in `consoleUI.executeCommand`
(check/apply from the console), and `--update` as a one-shot CLI action. The updater core must
live in a UI-agnostic module (`syncplay/updater.py`) called through `UiManager` so both surfaces
share it.

## Config keys (ui/ConfigurationGetter.py)

Standard client-setting recipe — defaults dict + `_iniStructure` (new `[update]` section or
`client_settings`) + argparse + `_overrideConfigWithArgs`:

| key | type | default | CLI |
|---|---|---|---|
| `autoUpdate` | bool | `True` | `--no-auto-update` |
| `autoInstallUpdates` | bool | `False` | `--auto-install-updates` |
| `updateRepo` | str | `constants.UPDATE_DEFAULT_REPO` | `--update-repo` |
| `updateRepoKey` | str | `""` (pinned key for non-default repos) | — |

`checkForUpdatesAutomatically` is unchanged. Updater *state* (skipped version, crash marker,
last check time) is not config — it lives in `state.json` inside the overlay root.

## Security model

An update channel is remote code execution by design. Non-negotiables:

- **Transport**: GitHub releases API over TLS (`https://api.github.com/repos/{repo}/releases`).
  Platform-agnostic, no server of ours to run, and the unauthenticated rate limit (60/hr/IP) is
  ample for on-demand checks. The client fetches the latest release's
  `syncplay-overlay-r*.manifest.json` asset, then the zip it names.
- **Signing**: every overlay zip is Ed25519-signed at build time (`ci/build-overlay.py`); the
  manifest carries the signature and sha256. The client verifies **sha256 + signature over the
  full zip bytes before extracting anything**, using `cryptography` (already in the frozen base
  for TLS — no new dependency). `min_base`/`fork_release` are read from `overlay.json` *inside*
  the verified zip; the manifest's copies are advisory (used only to decide whether to download).
- **Key pinning**: the public key for the default repo is baked into `constants.py`
  (`UPDATE_DEFAULT_REPO_PUBKEY`) and is always used for it — a config-file edit cannot swap it.
  For a *custom* repo, the first check performs trust-on-first-use: a modal shows the repo name
  and the key fingerprint from its manifest with an explicit warning ("updates run code on your
  machine; only continue if you trust this repository's owner"); on accept, the key is pinned in
  `updateRepoKey` and any later key change is a hard error, not a re-prompt.
- **The Syncplay server never delivers code.** With the fork's admin features it is tempting to
  push updates through the server channel; that would hand every server operator RCE on every
  client. The server may at most *announce* that an update exists (plain chat). No update
  material ever arrives over the Syncplay protocol.
- Downloads land in `pending/` and are verified there; nothing unverified is ever on `sys.path`.

## Overlay storage & bootstrap

Overlay root per platform:

| mode | overlay root |
|---|---|
| Windows installed | `%APPDATA%\Syncplay\overlay\` |
| Windows portable (config file next to exe) | `<exedir>\overlay\` if writable, else `%APPDATA%` (warn: breaks self-containment) |
| macOS `.app` | `~/Library/Application Support/Syncplay/overlay/` (bundle untouched → signature intact) |
| Linux AppImage | `$XDG_DATA_HOME/syncplay/overlay/` (image is read-only) |
| Linux distro package | none — notify-only |
| Source checkout | none — updater disabled (git-pull strategy later) |

Layout inside the root:

```
overlay/
  current/     # active overlay: syncplay/ package + overlay.json
  previous/    # last good overlay, kept for rollback
  pending/     # download + verify staging; atomic rename to current/
  state.json   # skipped version, crash marker, pinned key fingerprint, last check
```

**Bootstrap** (runs in the entry stubs before the app imports `syncplay` for real): read the
frozen base's `fork_release`; if `current/overlay.json` exists, parses, and is strictly newer,
and no crash marker is set, prepend `current/` to `sys.path` (purging any already-imported
`syncplay*` modules first). The bootstrap must stay tiny and boring — **it is frozen into the
exe stub and cannot be updated by an overlay**; every branch it grows is a branch we support
forever. Overlaid code resolves `resources/` relative to its own `__file__` (the existing
non-frozen branch of `utils.findWorkingDir`) so the overlay's `syncplayintf.lua` — not the
frozen one — reaches mpv.

**Robustness**: the bootstrap writes a crash marker before handing control to overlaid code and
the client clears it once startup completes; if the marker is present at boot, the overlay is
quarantined (renamed aside), the base (or `previous/`) boots instead, and the user is told.
`current/` → `previous/` rotation happens on every successful update.

**Uninstall**: the NSIS uninstaller's optional "remove configuration" step also removes
`%APPDATA%\Syncplay\overlay\`.

## Rollout phases

1. **Repoint the update check** — `client.checkForUpdate` queries the GitHub releases API of
   `updateRepo` and compares `fork_release`; notify + open release page. (The current
   syncplay.pl check can only ever announce *upstream* releases — it is already wrong for the
   fork.) Ships value with no overlay machinery.
2. **Overlay pipeline** — bootstrap in the entry stubs, `syncplay/updater.py`
   (download/verify/stage/rotate), config dialog group, restart flow. Requires **one final
   conventional release** so every install gains the bootstrap; there is no way around that.
3. **Git-pull strategy for source checkouts**; console `update` command.

Each phase follows the fork feature recipe (constants, i18n keys, docs, committed test suite).
The tooling half of phase 2 already exists: see `docs/overlay-release-guide.md`.
