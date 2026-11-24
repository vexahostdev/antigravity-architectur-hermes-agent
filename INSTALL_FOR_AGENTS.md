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
3. Default scratch folder and optional `infra.env`: where should loose non-project files be saved so root drives stay clean, and do they want an empty `~/.gemini/config/infra.env` template for remote SSH/API tokens?

## Step 3: Install the plugin

Clone or fetch this repository to a temporary directory (or read its files from the local checkout if already cloned), then run the zero-dependency installer:

```bash
# Standalone mode
python3 install.py --mode standalone

# Shared mode with an existing Hermes directory
python3 install.py --mode shared --hermes-home /path/to/.hermes
```

On Windows, use `python` with the user's detected Python 3.10+ binary.

If the user requested a custom persona or scratch path, update `~/.gemini/config/plugins/hermes-core/rules/AGENTS.md` right after `install.py` finishes. Keep all MCP tool references pointing to `ServerName="hermes-core_hermes-core"`.

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
