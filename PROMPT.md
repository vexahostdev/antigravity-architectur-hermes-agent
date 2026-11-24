# Standalone Zero-Clone Build Prompt

Copy everything inside the code block below and paste it directly into an Antigravity chat if you want the agent to build and verify the entire `hermes-core` architecture from scratch without cloning any repository.

````markdown
You are tasked with designing and installing a production-grade `hermes-core` plugin for Google Antigravity that replicates the closed learning loop architecture of `NousResearch/hermes-agent`: bounded curated memory, SQLite FTS5 cross-session recall, self-improving skills with usage telemetry and Curator lifecycle, dual-scope security scanning, rolling backups, local secret vault hygiene, and lifecycle hook nudges.

Execute every phase below in order and run the full isolated verification suite before concluding.

## Phase 0: Environment inspection and 3 setup questions

First inspect the host environment (without printing secret file contents):
1. Detect OS (Windows, macOS, or Linux), Python 3.10+ path (verify stdlib `sqlite3` has FTS5), `~/.gemini/config/`, and `~/.gemini/antigravity/brain/`.
2. Check if an existing Hermes Agent directory exists (`~/.hermes` or `%LOCALAPPDATA%\hermes`) containing `memories/` or `skills/`.

Then ask the user 3 concise questions and wait for answers before writing to `~/.gemini/config/`:
1. Memory and skills mode: if a Hermes directory exists, ask whether to share `memories/`, `skills/`, and read-only `state.db` bidirectionally with Hermes, or use standalone storage under `plugins/hermes-core/data/`.
2. Persona and language: what language and communication style should `rules/AGENTS.md` enforce?
3. Default scratch folder and optional local secrets file: where should non-project loose files be saved (never in root drives), and should an empty `~/.gemini/config/infra.env` template be prepared?

## Phase 1: Plugin manifests and rules (`~/.gemini/config/plugins/hermes-core/`)

Use **Python stdlib only** (zero external `pip` packages). Create:
1. `plugin.json`: declare `"name": "hermes-core"` and description.
2. `mcp_config.json`: register `"mcpServers": {"hermes-core": {"command": "<PYTHON_EXE>", "args": ["-u", "<PLUGIN_ROOT>/server/hermes_mcp.py"], "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}}`.
3. `hooks.json` directly at the plugin root (not inside a subfolder):
   - Register under `"hermes-core"`: `SessionStart` (timeout 15), `PreInvocation` (timeout 5), `PostToolUse` with `"matcher": "*"` (timeout 5), and `Stop` (timeout 15), each invoking `<PYTHON_EXE> -u server/hooks_runner.py`.
4. `rules/AGENTS.md`:
   - Persona (`SOUL.md`): direct answers matching question weight, deliverables first, zero filler, honest technical pushback.
   - Live memory precedence: frozen `SessionStart` `MEMORY.md`/`USER.md` beats stale static workspace notes.
   - Proactive bounded memory (`ServerName="hermes-core_hermes-core"`, `ToolName="memory"`): save preferences, environment facts, corrections, and milestones automatically; consolidate via `patch`/`remove`/`batch` near capacity; never store tokens or passwords; note rolling backups (`action="backups"`).
   - Active cross-session recall (`ServerName="hermes-core_hermes-core"`, `ToolName="session_search"`): immediately search prior sessions whenever the user references earlier chats or decisions.
   - Self-improving skills and Curator (`ServerName="hermes-core_hermes-core"`, `ToolName="skill_manage"`): create or patch reusable workflows, keep `SKILL.md` lean, put large references in `references/` or `scripts/`, support `pin`/`unpin`/`usage`/`curate`/`restore`, never force writes on review nudges.
   - Workspace scratch hygiene: route standalone outputs to the user's scratch folder, never drive roots; scope searches to project folders.
   - Local secrets vault rule (if `infra.env` used): parse via `split('=', 1)`, never echo secret values, prefer API tokens over browser login, and ask the user immediately if a remote server fails auth/connection (might have migrated).

## Phase 2: Implement 8 zero-dependency Python modules (`server/`)

1. `common.py`:
   - Path resolvers with `HERMES_CORE_*` env overrides so tests run isolated in temporary directories.
   - Cross-platform `file_lock(lock_path, timeout=3.0)` using `fcntl.flock` on POSIX and `msvcrt.locking` on Windows (always initialize `.lock` file with at least 1 byte `" "` before locking on Windows).
   - `load_hermes_limits()` reading `config.yaml` with regex fallback (`2200` memory, `1375` user, `10` memory turns, `15` skill iters).
   - `atomic_write_text(path, text)` using sibling temp file, UTF-8 LF `\n`, `os.fsync()`, and `os.replace()`.
   - `redact_secrets(text)` scrubbing PEM keys, Bearer tokens, `sk-`, `ghp_`, `xox`, `AIza`, `\d+\|[A-Za-z0-9]{20,}`, and `api_key|password|passwd|pass|token|secret` values containing special chars (`@`, `&`, `'`, `|`).
2. `threat_scanner.py`:
   - Dual-scope scanner (`"strict"` for memory, `"skill"` for skills, capped at 65,536 chars).
   - Detect invisible/bidi Unicode (`\u200b`..`\u202e`, `\u2066`..`\u2069`, mid-string `\ufeff`), prompt injection, secret/context exfiltration (`curl/wget` with secret env vars, `cat .env/.netrc`), `authorized_keys`, `~/.ssh` mutation, `~/.hermes/.env`, and hardcoded secrets.
   - Keep `curl|sh`, `powershell -enc`, and `eval(base64_decode` inside `"strict"` only so legitimate DevOps/backend skills produce zero false positives.
