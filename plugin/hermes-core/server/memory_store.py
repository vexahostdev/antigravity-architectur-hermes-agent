"""Bounded curated memory (MEMORY.md / USER.md) with cross-platform locking and auto-backups."""

from __future__ import annotations

import shutil
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import atomic_write_text, file_lock, get_memory_dir, load_hermes_limits
from threat_scanner import first_threat_error

ENTRY_DELIMITER = "\n§\n"

MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
}


def scan_memory_content(content: str) -> Optional[str]:
    """Reject prompt-injection, exfiltration, or hardcoded-secret payloads."""
    return first_threat_error(content, scope="strict")


def _error(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _parse_entries(raw: str) -> List[str]:
    if not raw:
        return []
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    entries = [part.strip() for part in normalized.split(ENTRY_DELIMITER)]
    return list(dict.fromkeys(e for e in entries if e))


def _render_entries(entries: List[str]) -> str:
    return ENTRY_DELIMITER.join(entries) if entries else ""


def _char_count(entries: List[str]) -> int:
    return len(_render_entries(entries))


def _find_unique_entry(entries: List[str], old_text: str) -> Tuple[Optional[int], Optional[str], List[str]]:
    """Return `(index, error_msg, matched_entries)`. Exact whole-entry match wins."""
    exact = [i for i, e in enumerate(entries) if e == old_text]
    if exact:
        return exact[0], None, [entries[exact[0]]]
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if not matches:
        return None, f"No entry matched '{old_text}'.", []
    distinct = list(dict.fromkeys(entries[i] for i in matches))
    if len(distinct) > 1:
        return None, "Multiple entries matched; provide a more specific substring.", distinct
    return matches[0], None, [entries[matches[0]]]


def _backup_memory_file(path: Path, keep: int = 15) -> Optional[Path]:
    """Snapshot existing MEMORY.md / USER.md into memories/.backups/ before mutating."""
    if not path.exists():
        return None
    try:
        if path.stat().st_size == 0:
            return None
    except OSError:
        return None

    backups_dir = path.parent / ".backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    bak_path = backups_dir / f"{path.stem}_{stamp}.bak"
    shutil.copy2(path, bak_path)

    old_snaps = sorted(backups_dir.glob(f"{path.stem}_*.bak"), key=lambda p: p.name, reverse=True)
    for stale in old_snaps[keep:]:
        with suppress(OSError):
            stale.unlink()
    return bak_path


class MemoryStore:
    """Concurrent-safe, bounded curated memory matching Hermes MEMORY.md / USER.md."""

    def __init__(self) -> None:
        limits = load_hermes_limits()
        self.memory_char_limit = int(limits["memory_char_limit"])
        self.user_char_limit = int(limits["user_char_limit"])
        self.memory_enabled = bool(limits["memory_enabled"])
        self.user_profile_enabled = bool(limits["user_profile_enabled"])

    def _validate_target(self, target: str) -> Optional[str]:
        if target not in ("memory", "user"):
            return f"Invalid target '{target}'. Expected 'memory' or 'user'."
        if target == "memory" and not self.memory_enabled:
            return "Memory store is disabled in config."
        if target == "user" and not self.user_profile_enabled:
            return "User profile store is disabled in config."
        return None

    def _path_for(self, target: str) -> Path:
        filename = "USER.md" if target == "user" else "MEMORY.md"
        return get_memory_dir() / filename

    def _lock_for(self, path: Path) -> Path:
        return path.with_suffix(path.suffix + ".lock")

    def _limit_for(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _read_locked_entries(self, path: Path) -> Tuple[List[str], Optional[str]]:
        if not path.exists():
            return [], None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return [], f"Refusing to mutate {path.name}: file could not be read ({exc})."
        return _parse_entries(raw), None

    def _usage_info(self, target: str, entries: List[str]) -> Dict[str, Any]:
        limit = self._limit_for(target)
        count = _char_count(entries)
        pct = min(100, int((count / limit) * 100)) if limit > 0 else 0
        return {
            "char_count": count,
            "char_limit": limit,
            "usage": f"{count:,}/{limit:,}",
            "usage_pct": f"{pct}% ({count:,}/{limit:,} chars)",
            "entry_count": len(entries),
        }

    def status(self, target: str = "all") -> Dict[str, Any]:
        if target == "all":
            return {
                "success": True,
                "memory": self.status("memory"),
                "user": self.status("user"),
            }
        if err := self._validate_target(target):
            return _error(err)
        path = self._path_for(target)
        with file_lock(self._lock_for(path)):
            entries, read_err = self._read_locked_entries(path)
            if read_err:
                return _error(read_err)
        return {
            "success": True,
            "target": target,
            "file_path": str(path),
            "entries": entries,
            **self._usage_info(target, entries),
        }

    def add(self, target: str, content: str) -> Dict[str, Any]:
        if err := self._validate_target(target):
            return _error(err)
        clean = (content or "").strip()
        if not clean:
            return _error("Content cannot be empty.")
        if threat := scan_memory_content(clean):
            return _error(threat)

        path = self._path_for(target)
        limit = self._limit_for(target)
        with file_lock(self._lock_for(path)):
            entries, read_err = self._read_locked_entries(path)
            if read_err:
                return _error(read_err)
            if clean in entries:
                return {
                    "success": True,
                    "target": target,
                    "message": "Entry already exists (no duplicate added).",
                    **self._usage_info(target, entries),
                }
            new_entries = entries + [clean]
            new_len = _char_count(new_entries)
            if new_len > limit:
                return _error(
                    f"{target} at {_char_count(entries):,}/{limit:,} chars. Adding this entry "
                    f"({len(clean)} chars, total {new_len:,}) would exceed the limit. "
                    f"Consolidate via 'patch' or 'remove' first, then retry 'add'.",
                    current_entries=entries,
                    usage=f"{_char_count(entries):,}/{limit:,}",
                    limit=limit,
                    needed=new_len,
                )
            bak = _backup_memory_file(path)
            atomic_write_text(path, _render_entries(new_entries))
            return {
                "success": True,
                "target": target,
                "message": "Entry added.",
                "backup_file": bak.name if bak else None,
                **self._usage_info(target, new_entries),
            }

    def patch(self, target: str, old_text: str, content: str) -> Dict[str, Any]:
        if err := self._validate_target(target):
            return _error(err)
        needle = (old_text or "").strip()
        replacement = (content or "").strip()
        if not needle:
            return _error("old_text cannot be empty.")
        if replacement and (threat := scan_memory_content(replacement)):
            return _error(threat)

        path = self._path_for(target)
        limit = self._limit_for(target)
        with file_lock(self._lock_for(path)):
            entries, read_err = self._read_locked_entries(path)
            if read_err:
                return _error(read_err)
            idx, match_err, matched = _find_unique_entry(entries, needle)
            if match_err or idx is None:
                return _error(
                    match_err or "Match failed.",
                    matches=matched if matched else None,
                    current_entries=entries,
                )
            entry = entries[idx]
            occurrences = entry.count(needle)
            if occurrences > 1:
                return _error(
                    f"old_text occurs {occurrences} times inside the matched entry; pass a longer unique span.",
                    matched_entry=entry,
                )
            updated = entry.replace(needle, replacement, 1).strip()
            new_entries = list(entries)
            if not updated:
                new_entries.pop(idx)
                msg = "Entry emptied by patch and removed."
            else:
                new_entries[idx] = updated
                new_entries = list(dict.fromkeys(new_entries))
                msg = "Entry patched."
            new_len = _char_count(new_entries)
            if new_len > limit:
                return _error(
                    f"Patched result ({new_len:,}/{limit:,} chars) exceeds the limit.",
                    current_entries=entries,
                    limit=limit,
                )
            bak = _backup_memory_file(path)
            atomic_write_text(path, _render_entries(new_entries))
            return {
                "success": True,
                "target": target,
                "message": msg,
                "updated_entry": updated or None,
                "backup_file": bak.name if bak else None,
                **self._usage_info(target, new_entries),
            }

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        if err := self._validate_target(target):
            return _error(err)
        needle = (old_text or "").strip()
        if not needle:
            return _error("old_text cannot be empty.")

        path = self._path_for(target)
        with file_lock(self._lock_for(path)):
            entries, read_err = self._read_locked_entries(path)
            if read_err:
                return _error(read_err)
            idx, match_err, matched = _find_unique_entry(entries, needle)
            if match_err or idx is None:
                return _error(
                    match_err or "Match failed.",
                    matches=matched if matched else None,
                    current_entries=entries,
                )
            removed_entry = entries[idx]
            new_entries = [e for e in entries if e != removed_entry]
            bak = _backup_memory_file(path)
            atomic_write_text(path, _render_entries(new_entries))
            return {
                "success": True,
                "target": target,
                "message": "Entry removed.",
                "removed_entry": removed_entry,
                "backup_file": bak.name if bak else None,
                **self._usage_info(target, new_entries),
            }

    def batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        if err := self._validate_target(target):
            return _error(err)
        if not isinstance(operations, list) or not operations:
            return _error("operations must be a non-empty list.")

        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                return _error(f"Operation #{i} must be an object.")
            act = op.get("action")
            if act not in ("add", "patch", "remove"):
                return _error(f"Operation #{i} has invalid action '{act}'. Expected add, patch, or remove.")
            if act in ("add", "patch") and op.get("content"):
                if threat := scan_memory_content(str(op["content"])):
                    return _error(f"Operation #{i}: {threat}")

        path = self._path_for(target)
        limit = self._limit_for(target)
        with file_lock(self._lock_for(path)):
            entries, read_err = self._read_locked_entries(path)
            if read_err:
                return _error(read_err)
            working = list(entries)
            for i, op in enumerate(operations):
                act = op["action"]
                content = str(op.get("content") or "").strip()
                old_text = str(op.get("old_text") or "").strip()

                if act == "add":
                    if not content:
                        return _error(f"Operation #{i} (add): content cannot be empty.", current_entries=entries)
                    if content not in working:
                        working.append(content)
                elif act == "patch":
                    if not old_text:
                        return _error(f"Operation #{i} (patch): old_text cannot be empty.", current_entries=entries)
                    idx, match_err, matched = _find_unique_entry(working, old_text)
                    if match_err or idx is None:
                        return _error(
                            f"Operation #{i} (patch): {match_err}",
                            matches=matched if matched else None,
                            current_entries=entries,
                        )
                    entry = working[idx]
                    if entry.count(old_text) > 1:
                        return _error(
                            f"Operation #{i} (patch): old_text appears multiple times in matched entry.",
                            matched_entry=entry,
                        )
                    updated = entry.replace(old_text, content, 1).strip()
                    if not updated:
                        working.pop(idx)
                    else:
                        working[idx] = updated
                        working = list(dict.fromkeys(working))
                elif act == "remove":
                    if not old_text:
                        return _error(f"Operation #{i} (remove): old_text cannot be empty.", current_entries=entries)
                    idx, match_err, matched = _find_unique_entry(working, old_text)
                    if match_err or idx is None:
                        return _error(
                            f"Operation #{i} (remove): {match_err}",
                            matches=matched if matched else None,
                            current_entries=entries,
                        )
                    removed_val = working[idx]
                    working = [e for e in working if e != removed_val]

            final_len = _char_count(working)
            if final_len > limit:
                return _error(
                    f"Batch result ({final_len:,}/{limit:,} chars) exceeds limit; rolled back.",
                    current_entries=entries,
                    limit=limit,
                    needed=final_len,
                )
            bak = _backup_memory_file(path)
            atomic_write_text(path, _render_entries(working))
            return {
                "success": True,
                "target": target,
                "message": f"Applied {len(operations)} batch operation(s).",
                "backup_file": bak.name if bak else None,
                **self._usage_info(target, working),
            }

    def list_backups(self, target: str = "all") -> Dict[str, Any]:
        if target == "all":
            return {
                "success": True,
                "memory": self.list_backups("memory"),
                "user": self.list_backups("user"),
            }
        if err := self._validate_target(target):
            return _error(err)
        path = self._path_for(target)
        backups_dir = path.parent / ".backups"
        if not backups_dir.exists():
            return {"success": True, "target": target, "count": 0, "backups": []}
        snaps = sorted(backups_dir.glob(f"{path.stem}_*.bak"), key=lambda p: p.name, reverse=True)
        items = []
        for snap in snaps:
            try:
                st = snap.stat()
                items.append(
                    {
                        "file": snap.name,
                        "size": st.st_size,
                        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
            except OSError:
                continue
        return {"success": True, "target": target, "count": len(items), "backups": items}

    def render_frozen_snapshot(self) -> str:
        """Render bounded prompt snapshot for SessionStart injection."""
        blocks: List[str] = []
        near_capacity = False
        for target in ("memory", "user"):
            if (target == "memory" and not self.memory_enabled) or (
                target == "user" and not self.user_profile_enabled
            ):
                continue
            path = self._path_for(target)
            with file_lock(self._lock_for(path)):
                entries, read_err = self._read_locked_entries(path)
            if read_err:
                blocks.append(f"[{target} unreadable: {read_err}]")
                continue
            info = self._usage_info(target, entries)
            pct_val = int((info["char_count"] / info["char_limit"]) * 100) if info["char_limit"] > 0 else 0
            if pct_val >= 90:
                near_capacity = True
            header = MEMORY_BLOCK_HEADERS[target]
            sep = "=============================================="
            lines = [sep, f"{header} [{info['usage_pct']}]", sep]
            if entries:
                lines.extend(f"- {e if not scan_memory_content(e) else '[BLOCKED: security threat]'}" for e in entries)
            else:
                lines.append("(empty)")
            blocks.append("\n".join(lines))

        if near_capacity:
            blocks.append(
                "[Memory Capacity Note: >=90% full. To add new durable facts, consolidate or shorten "
                "older entries via memory(action='patch'/'remove'/'batch') first.]"
            )
        return "\n\n".join(blocks)
