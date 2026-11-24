"""Antigravity lifecycle hook runner for the Hermes closed learning loop."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import atomic_write_text, file_lock, get_data_dir, load_hermes_limits
from fts_indexer import get_recent_sessions_digest, sync_all_transcripts, sync_single_transcript
from memory_store import MemoryStore
from skill_curator import bump_use, bump_view
from skill_manager import SkillManager, resolve_skill_name_from_path, sync_skills_registry

_STATE_TTL_SECONDS = 7 * 86400


def _state_path() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "hook_state.json"


def _log_error(msg: str) -> None:
    try:
        log_path = get_data_dir() / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


def _state_lock():
    return file_lock(_state_path().with_suffix(".json.lock"), timeout=2.0)


def _load_state() -> Dict[str, Dict[str, Any]]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    cutoff = time.time() - _STATE_TTL_SECONDS
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, dict) and float(v.get("updated_at") or 0.0) >= cutoff
    }


def _get_conv_entry(state: Dict[str, Dict[str, Any]], conv_id: str) -> Dict[str, Any]:
    entry = state.get(conv_id)
    if not isinstance(entry, dict):
        entry = {
            "turns_since_memory": 0,
            "iters_since_skill": 0,
            "last_user_hash": "",
            "memory_writes": 0,
            "skill_writes": 0,
            "updated_at": time.time(),
        }
        state[conv_id] = entry
    return entry


def _save_state(state: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_text(_state_path(), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _detect_hermes_tool(tool_call: Dict[str, Any]) -> str:
    name = str(tool_call.get("name") or "")
    args = tool_call.get("args") or {}
    if name == "call_mcp_tool" and isinstance(args, dict):
        if args.get("ServerName") in ("hermes-core", "hermes-core_hermes-core"):
            return str(args.get("ToolName") or "")
    if name.startswith("mcp_hermes-core_hermes-core_"):
        return name[len("mcp_hermes-core_hermes-core_") :]
    if name.startswith("mcp_hermes-core_"):
        return name[len("mcp_hermes-core_") :]
    if name in ("memory", "skill_manage", "session_search"):
        return name
    return ""


def _handle_session_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    conv_id = str(payload.get("conversationId") or "")
    try:
        sync_skills_registry()
    except Exception as exc:
        _log_error(f"SessionStart sync_skills_registry failed: {exc}")

    try:
        sync_all_transcripts(max_seconds=1.2)
    except Exception as exc:
        _log_error(f"SessionStart sync_all_transcripts failed: {exc}")

    snapshot = MemoryStore().render_frozen_snapshot()
    digest = ""
    try:
        digest = get_recent_sessions_digest(exclude_session_id=conv_id, limit=5)
    except Exception as exc:
        _log_error(f"SessionStart get_recent_sessions_digest failed: {exc}")

    parts = [snapshot]
    if digest:
        parts.append(digest)
    try:
        cur_preview = SkillManager().curate(dry_run=True)
        due_stale = len(cur_preview.get("to_stale") or [])
        due_arch = len(cur_preview.get("to_archive") or [])
        if due_stale or due_arch:
            parts.append(
                f"[Curator Preview: {due_stale} stale, {due_arch} archive-eligible agent skill(s). "
                "Run skill_manage(action='curate') to review.]"
            )
    except Exception as exc:
        _log_error(f"SessionStart curate preview failed: {exc}")
    parts.append(
        "[Hermes Core Active: memory/user snapshot is live across rooms. "
        'Use `hermes-core_hermes-core` MCP tools (`memory`, `session_search`, `skill_manage`, '
        'e.g. `call_mcp_tool(ServerName="hermes-core_hermes-core", ToolName=...)`) to persist durable facts, '
        "search prior sessions, and manage skills.]"
    )
    return {"injectSteps": [{"systemMessage": {"systemMessage": "\n\n".join(parts)}}]}


def _handle_pre_invocation(payload: Dict[str, Any]) -> Dict[str, Any]:
    conv_id = str(payload.get("conversationId") or "default")
    user_input = str(payload.get("lastUserInput") or "").strip()
    if not user_input:
        return {}
    digest = hashlib.sha1(user_input.encode("utf-8", "replace")).hexdigest()
    with _state_lock():
        state = _load_state()
        entry = _get_conv_entry(state, conv_id)
        if entry.get("last_user_hash") != digest:
            entry["last_user_hash"] = digest
            entry["turns_since_memory"] = int(entry.get("turns_since_memory") or 0) + 1
            entry["updated_at"] = time.time()
            _save_state(state)
    return {}


def _handle_post_tool_use(payload: Dict[str, Any]) -> Dict[str, Any]:
    conv_id = str(payload.get("conversationId") or "default")
    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tool_call.get("name") or "")
    tool_args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    hermes_tool = _detect_hermes_tool(tool_call)

    if tool_name in ("read_file", "view_file"):
        read_path = str(tool_args.get("Path") or tool_args.get("AbsolutePath") or "").strip()
        if read_path:
            try:
                skill_name = resolve_skill_name_from_path(read_path)
                if skill_name:
                    bump_view(skill_name)
                    bump_use(skill_name)
            except Exception as exc:
                _log_error(f"PostToolUse skill usage bump failed: {exc}")

    with _state_lock():
        state = _load_state()
        entry = _get_conv_entry(state, conv_id)
        if hermes_tool == "memory":
            entry["turns_since_memory"] = 0
            entry["memory_writes"] = int(entry.get("memory_writes") or 0) + 1
        elif hermes_tool == "skill_manage":
            entry["iters_since_skill"] = 0
            entry["skill_writes"] = int(entry.get("skill_writes") or 0) + 1
        else:
            entry["iters_since_skill"] = int(entry.get("iters_since_skill") or 0) + 1
        entry["updated_at"] = time.time()
        _save_state(state)
    return {}


def _handle_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    conv_id = str(payload.get("conversationId") or "default")
    transcript_path = str(payload.get("transcriptPath") or "").strip()
    if transcript_path:
        p = Path(transcript_path)
        if p.exists():
            try:
                sync_single_transcript(p)
            except Exception as exc:
                _log_error(f"Stop sync_single_transcript failed: {exc}")

    if payload.get("terminationReason") != "NO_TOOL_CALL" or bool(payload.get("continuationActive")):
        return {"decision": ""}

    limits = load_hermes_limits()
    mem_interval = int(limits["memory_nudge_interval"])
    skill_interval = int(limits["skill_nudge_interval"])

    mem_due = False
    skill_due = False
    turns = 0
    iters = 0

    with _state_lock():
        state = _load_state()
        entry = _get_conv_entry(state, conv_id)
        turns = int(entry.get("turns_since_memory") or 0)
        iters = int(entry.get("iters_since_skill") or 0)
        mem_due = mem_interval > 0 and turns >= mem_interval
        skill_due = skill_interval > 0 and iters >= skill_interval
        if mem_due:
            entry["turns_since_memory"] = 0
        if skill_due:
            entry["iters_since_skill"] = 0
        if mem_due or skill_due:
            entry["updated_at"] = time.time()
            _save_state(state)

    if not (mem_due or skill_due):
        return {"decision": ""}

    checks = []
    if mem_due:
        checks.append(
            f"- Memory review due ({turns} turns since last memory update): check if any durable user preference, "
            'environment fact, or correction should be saved/patched via MCP `ServerName="hermes-core_hermes-core"` `ToolName="memory"`.'
        )
    if skill_due:
        checks.append(
            f"- Skill review due ({iters} tool calls since last skill update): check if a reusable multi-step "
            'workflow or tricky workaround should be saved/refined via MCP `ServerName="hermes-core_hermes-core"` `ToolName="skill_manage"`.'
        )
    reason = (
        "[Hermes Learning Loop Review]\n"
        + "\n".join(checks)
        + "\nIf nothing durable needs saving, do not force a write; reply with a brief 1-line confirmation."
    )
    return {"decision": "continue", "reason": reason}


def dispatch_hook(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    event = str(payload.get("hookEventName") or "")
    if event == "SessionStart":
        return event, _handle_session_start(payload)
    if event == "PreInvocation":
        return event, _handle_pre_invocation(payload)
    if event == "PostToolUse":
        return event, _handle_post_tool_use(payload)
    if event == "Stop":
        return event, _handle_stop(payload)
    return event, {}


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    event = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            event = str(payload.get("hookEventName") or "")
            _, out = dispatch_hook(payload)
        else:
            out = {}
    except Exception as exc:
        _log_error(f"Hook failure ({event}): {exc}\n{traceback.format_exc()}")
        out = {"decision": ""} if event == "Stop" else {}

    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
