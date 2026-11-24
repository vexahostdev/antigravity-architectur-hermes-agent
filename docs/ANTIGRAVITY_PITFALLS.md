# Antigravity pitfalls fixed in hermes-core

Building a memory and skills loop inside Antigravity sounds straightforward until you test it in a fresh chat room. These five quirks actually broke during live testing and shaped how `hermes-core` is built.

## 1. Plugin MCP servers are double-namespaced

When you declare `"hermes-core"` inside `mcp_config.json` of a plugin whose directory is also named `hermes-core`, Antigravity registers the server as `hermes-core_hermes-core`, not `hermes-core`.

Writing `ServerName="hermes-core"` in `AGENTS.md` or checking `args.get("ServerName") == "hermes-core"` in `PostToolUse` causes lighter models to miss the tool call and stops turn counters from resetting. `hermes-core` handles `hermes-core_hermes-core` across rules, hooks, and tests.

## 2. `skills.json` does not expand `~/` or accept regex excludes

Some local docs suggest `~/my-skills` and wildcard patterns inside `~/.gemini/config/skills.json`. In practice (documented in `https://antigravity.google/docs/customization-discovery.md`), Antigravity:
- Resolves non-absolute paths in personal `skills.json` relative to `~/.gemini/config/`, so `~/AppData/Local/hermes/skills` turns into `~/.gemini/config/~/...` and silently loads zero skills.
- Matches `exclude` entries by exact folder or file name only, so regex patterns like `^\..*` or wildcards like `load-*` never match.
- Scans each `entries` directory strictly one level deep, skipping nested `<category>/<skill>/SKILL.md` folders unless each category folder is listed explicitly.

`skill_manager.py --sync` resolves every path to a real absolute filesystem path, lists each category folder, and writes exact hidden folder names (`[".archive", ".curator_backups", ".hub", ".locks"]`) in `exclude`.

## 3. Plugin skill scanners skip directory junctions

Trying to mirror skills into `plugins/<name>/skills/` on Windows with NTFS directory junctions (`mklink /J`) looks clean on disk, but Antigravity's plugin skill scanner ignores junctions. Registering the source folders directly in `~/.gemini/config/skills.json` keeps two-way live sync with zero file copying and zero skipped skills.

## 4. Windows `msvcrt.locking` fails on zero-byte lock files

On Linux and macOS, `fcntl.flock` works on an empty file descriptor. On Windows, `msvcrt.locking(..., LK_NBLCK, 1)` raises `PermissionError` or `OSError` if the lock file is 0 bytes. `common.file_lock()` ensures the `.lock` file contains at least one byte before acquiring the Windows lock, while using `fcntl.flock` on POSIX.

## 5. `Stop` hooks need a `continuationActive` guard

A `Stop` hook that returns `{"decision": "continue", "reason": "[Hermes Learning Loop Review] ..."}` forces the agent to run one more turn. Without checking `not payload.get("continuationActive")` and resetting the due counter atomically before returning `"continue"`, the agent can get trapped in an endless review loop.

## 6. Security scanners need separate scopes for memory vs. skills

Blocking `curl ... | bash` or `eval(base64_decode` makes sense inside `MEMORY.md` (which enters every session prompt), so those stay blocked in `scope="strict"`. Blocking them inside `SKILL.md` breaks real DevOps and backend runbooks that legitimately document official installer commands or shell escaping tricks. `threat_scanner.py` keeps injection, invisible Unicode, SSH backdoors, and secret exfiltration blocked in both scopes while keeping shell-pipe checks scoped to memory writes only.