3. `memory_store.py`:
   - Manage `MEMORY.md` (2,200 chars) and `USER.md` (1,375 chars) joined by `\n§\n` with LF line endings and no trailing newline.
   - Copy pre-write snapshot to `<memory_dir>/.backups/<STEM>_<UTC_STAMP>.bak` before every mutation and prune keeping the newest 15 files.
   - Actions: `status`, `add`, `patch` (exact whole-entry wins over substring; fail on ambiguous or multi-match), `remove`, `batch` (atomic rollback on error/overflow), `backups`, plus `render_frozen_snapshot()`.
4. `fts_indexer.py`:
   - Maintain SQLite WAL + FTS5 (`unicode61`) database `<DATA_DIR>/sessions_fts.db` with `indexed_files`, `ag_sessions`, `ag_messages`, and `ag_messages_fts` triggers.
   - Incrementally index `transcript.jsonl` files (`USER_INPUT` and non-empty `PLANNER_RESPONSE` `content` only; strip `<USER_REQUEST>`/`<ADDITIONAL_METADATA>` wrappers; never delete already-indexed rows if a UI room is deleted).
   - Optional strict read-only (`?mode=ro`) bridge to Hermes `state.db`, excluding hidden sources and demoting `cron` below interactive chats.
   - Implement `discover`, `scroll`, `read`, `browse` (2,000-char cap + `redact_secrets`) and `get_recent_sessions_digest(limit=5)`. Support `--full` CLI flag.
5. `skill_curator.py`:
   - Maintain 13-field `<skills_dir>/.usage.json` records under `.usage.json.lock` (`archived_at`, `created_at`, `created_by`, `last_patched_at`, `last_reused_patch_generation`, `last_used_at`, `last_viewed_at`, `patch_count`, `patch_generation`, `pinned`, `state`, `use_count`, `view_count`).
   - Implement `bump_view`, `bump_use` (wakes `stale -> active`), `bump_patch`, `record_created`, `record_archived`, `record_restored`, `set_pinned`, `is_pinned`, `usage_report`, `create_curator_backup` (`.curator_backups/<UTC>/skills.tar.gz`, keep 5), and `run_curator` (`stale_after_days=14`, `archive_after_days=30`, `dry_run=True` default, skips `pinned` and non-`agent` skills).
6. `skill_manager.py`:
   - Discover active `SKILL.md` folders skipping `.`-prefixed directories.
   - `sync_skills_registry()` (`--sync`): write real **absolute paths** of skill root and every category folder into `~/.gemini/config/skills.json`, use **exact hidden folder names** in `exclude` (`[".archive", ".curator_backups", ".hub", ".locks"]`), and preserve existing non-managed entries.
   - Actions: `list`, `view`, `create`, `edit`, `patch`, `write_file`, `remove_file`, `delete` (refuse if pinned; move to `.archive/`), `pin`, `unpin`, `usage`, `curate`, `restore`.
   - Expose `resolve_skill_name_from_path(file_path)`.
7. `hermes_mcp.py`:
   - Stdio JSON-RPC 2.0 server (`initialize`, silent ignore for notifications without `id`, `ping`, `tools/list`, `tools/call`). Force UTF-8 + LF `\n` stdio; log errors strictly to `stderr`. Expose `memory`, `session_search` (quick incremental sync first), and `skill_manage`.
8. `hooks_runner.py`:
   - Fail-open hook runner storing per-conversation counters in `hook_state.json` (7-day TTL).
   - `SessionStart`: sync `skills.json` + FTS5, inject frozen `MEMORY.md` + `USER.md` + 5 recent sessions + read-only Curator preview (never auto-archive at startup).
   - `PreInvocation`: count unique `lastUserInput` turns.
   - `PostToolUse`: detect `read_file`/`view_file` inside active skills to bump view/use; reset `turns_since_memory` / `iters_since_skill` when `call_mcp_tool` calls `ServerName="hermes-core_hermes-core"` (or eager prefix `mcp_hermes-core_hermes-core_`).
   - `Stop`: sync `transcriptPath`; only when `terminationReason == "NO_TOOL_CALL"` and `not continuationActive`, check thresholds, reset counters atomically BEFORE returning `{"decision": "continue", "reason": "[Hermes Learning Loop Review] ..."}`.

## Phase 3: Critical Antigravity constraints

1. Plugin MCP servers register under `<plugin>_<server>` -> **`hermes-core_hermes-core`**. Both `AGENTS.md` and `hooks_runner.py` must use this exact name.
2. Never place NTFS junctions inside `plugins/hermes-core/skills/` (Antigravity's plugin scanner skips junctions). Never put `~/` or regex/wildcards in `skills.json` (Antigravity does not expand `~/` and only matches exact names in `exclude`).
3. Register `"hermes-core": {"enabled": true}` inside `~/.gemini/config/config.json` without removing existing settings.

## Phase 4: Sync, backfill, and isolated verification

Run `skill_manager.py --sync` and `fts_indexer.py --full`, confirm zero false positives scanning existing active skills with `threat_scanner(scope="skill")`, and execute an isolated `TemporaryDirectory` test suite covering `memory` (CRUD, backups retention 15, threat blocking), `session_search` (4 modes, redaction, deletion survival), `skill_manage` (`skills.json` absolute paths + custom entry preservation, pin guard, curator backup/archive, restore), and all 4 lifecycle hooks (including `Stop` anti-loop). Report verified counts and instruct the user to open a New Chat.
````
