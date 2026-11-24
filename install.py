#!/usr/bin/env python3
"""Cross-platform zero-dependency installer for the hermes-core Antigravity plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_WS_START = "<!-- hermes-core:workspace-paths -->"
_WS_END = "<!-- /hermes-core:workspace-paths -->"


def check_environment() -> bool:
    """Verify Python 3.10+ and stdlib sqlite3 FTS5 support."""
    if sys.version_info < (3, 10):
        print(
            f"Error: Python 3.10+ is required (found {sys.version_info.major}.{sys.version_info.minor}).",
            file=sys.stderr,
        )
        return False
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(content)")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        print(f"Error: Python sqlite3 was built without FTS5 support ({exc}).", file=sys.stderr)
        return False
    return True


def detect_hermes_home() -> Optional[Path]:
    candidates = [Path.home() / ".hermes"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "hermes")
    for cand in candidates:
        if (cand / "memories").is_dir() or (cand / "skills").is_dir():
            return cand.resolve()
    return None


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def copy_plugin_tree(src: Path, dst: Path, force_rules: bool = False) -> Tuple[bool, bool]:
    """Copy plugin code/manifests while preserving customized rules/AGENTS.md on upgrade."""
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "plugin.json", dst / "plugin.json")

    src_server = src / "server"
    dst_server = dst / "server"
    if dst_server.exists():
        shutil.rmtree(dst_server)
    shutil.copytree(
        src_server,
        dst_server,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    src_rules = src / "rules"
    dst_rules = dst / "rules"
    dst_rules.mkdir(parents=True, exist_ok=True)

    rules_preserved = False
    rules_backed_up = False
    for item in src_rules.iterdir():
        target = dst_rules / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
            continue
        if item.name == "AGENTS.md":
            if target.exists() and not force_rules:
                rules_preserved = True
                continue
            if target.exists() and force_rules:
                shutil.copy2(target, dst_rules / "AGENTS.md.bak")
                rules_backed_up = True
        shutil.copy2(item, target)

    return rules_preserved, rules_backed_up


def apply_workspace_paths(
    agents_md_path: Path,
    scratch_dir: Optional[Path],
    projects_dir: Optional[Path],
) -> bool:
    """Update the managed workspace-hygiene marker block inside rules/AGENTS.md."""
    if (scratch_dir is None and projects_dir is None) or not agents_md_path.is_file():
        return False
    text = agents_md_path.read_text(encoding="utf-8")
    if _WS_START not in text or _WS_END not in text:
        return False

    scratch_line = (
        f"- Save loose non-project outputs (scratch scripts, drafts, exports) inside `{scratch_dir}` rather than drive roots (`C:\\`, `D:\\`, `/`) or Desktop."
        if scratch_dir
        else "- Save loose non-project outputs (scratch scripts, drafts, exports) inside the configured scratch folder rather than drive roots (`C:\\`, `D:\\`, `/`) or Desktop."
    )
    projects_line = (
        f"- Place new projects under `{projects_dir}` by default, and keep existing project work inside its own folder. Do not ask for a directory on routine tasks; only ask when a new project destination is ambiguous."
        if projects_dir
        else "- Keep project work inside the active project directory. Do not ask where to save files on routine tasks; only ask when creating a new project whose target folder is ambiguous."
    )
    replacement = f"{_WS_START}\n{scratch_line}\n{projects_line}\n{_WS_END}"
    pattern = re.compile(re.escape(_WS_START) + r".*?" + re.escape(_WS_END), re.DOTALL)
    updated, count = pattern.subn(lambda _m: replacement, text, count=1)
    if count and updated != text:
        agents_md_path.write_text(updated, encoding="utf-8", newline="\n")
        return True
    return False


def build_mcp_config(template_path: Path, python_exe: str, mcp_server_path: Path) -> Dict[str, Any]:
    raw = template_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    srv = data["mcpServers"]["hermes-core"]
    srv["command"] = python_exe
    srv["args"] = ["-u", str(mcp_server_path)]
    return data


def build_hooks_config(template_path: Path, python_exe: str) -> Dict[str, Any]:
    raw = template_path.read_text(encoding="utf-8")
    quoted_py = f'"{python_exe}"' if " " in python_exe else python_exe
    hook_rel = str(Path("server") / "hooks_runner.py")
    hook_cmd = f"{quoted_py} -u {hook_rel}"
    data = json.loads(raw)
    root = data["hermes-core"]
    for ev in ("SessionStart", "PreInvocation", "Stop"):
        for handler in root.get(ev, []):
            handler["command"] = hook_cmd
    for group in root.get("PostToolUse", []):
        for handler in group.get("hooks", []):
            handler["command"] = hook_cmd
    return data


def _legacy_home_prefix(directory: Path) -> Optional[str]:
    resolved = directory.resolve()
    home = Path.home().resolve()
    try:
        rel_posix = resolved.relative_to(home).as_posix()
        return "~" if rel_posix == "." else f"~/{rel_posix}"
    except ValueError:
        return None


def _is_managed_entry(entry_path: str, prefixes: Tuple[str, ...]) -> bool:
    norm = (entry_path or "").strip().replace("\\", "/").rstrip("/").lower()
    for prefix in prefixes:
        p = prefix.strip().replace("\\", "/").rstrip("/").lower()
        if p and (norm == p or norm.startswith(p + "/")):
            return True
    return False


def uninstall_plugin(config_root: Path, purge_data: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """Remove hermes-core registrations and runtime files cleanly."""
    dst_plugin = config_root / "plugins" / "hermes-core"
    data_dir = dst_plugin / "data"
    settings_path = data_dir / "plugin_settings.json"

    settings: Dict[str, Any] = {}
    if settings_path.is_file():
        with suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded

    mode = str(settings.get("mode") or "standalone")
    hermes_home_raw = settings.get("hermes_home")

    # 1. Remove from config.json
    cfg_path = config_root / "config.json"
    removed_from_config = False
    if cfg_path.is_file():
        with suppress(OSError, json.JSONDecodeError):
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg_data, dict) and isinstance(cfg_data.get("plugins"), dict):
                if "hermes-core" in cfg_data["plugins"]:
                    removed_from_config = True
                    if not dry_run:
                        del cfg_data["plugins"]["hermes-core"]
                        atomic_write_json(cfg_path, cfg_data)

    # 2. Remove managed entries from skills.json while preserving custom user entries
    skill_roots: List[Path] = [data_dir / "skills"]
    if hermes_home_raw:
        skill_roots.append(Path(str(hermes_home_raw)).expanduser() / "skills")
    prefixes_list: List[str] = []
    for sr in skill_roots:
        res = sr.resolve() if sr.exists() else sr.absolute()
        prefixes_list.extend([str(res), res.as_posix(), str(sr), sr.as_posix()])
        if legacy := _legacy_home_prefix(res):
            prefixes_list.append(legacy)
    managed_prefixes = tuple(dict.fromkeys(prefixes_list))

    skills_json_path = config_root / "skills.json"
    removed_skills_entries = 0
    if skills_json_path.is_file():
        with suppress(OSError, json.JSONDecodeError):
            sk_data = json.loads(skills_json_path.read_text(encoding="utf-8"))
            if isinstance(sk_data, dict) and isinstance(sk_data.get("entries"), list):
                kept = []
                for item in sk_data["entries"]:
                    if isinstance(item, dict) and _is_managed_entry(str(item.get("path") or ""), managed_prefixes):
                        removed_skills_entries += 1
                    else:
                        kept.append(item)
                if removed_skills_entries and not dry_run:
                    sk_data["entries"] = kept
                    atomic_write_json(skills_json_path, sk_data)

    # 3. Remove plugin code & optionally preserve standalone user data
    preserved_paths: List[str] = []
    if purge_data or mode != "standalone":
        if not dry_run and dst_plugin.exists():
            shutil.rmtree(dst_plugin)
    else:
        for sub_name in ("rules", "server"):
            sub = dst_plugin / sub_name
            if sub.exists() and not dry_run:
                shutil.rmtree(sub)
        for fname in ("plugin.json", "mcp_config.json", "hooks.json"):
            fpath = dst_plugin / fname
            if fpath.is_file() and not dry_run:
                fpath.unlink()
        if data_dir.is_dir():
            for item in list(data_dir.iterdir()):
                if item.name in ("memories", "skills") and item.is_dir():
                    preserved_paths.append(str(item))
                    continue
                if not dry_run:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
        if hermes_home_raw:
            preserved_paths.append(str(hermes_home_raw))

    if mode != "standalone" and hermes_home_raw:
        preserved_paths.append(str(hermes_home_raw))

    return {
        "success": True,
        "action": "uninstall",
        "dry_run": dry_run,
        "mode": mode,
        "purge_data": purge_data,
        "config_root": str(config_root),
        "plugin_root": str(dst_plugin),
        "removed_from_config_json": removed_from_config,
        "removed_skills_entries": removed_skills_entries,
        "preserved_paths": preserved_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or uninstall the hermes-core Antigravity plugin.")
    parser.add_argument("--mode", choices=("standalone", "shared"), default="standalone")
    parser.add_argument("--hermes-home", default=None, help="Existing Hermes home directory for shared mode.")
    parser.add_argument("--config-root", default=None, help="Override ~/.gemini/config root.")
    parser.add_argument("--scratch-dir", default=None, help="Default directory for loose non-project scratch files.")
    parser.add_argument("--projects-dir", default=None, help="Optional default root directory for user projects.")
    parser.add_argument("--force-rules", action="store_true", help="Backup and overwrite customized rules/AGENTS.md.")
    parser.add_argument("--skip-backfill", action="store_true", help="Skip initial FTS5 transcript scan.")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall hermes-core from Antigravity config.")
    parser.add_argument("--purge-data", action="store_true", help="With --uninstall, delete standalone memories/ and skills/.")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing or deleting files.")
    args = parser.parse_args()

    config_root = Path(args.config_root).expanduser().resolve() if args.config_root else (Path.home() / ".gemini" / "config")

    if args.uninstall:
        result = uninstall_plugin(config_root, purge_data=args.purge_data, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0

    if not check_environment():
        return 1

    repo_root = Path(__file__).resolve().parent
    src_plugin = repo_root / "plugin" / "hermes-core"
    if not (src_plugin / "plugin.json").is_file():
        print(f"Error: plugin source missing at {src_plugin}", file=sys.stderr)
        return 1

    dst_plugin = config_root / "plugins" / "hermes-core"
    data_dir = dst_plugin / "data"

    hermes_home: Optional[Path] = None
    if args.mode == "shared":
        hermes_home = Path(args.hermes_home).expanduser().resolve() if args.hermes_home else detect_hermes_home()
        if hermes_home is None or not hermes_home.exists():
            print("Error: --mode shared requested, but no Hermes directory was found. Pass --hermes-home.", file=sys.stderr)
            return 1

    scratch_dir = Path(args.scratch_dir).expanduser().resolve() if args.scratch_dir else None
    projects_dir = Path(args.projects_dir).expanduser().resolve() if args.projects_dir else None

    python_exe = str(Path(sys.executable).resolve())
    mcp_server_path = dst_plugin / "server" / "hermes_mcp.py"

    plan = {
        "mode": args.mode,
        "hermes_home": str(hermes_home) if hermes_home else None,
        "scratch_dir": str(scratch_dir) if scratch_dir else None,
        "projects_dir": str(projects_dir) if projects_dir else None,
        "config_root": str(config_root),
        "plugin_root": str(dst_plugin),
        "python_exe": python_exe,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return 0

    rules_preserved, rules_backed_up = copy_plugin_tree(src_plugin, dst_plugin, force_rules=args.force_rules)
    workspace_paths_updated = apply_workspace_paths(dst_plugin / "rules" / "AGENTS.md", scratch_dir, projects_dir)

    data_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "standalone":
        (data_dir / "memories").mkdir(parents=True, exist_ok=True)
        (data_dir / "skills").mkdir(parents=True, exist_ok=True)

    atomic_write_json(
        data_dir / "plugin_settings.json",
        {
            "mode": args.mode,
            "hermes_home": str(hermes_home) if hermes_home else None,
            "scratch_dir": str(scratch_dir) if scratch_dir else None,
            "projects_dir": str(projects_dir) if projects_dir else None,
        },
    )
    atomic_write_json(
        dst_plugin / "mcp_config.json",
        build_mcp_config(src_plugin / "mcp_config.template.json", python_exe, mcp_server_path),
    )
    atomic_write_json(
        dst_plugin / "hooks.json",
        build_hooks_config(src_plugin / "hooks.template.json", python_exe),
    )

    cfg_path = config_root / "config.json"
    cfg_data: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg_data = loaded
        except (OSError, json.JSONDecodeError):
            cfg_data = {}
    plugins_map = cfg_data.setdefault("plugins", {})
    if isinstance(plugins_map, dict):
        plugins_map["hermes-core"] = {"enabled": True}
    atomic_write_json(cfg_path, cfg_data)

    run_env = os.environ.copy()
    run_env["PYTHONUTF8"] = "1"
    run_env["PYTHONIOENCODING"] = "utf-8"
    run_env["HERMES_CORE_CONFIG_ROOT"] = str(config_root)
    run_env["HERMES_CORE_DATA_DIR"] = str(data_dir)

    sync_proc = subprocess.run(
        [python_exe, "-u", str(dst_plugin / "server" / "skill_manager.py"), "--sync"],
        env=run_env,
        capture_output=True,
        text=True,
        check=True,
    )
    sync_out = json.loads(sync_proc.stdout)

    fts_out: Dict[str, Any] = {"skipped": True}
    if not args.skip_backfill:
        fts_proc = subprocess.run(
            [python_exe, "-u", str(dst_plugin / "server" / "fts_indexer.py"), "--full"],
            env=run_env,
            capture_output=True,
            text=True,
            check=True,
        )
        fts_out = json.loads(fts_proc.stdout)

    print(
        json.dumps(
            {
                "success": True,
                **plan,
                "rules_preserved": rules_preserved,
                "rules_backed_up": rules_backed_up,
                "workspace_paths_updated": workspace_paths_updated,
                "skills_sync": sync_out,
                "fts_backfill": fts_out,
                "next_step": "Open a New Chat in Antigravity so the plugin, MCP tools, hooks, and skills load.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
