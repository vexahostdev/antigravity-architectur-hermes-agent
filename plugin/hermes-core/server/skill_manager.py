"""Live-shared skills manager synced via Antigravity global skills.json + Curator telemetry."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import (
    atomic_write_text,
    get_global_skills_json,
    get_hermes_skills_dir,
    get_plugin_skills_dir,
)
from skill_curator import (
    bump_patch,
    bump_view,
    is_pinned,
    record_archived,
    record_created,
    record_restored,
    run_curator,
    set_pinned,
    usage_report,
)
from threat_scanner import first_threat_error

_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VALID_CAT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _error(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _format_skills_entry_path(directory: Path) -> str:
    """Return native absolute path (Antigravity does not expand '~/' in skills.json)."""
    return str(directory.resolve())


def _legacy_home_prefix(directory: Path) -> Optional[str]:
    resolved = directory.resolve()
    home = Path.home().resolve()
    try:
        rel = resolved.relative_to(home)
        rel_posix = rel.as_posix()
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


def _parse_frontmatter(text: str) -> Tuple[Optional[str], Optional[str], bool]:
    m = _FM_RE.match(text.replace("\r\n", "\n"))
    if not m:
        return None, None, False
    fm = m.group(1)
    name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    name = name_m.group(1).strip().strip("\"'") if name_m else None
    desc = desc_m.group(1).strip().strip("\"'") if desc_m else None
    return name, desc, bool(name and desc)


def _discover_active_skills() -> Dict[str, Dict[str, Any]]:
    skills_root = get_hermes_skills_dir()
    found: Dict[str, Dict[str, Any]] = {}
    if not skills_root.exists():
        return found

    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        try:
            rel = skill_md.relative_to(skills_root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        skill_dir = skill_md.parent
        leaf = skill_dir.name
        if leaf in found:
            continue
        category = "/".join(rel.parts[:-2]) if len(rel.parts) > 2 else None
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        fm_name, fm_desc, _ = _parse_frontmatter(text)
        support_files = sorted(
            str(p.relative_to(skill_dir)).replace("\\", "/")
            for p in skill_dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md" and not p.name.startswith(".")
        )
        found[leaf] = {
            "name": leaf,
            "frontmatter_name": fm_name or leaf,
            "category": category,
            "description": fm_desc or "",
            "dir": skill_dir,
            "skill_md": skill_md,
            "support_files": support_files,
        }
    return found


def sync_skills_registry() -> Dict[str, Any]:
    """Register active skill parent folders in skills.json using absolute paths and exact excludes."""
    skills_root = get_hermes_skills_dir().resolve()
    active = _discover_active_skills()

    parent_dirs_set = {info["dir"].parent.resolve() for info in active.values()}

    def _sort_key(p: Path) -> Tuple[int, str]:
        if p == skills_root:
            return (0, "")
        try:
            return (1, p.relative_to(skills_root).as_posix().lower())
        except ValueError:
            return (2, p.as_posix().lower())

    ordered_parents = sorted(parent_dirs_set, key=_sort_key)
    managed_entries: List[Dict[str, Any]] = []
    for pdir in ordered_parents:
        formatted = _format_skills_entry_path(pdir)
        if pdir == skills_root:
            hidden_names = sorted(
                (
                    {p.name for p in skills_root.iterdir() if p.is_dir() and p.name.startswith(".")}
                    if skills_root.exists()
                    else set()
                )
                | {".archive", ".curator_backups", ".hub", ".locks"}
            )
            managed_entries.append({"path": formatted, "exclude": hidden_names})
        else:
            managed_entries.append({"path": formatted})

    skills_json_path = get_global_skills_json()
    existing_raw = ""
    existing_data: Dict[str, Any] = {}
    if skills_json_path.exists():
        try:
            existing_raw = skills_json_path.read_text(encoding="utf-8")
            parsed = json.loads(existing_raw)
            if isinstance(parsed, dict):
                existing_data = parsed
        except Exception:
            existing_data = {}

    prefixes_list = [str(skills_root), skills_root.as_posix()]
    if legacy := _legacy_home_prefix(skills_root):
        prefixes_list.append(legacy)
    managed_prefixes = tuple(prefixes_list)

    preserved_entries: List[Any] = []
    for item in existing_data.get("entries") or []:
        if isinstance(item, dict) and _is_managed_entry(str(item.get("path") or ""), managed_prefixes):
            continue
        preserved_entries.append(item)

    new_data = dict(existing_data)
    new_data["entries"] = preserved_entries + managed_entries
    new_text = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    wrote = new_text != existing_raw
    if wrote:
        atomic_write_text(skills_json_path, new_text)

    unlinked_junctions: List[str] = []
    plugin_skills_dir = get_plugin_skills_dir()
    if plugin_skills_dir.exists():
        for entry in sorted(plugin_skills_dir.iterdir()):
            if hasattr(os.path, "isjunction") and os.path.isjunction(entry):
                os.rmdir(entry)
                unlinked_junctions.append(entry.name)
            elif entry.is_symlink():
                entry.unlink()
                unlinked_junctions.append(entry.name)
        with suppress(OSError):
            plugin_skills_dir.rmdir()

    return {
        "success": True,
        "active_count": len(active),
        "skills_json": str(skills_json_path),
        "updated_skills_json": wrote,
        "registered_dirs": [e["path"] for e in managed_entries],
        "unlinked_junctions": unlinked_junctions,
    }


sync_skill_junctions = sync_skills_registry


def resolve_skill_name_from_path(file_path: str) -> Optional[str]:
    """Return active skill leaf name if *file_path* points inside the managed active skills dir."""
    if not file_path:
        return None
    try:
        target = Path(file_path).resolve()
        skills_root = get_hermes_skills_dir().resolve()
        rel = target.relative_to(skills_root)
    except (OSError, ValueError):
        return None
    if not rel.parts or any(part.startswith(".") for part in rel.parts):
        return None
    cur = target if target.is_dir() else target.parent
    while True:
        if (cur / "SKILL.md").is_file():
            return cur.name
        if cur == skills_root or cur.parent == cur:
            break
        cur = cur.parent
    return None


def _safe_rel_path(skill_dir: Path, rel_path: str) -> Tuple[Optional[Path], Optional[str]]:
    clean = (rel_path or "").strip().replace("\\", "/")
    if not clean:
        return None, "file_path cannot be empty."
    if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
        return None, "file_path must be relative to the skill directory."
    parts = [p for p in clean.split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None, "Path traversal ('..' or '.') is not allowed."
    target = (skill_dir / "/".join(parts)).resolve()
    root_resolved = skill_dir.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return None, "Resolved path escapes the skill directory."
    return target, None


class SkillManager:
    """CRUD manager for live-shared skills."""

    def list_skills(self) -> Dict[str, Any]:
        active = _discover_active_skills()
        items = [
            {
                "name": info["name"],
                "category": info["category"],
                "description": info["description"],
                "support_files": info["support_files"],
                "path": str(info["dir"]),
            }
            for info in active.values()
        ]
        return {"success": True, "count": len(items), "skills": items}

    def view(self, name: str, file_path: Optional[str] = None) -> Dict[str, Any]:
        active = _discover_active_skills()
        info = active.get((name or "").strip())
        if not info:
            return _error(f"Skill '{name}' not found.", available=sorted(active.keys()))
        skill_dir: Path = info["dir"]
        if not file_path or file_path.strip() in ("", "SKILL.md"):
            target = info["skill_md"]
            rel = "SKILL.md"
        else:
            target, err = _safe_rel_path(skill_dir, file_path)
            if err or target is None:
                return _error(err or "Invalid file_path.")
            rel = file_path.strip().replace("\\", "/")
            if not target.is_file():
                return _error(f"File '{rel}' not found in skill '{name}'.", support_files=info["support_files"])
        content = target.read_text(encoding="utf-8", errors="replace")
        bump_view(info["name"])
        return {
            "success": True,
            "name": info["name"],
            "category": info["category"],
            "file_path": rel,
            "support_files": info["support_files"],
            "content": content,
        }

    def create(self, name: str, content: str, category: Optional[str] = None) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not _VALID_NAME_RE.fullmatch(clean_name):
            return _error("Skill name must match ^[a-z0-9][a-z0-9-]{0,63}$.")
        clean_cat = (category or "").strip() or None
        if clean_cat and not _VALID_CAT_RE.fullmatch(clean_cat):
            return _error("Category must match ^[a-z0-9][a-z0-9_-]{0,63}$.")

        active = _discover_active_skills()
        if clean_name in active:
            return _error(f"Skill '{clean_name}' already exists; use 'edit' or 'patch' instead.")

        raw = (content or "").replace("\r\n", "\n")
        if not raw.strip():
            return _error("content cannot be empty.")
        if threat := first_threat_error(raw, scope="skill"):
            return _error(threat)
        _, _, valid_fm = _parse_frontmatter(raw)
        if not valid_fm:
            return _error("SKILL.md must begin with YAML frontmatter containing both 'name' and 'description'.")

        skills_root = get_hermes_skills_dir()
        skill_dir = (skills_root / clean_cat / clean_name) if clean_cat else (skills_root / clean_name)
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            return _error(f"Skill directory already exists at {skill_dir}.")

        atomic_write_text(skill_md, raw if raw.endswith("\n") else raw + "\n")
        record_created(clean_name, created_by="agent")
        sync_res = sync_skills_registry()
        return {
            "success": True,
            "message": f"Skill '{clean_name}' created and registered in skills.json.",
            "name": clean_name,
            "category": clean_cat,
            "path": str(skill_dir),
            "sync": sync_res,
        }

    def edit(self, name: str, content: str) -> Dict[str, Any]:
        active = _discover_active_skills()
        info = active.get((name or "").strip())
        if not info:
            return _error(f"Skill '{name}' not found.", available=sorted(active.keys()))
        raw = (content or "").replace("\r\n", "\n")
        if not raw.strip():
            return _error("content cannot be empty.")
        if threat := first_threat_error(raw, scope="skill"):
            return _error(threat)
        _, _, valid_fm = _parse_frontmatter(raw)
        if not valid_fm:
            return _error("SKILL.md must begin with YAML frontmatter containing both 'name' and 'description'.")
        atomic_write_text(info["skill_md"], raw if raw.endswith("\n") else raw + "\n")
        bump_patch(info["name"])
        return {"success": True, "message": f"Skill '{name}' updated.", "path": str(info["skill_md"])}

    def patch(
        self,
        name: str,
        old_string: str,
        new_string: str,
        file_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        active = _discover_active_skills()
        info = active.get((name or "").strip())
        if not info:
            return _error(f"Skill '{name}' not found.", available=sorted(active.keys()))
        if not old_string:
            return _error("old_string cannot be empty.")

        rel = (file_path or "SKILL.md").strip().replace("\\", "/")
        if rel == "SKILL.md":
            target = info["skill_md"]
        else:
            target, err = _safe_rel_path(info["dir"], rel)
            if err or target is None:
                return _error(err or "Invalid file_path.")
            if not target.is_file():
                return _error(f"File '{rel}' does not exist in skill '{name}'.")

        existing = target.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        needle = old_string.replace("\r\n", "\n")
        count = existing.count(needle)
        if count == 0:
            return _error(f"old_string not found in {rel}.")
        if count > 1:
            return _error(f"old_string matched {count} times in {rel}; provide a unique surrounding block.")

        updated = existing.replace(needle, (new_string or "").replace("\r\n", "\n"), 1)
        if threat := first_threat_error(updated, scope="skill"):
            return _error(threat)
        if rel == "SKILL.md":
            _, _, valid_fm = _parse_frontmatter(updated)
            if not valid_fm:
                return _error("Patched SKILL.md lost required YAML frontmatter (name + description).")

        atomic_write_text(target, updated if updated.endswith("\n") else updated + "\n")
        bump_patch(info["name"])
        return {"success": True, "message": f"Patched {rel} in skill '{name}'.", "path": str(target)}

    def write_file(self, name: str, file_path: str, file_content: str) -> Dict[str, Any]:
        active = _discover_active_skills()
        info = active.get((name or "").strip())
        if not info:
            return _error(f"Skill '{name}' not found.", available=sorted(active.keys()))
        rel = (file_path or "").strip().replace("\\", "/")
        if rel == "SKILL.md":
            return _error("Use 'edit' or 'patch' to modify SKILL.md.")
        target, err = _safe_rel_path(info["dir"], rel)
        if err or target is None:
            return _error(err or "Invalid file_path.")
        normalized = (file_content or "").replace("\r\n", "\n")
        if threat := first_threat_error(normalized, scope="skill"):
            return _error(threat)
        atomic_write_text(target, normalized)
        bump_patch(info["name"])
        return {"success": True, "message": f"Wrote '{rel}' in skill '{name}'.", "path": str(target)}

    def remove_file(self, name: str, file_path: str) -> Dict[str, Any]:
        active = _discover_active_skills()
        info = active.get((name or "").strip())
        if not info:
            return _error(f"Skill '{name}' not found.", available=sorted(active.keys()))
        rel = (file_path or "").strip().replace("\\", "/")
        if rel == "SKILL.md":
            return _error("Cannot delete SKILL.md via remove_file; use action='delete' to archive the skill.")
        target, err = _safe_rel_path(info["dir"], rel)
        if err or target is None:
            return _error(err or "Invalid file_path.")
        if not target.is_file():
            return _error(f"File '{rel}' not found in skill '{name}'.")
        target.unlink()
        return {"success": True, "message": f"Removed '{rel}' from skill '{name}'."}

    def delete(self, name: str, *, bypass_pin: bool = False) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        active = _discover_active_skills()
        info = active.get(clean_name)
        if not info:
            return _error(f"Skill '{clean_name}' not found.", available=sorted(active.keys()))
        if not bypass_pin and is_pinned(clean_name):
            return _error(f"Skill '{clean_name}' is pinned; unpin it first before archiving/deleting.")

        plugin_dst = get_plugin_skills_dir() / clean_name
        if hasattr(os.path, "isjunction") and os.path.isjunction(plugin_dst):
            os.rmdir(plugin_dst)
        elif plugin_dst.is_symlink():
            plugin_dst.unlink()

        archive_dir = get_hermes_skills_dir() / ".archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_path = archive_dir / clean_name
        if archived_path.exists():
            stamp = time.strftime("%Y%m%d_%H%M%S")
            archived_path = archive_dir / f"{clean_name}-{stamp}"
        shutil.move(str(info["dir"]), str(archived_path))
        record_archived(clean_name)
        sync_res = sync_skills_registry()
        return {
            "success": True,
            "message": f"Skill '{clean_name}' archived to {archived_path} and synced.",
            "archived_to": str(archived_path),
            "sync": sync_res,
        }

    def _archive_for_curator(self, name: str) -> Dict[str, Any]:
        return self.delete(name, bypass_pin=False)

    def pin(self, name: str) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        active = _discover_active_skills()
        if clean_name not in active:
            return _error(f"Skill '{clean_name}' not found.", available=sorted(active.keys()))
        return set_pinned(clean_name, True)

    def unpin(self, name: str) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        active = _discover_active_skills()
        if clean_name not in active:
            return _error(f"Skill '{clean_name}' not found.", available=sorted(active.keys()))
        return set_pinned(clean_name, False)

    def usage(self, name: Optional[str] = None) -> Dict[str, Any]:
        return usage_report(_discover_active_skills(), name=name)

    def curate(self, *, dry_run: bool = True) -> Dict[str, Any]:
        active = _discover_active_skills()
        res = run_curator(active, dry_run=dry_run, archive_callback=self._archive_for_curator)
        if not dry_run and res.get("archived"):
            res["sync"] = sync_skills_registry()
        return res

    def restore(self, name: str, category: Optional[str] = None) -> Dict[str, Any]:
        clean_name = (name or "").strip()
        if not _VALID_NAME_RE.fullmatch(clean_name):
            return _error("Skill name must match ^[a-z0-9][a-z0-9-]{0,63}$.")
        clean_cat = (category or "").strip() or None
        if clean_cat and not _VALID_CAT_RE.fullmatch(clean_cat):
            return _error("Category must match ^[a-z0-9][a-z0-9_-]{0,63}$.")

        active = _discover_active_skills()
        if clean_name in active:
            return _error(f"Skill '{clean_name}' is already active at {active[clean_name]['dir']}.")

        archive_dir = get_hermes_skills_dir() / ".archive"
        if not archive_dir.exists():
            return _error(f"No .archive directory exists; '{clean_name}' not found.")

        candidates: List[Path] = []
        exact = archive_dir / clean_name
        if exact.is_dir() and (exact / "SKILL.md").is_file():
            candidates.append(exact)
        for p in sorted(archive_dir.glob(f"{clean_name}-*"), reverse=True):
            if p.is_dir() and (p / "SKILL.md").is_file():
                candidates.append(p)
        if not candidates:
            return _error(f"Archived skill '{clean_name}' not found in {archive_dir}.")

        src_dir = candidates[0]
        skills_root = get_hermes_skills_dir()
        dst_dir = (skills_root / clean_cat / clean_name) if clean_cat else (skills_root / clean_name)
        if dst_dir.exists():
            return _error(f"Destination '{dst_dir}' already exists.")
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(dst_dir))
        record_restored(clean_name)
        sync_res = sync_skills_registry()
        return {
            "success": True,
            "message": f"Skill '{clean_name}' restored from archive.",
            "name": clean_name,
            "path": str(dst_dir),
            "sync": sync_res,
        }


if __name__ == "__main__":
    if "--sync" in sys.argv:
        print(json.dumps(sync_skills_registry(), ensure_ascii=False, indent=2))
    else:
        print("Usage: python skill_manager.py --sync")
