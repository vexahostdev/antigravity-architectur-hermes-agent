"""Zero-dependency stdio JSON-RPC 2.0 MCP server for hermes-core."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fts_indexer import session_search, sync_all_transcripts
from memory_store import MemoryStore
from skill_manager import SkillManager

SERVER_INFO = {"name": "hermes-core", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2025-03-26"

TOOLS_SPEC = [
    {
        "name": "memory",
        "description": (
            "Manage persistent bounded curated memory (MEMORY.md max 2,200 chars; USER.md max 1,375 chars, "
            "delimiter '§'). Use proactively whenever the user reveals preferences, environment facts, "
            "corrections, or milestones. Never store secrets or tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "add", "patch", "remove", "batch", "backups"],
                    "description": "Operation to perform.",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user", "all"],
                    "description": "Target store: 'memory' (agent notes), 'user' (user profile), or 'all'.",
                },
                "content": {
                    "type": "string",
                    "description": "Entry text for 'add', or replacement span for 'patch'.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Unique substring (or whole entry) identifying target entry for 'patch' or 'remove'.",
                },
                "operations": {
                    "type": "array",
                    "description": "List of {action, content, old_text} objects for atomic 'batch' updates.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "patch", "remove"]},
                            "content": {"type": "string"},
                            "old_text": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "session_search",
        "description": (
            "Search and read past conversations across Antigravity transcripts (and optional Hermes state.db) "
            "via SQLite FTS5. Modes inferred from args: DISCOVER (query), SCROLL (session_id + around_message_id), "
            "READ (session_id), BROWSE (no query/session_id)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text keywords to search across past conversations."},
                "session_id": {"type": "string", "description": "Specific session ID to read or scroll."},
                "around_message_id": {"type": "integer", "description": "Message ID / step index anchor for SCROLL mode."},
                "window": {"type": "integer", "description": "Context window (+/- messages) around anchor (default 5, max 15)."},
                "origin": {
                    "type": "string",
                    "enum": ["all", "antigravity", "hermes"],
                    "description": "Filter source history (default 'all').",
                },
                "role_filter": {"type": "string", "enum": ["user", "assistant", "tool"]},
                "after": {"type": "string", "description": "Start date (YYYY-MM-DD, ISO) or relative ('24h', '7d', '2w')."},
                "before": {"type": "string", "description": "End date (YYYY-MM-DD, ISO) or relative."},
                "offset": {"type": "integer", "description": "Pagination offset for READ mode."},
                "limit": {"type": "integer", "description": "Max sessions or messages to return."},
            },
        },
    },
    {
        "name": "skill_manage",
        "description": (
            "Create, view, edit, patch, pin/unpin, curate, restore, or archive procedural skills "
            "auto-registered via ~/.gemini/config/skills.json. Protected by threat scanning and .usage.json telemetry. "
            "Keep SKILL.md concise with valid YAML frontmatter (name + description); put bulky docs in references/ or scripts/."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "view",
                        "create",
                        "edit",
                        "patch",
                        "write_file",
                        "remove_file",
                        "delete",
                        "pin",
                        "unpin",
                        "usage",
                        "curate",
                        "restore",
                    ],
                    "description": "Skill action to perform.",
                },
                "name": {"type": "string", "description": "Skill name (lowercase hyphenated, e.g. 'my-workflow')."},
                "category": {"type": "string", "description": "Optional category subfolder for 'create' or 'restore'."},
                "content": {"type": "string", "description": "Full SKILL.md markdown content for 'create' or 'edit'."},
                "old_string": {"type": "string", "description": "Unique text span to replace for 'patch'."},
                "new_string": {"type": "string", "description": "Replacement text for 'patch'."},
                "file_path": {"type": "string", "description": "Relative path under skill dir for 'view', 'patch', 'write_file', 'remove_file'."},
                "file_content": {"type": "string", "description": "File content for 'write_file'."},
                "dry_run": {"type": "boolean", "description": "For 'curate': preview transitions without mutating disk (default true)."},
            },
            "required": ["action"],
        },
    },
]


def _handle_memory(args: Dict[str, Any]) -> Dict[str, Any]:
    store = MemoryStore()
    action = args.get("action")
    target = args.get("target") or ("all" if action in ("status", "backups") else "memory")

    if action == "status":
        return store.status(target)
    if action == "backups":
        return store.list_backups(target)
    if action == "add":
        return store.add(target, str(args.get("content") or ""))
    if action == "patch":
        return store.patch(target, str(args.get("old_text") or ""), str(args.get("content") or ""))
    if action == "remove":
        return store.remove(target, str(args.get("old_text") or ""))
    if action == "batch":
        return store.batch(target, args.get("operations") or [])
    return {"success": False, "error": f"Unknown memory action '{action}'."}


def _handle_session_search(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        sync_all_transcripts(max_seconds=0.8)
    except Exception as exc:
        print(f"[hermes-core] incremental sync warning: {exc}", file=sys.stderr)
    return session_search(
        query=args.get("query"),
        session_id=args.get("session_id"),
        around_message_id=args.get("around_message_id"),
        window=int(args.get("window") or 5),
        origin=str(args.get("origin") or "all"),
        role_filter=args.get("role_filter"),
        after=args.get("after"),
        before=args.get("before"),
        offset=int(args.get("offset") or 0),
        limit=args.get("limit"),
    )


def _handle_skill_manage(args: Dict[str, Any]) -> Dict[str, Any]:
    mgr = SkillManager()
    action = args.get("action")
    name = str(args.get("name") or "")

    if action == "list":
        return mgr.list_skills()
    if action == "view":
        return mgr.view(name, file_path=args.get("file_path"))
    if action == "create":
        return mgr.create(name, str(args.get("content") or ""), category=args.get("category"))
    if action == "edit":
        return mgr.edit(name, str(args.get("content") or ""))
    if action == "patch":
        return mgr.patch(
            name,
            str(args.get("old_string") or ""),
            str(args.get("new_string") or ""),
            file_path=args.get("file_path"),
        )
    if action == "write_file":
        return mgr.write_file(name, str(args.get("file_path") or ""), str(args.get("file_content") or ""))
    if action == "remove_file":
        return mgr.remove_file(name, str(args.get("file_path") or ""))
    if action == "delete":
        return mgr.delete(name)
    if action == "pin":
        return mgr.pin(name)
    if action == "unpin":
        return mgr.unpin(name)
    if action == "usage":
        return mgr.usage(name=args.get("name"))
    if action == "curate":
        dry_run = True if args.get("dry_run") is None else bool(args.get("dry_run"))
        return mgr.curate(dry_run=dry_run)
    if action == "restore":
        return mgr.restore(name, category=args.get("category"))
    return {"success": False, "error": f"Unknown skill_manage action '{action}'."}


def _dispatch_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "memory":
        return _handle_memory(args)
    if name == "session_search":
        return _handle_session_search(args)
    if name == "skill_manage":
        return _handle_skill_manage(args)
    return {"success": False, "error": f"Unknown tool '{name}'."}


def _send(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[hermes-core] malformed JSON-RPC: {exc}", file=sys.stderr)
            continue

        req_id = req.get("id")
        if req_id is None:
            continue

        method = req.get("method")
        params = req.get("params") or {}

        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": proto,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": SERVER_INFO,
                        },
                    }
                )
            elif method == "ping":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
            elif method == "tools/list":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SPEC}})
            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments") or {}
                res = _dispatch_tool(tool_name, tool_args)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                            "isError": not bool(res.get("success", True)),
                        },
                    }
                )
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            )


if __name__ == "__main__":
    main()
