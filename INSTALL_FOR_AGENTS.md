# Agent installation guide

Use this file when a user pastes this repository URL into Antigravity and asks you to set up the Hermes Agent architecture (`hermes-core`).

## Step 1: Inspect the environment first

Run a quick check (without printing any secret files) to find:
1. Host OS (Windows, macOS, or Linux).
2. Python 3.10+ executable path (`python3` or `python`) and confirm `sqlite3` supports FTS5.
3. Antigravity config root (`~/.gemini/config`) and transcript directory (`~/.gemini/antigravity/brain`).
4. Whether an existing Hermes Agent directory exists (`~/.hermes` or `%LOCALAPPDATA%\hermes`) with `memories/` or `skills/`.

## Step 2: Ask the user 3 short setup questions

Wait for the user's answers before writing to `~/.gemini/config/`:
1. Memory and skills mode: if an existing Hermes installation was found, ask whether to run in `shared` mode (live two-way sync with Hermes `memories/`, `skills/`, and read-only `state.db`) or `standalone` mode inside `hermes-core/data/`. If no Hermes install exists, confirm `standalone` mode.
2. Persona and language: what language and tone should go into `rules/AGENTS.md` (for example, concise English or casual Indonesian)?
3. Workspace folders and optional `infra.env`:
   - Default scratch folder for loose non-project files (suggest `~/antigravity-scratch` on macOS/Linux or `%USERPROFILE%\antigravity-scratch` on Windows if they have none).
   - Optional root folder where new projects live (for example `~/Projects` or `D:\Projects`) so only ambiguous new projects require asking where to save.
   - Whether they want an empty `~/.gemini/config/infra.env` template for local SSH/API tokens.

## Step 3: Install the plugin

Clone or fetch this repository to a temporary directory (or read its files from the local checkout if already cloned), then run the zero-dependency installer:

```bash
# Standalone mode (example with workspace hygiene paths)
python3 install.py --mode standalone --scratch-dir ~/antigravity-scratch

# Shared mode with an existing Hermes directory
python3 install.py --mode shared --hermes-home /path/to/.hermes --scratch-dir ~/antigravity-scratch
```

On Windows, use `python` with the user's detected Python 3.10+ binary. Pass `--projects-dir <path>` if the user gave a default projects root.

Check `~/.gemini/config/config.json`: if another persistent memory plugin (such as `personal-memory`) is enabled alongside `hermes-core`, ask the user before disabling it so `SessionStart` does not inject duplicate memory snapshots.

Re-running `install.py` preserves customized `rules/AGENTS.md` automatically (pass `--force-rules` to back up and reset it). Use `python3 install.py --uninstall` (plus `--purge-data` to delete standalone `memories/` and `skills/`) for clean removal.

If the user requested a custom persona or extra conventions, update `~/.gemini/config/plugins/hermes-core/rules/AGENTS.md` right after `install.py` finishes. Keep all MCP tool references pointing to `ServerName="hermes-core_hermes-core"`.

## Step 4: Verify before finishing

1. Run the isolated test suite from the repo:
   ```bash
   python3 tests/verify_all.py
   ```
   Confirm exit code `0` and `ALL VERIFICATION TESTS PASSED (5/5)`.
2. Inspect `~/.gemini/config/skills.json` and confirm every `path` is an absolute path (never `~/`) and every `exclude` entry is an exact folder name (never a regex or wildcard).
3. Inspect `~/.gemini/config/config.json` and confirm `plugins.hermes-core.enabled` is `true`.
4. Confirm `~/.gemini/config/plugins/hermes-core/hooks.json` and `mcp_config.json` exist directly inside the plugin root and contain the resolved Python path.

## Step 5: Report back

Tell the user what was installed, share the paths to their memory files and `skills.json`, and remind them to open a New Chat so Antigravity loads the new plugin, MCP tools, hooks, and rules.
