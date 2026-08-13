-- luacheck configuration for syncplayintf.lua (the mpv-side half of every mpv feature).
--
-- Run: luacheck syncplay/resources/syncplayintf.lua
-- Gated by tests/suite_lua.py against tests/lint_baseline_luacheck.txt.
--
-- Without this file luacheck reports ~424 warnings, nearly all of them noise: it has no idea
-- what mpv injects into a script's environment, and it treats the script's own top-level
-- function definitions as undeclared globals. The settings below cut that to the ~30 findings
-- that actually mean something.

-- mpv embeds Lua 5.1 (LuaJIT) on Windows and several distros, and 5.2 elsewhere. 5.1 is the
-- strict floor, so lint against it: anything 5.1 rejects would break the overlay *somewhere*.
-- This is the whole point of the check — a syntax/scope error does not degrade gracefully, it
-- takes down chat, the yap timer, the pause warning and track proposals all at once.
std = "lua51"

-- The single global mpv provides. Everything else the script uses it must define itself.
globals = {"mp"}

-- The script is one flat file of top-level `function foo()` definitions calling each other.
-- Without this every one of them is both an "undefined variable" read and a "non-standard
-- global" write. Note this does NOT mask the bug class CLAUDE.md warns about: a file-scope
-- `local` (e.g. `local utils = require 'mp.utils'`) declared *below* a closure that uses it is
-- still reported, because reading it early is a read of a global that is never assigned.
allow_defined_top = true

-- Upstream's repl.lua ancestry has long lines throughout; not worth a fork-wide reformat.
max_line_length = false

exclude_files = {"syncplay/vendor"}
