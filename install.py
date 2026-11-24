#!/usr/bin/env python3
"""Cross-platform zero-dependency installer for the hermes-core Antigravity plugin."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


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


def copy_plugin_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "plugin.json", dst / "plugin.json")

    for sub in ("rules", "server"):
        src_sub = src / sub
        dst_sub = dst / sub
        if dst_sub.exists():
            shutil.rmtree(dst_sub)
        shutil.copytree(
            src_sub,
            dst_sub,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Install hermes-core plugin into Antigravity config.")
    parser.add_argument("--mode", choices=("standalone", "shared"), default="standalone")
    parser.add_argument("--hermes-home", default=None, help="Existing Hermes home directory for shared mode.")
    parser.add_argument("--config-root", default=None, help="Override ~/.gemini/config root.")
    parser.add_argument("--skip-backfill", action="store_true", help="Skip initial FTS5 transcript scan.")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing files.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    src_plugin = repo_root / "plugin" / "hermes-core"
    if not (src_plugin / "plugin.json").is_file():
        print(f"Error: plugin source missing at {src_plugin}", file=sys.stderr)
        return 1

    config_root = Path(args.config_root).expanduser().resolve() if args.config_root else (Path.home() / ".gemini" / "config")
    dst_plugin = config_root / "plugins" / "hermes-core"
    data_dir = dst_plugin / "data"

    hermes_home: Optional[Path] = None
    if args.mode == "shared":
        hermes_home = Path(args.hermes_home).expanduser().resolve() if args.hermes_home else detect_hermes_home()
        if hermes_home is None or not hermes_home.exists():
            print("Error: --mode shared requested, but no Hermes directory was found. Pass --hermes-home.", file=sys.stderr)
            return 1

    python_exe = str(Path(sys.executable).resolve())
    mcp_server_path = dst_plugin / "server" / "hermes_mcp.py"

    plan = {
        "mode": args.mode,
        "hermes_home": str(hermes_home) if hermes_home else None,
        "config_root": str(config_root),
        "plugin_root": str(dst_plugin),
        "python_exe": python_exe,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return 0

    copy_plugin_tree(src_plugin, dst_plugin)
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "standalone":
        (data_dir / "memories").mkdir(parents=True, exist_ok=True)
        (data_dir / "skills").mkdir(parents=True, exist_ok=True)

    atomic_write_json(
        data_dir / "plugin_settings.json",
        {"mode": args.mode, "hermes_home": str(hermes_home) if hermes_home else None},
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
