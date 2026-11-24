"""Skill usage telemetry, pinning, and Curator lifecycle compatible with .usage.json."""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from common import atomic_write_text, file_lock, get_hermes_config_path, get_hermes_skills_dir

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

_BACKUP_EXCLUDES = {".curator_backups", ".hub", ".locks", ".git", ".usage.json.lock"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_file() -> Path:
    return get_hermes_skills_dir() / ".usage.json"


def _backups_dir() -> Path:
    return get_hermes_skills_dir() / ".curator_backups"


def _empty_record() -> Dict[str, Any]:
    return {
        "archived_at": None,
        "created_at": _now_iso(),
        "created_by": None,
        "last_patched_at": None,
        "last_reused_patch_generation": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "patch_count": 0,
        "patch_generation": 0,
        "pinned": False,
        "state": STATE_ACTIVE,
        "use_count": 0,
        "view_count": 0,
    }


def _backfilled(rec: Any) -> Dict[str, Any]:
    if not isinstance(rec, dict):
        return _empty_record()
    merged = dict(rec)
    for k, v in _empty_record().items():
        if k not in merged:
            merged[k] = v
    return merged


def _usage_lock():
    return file_lock(_usage_file().with_suffix(".json.lock"))


def load_curator_settings() -> Dict[str, Any]:
    settings: Dict[str, Any] = {
        "enabled": True,
        "interval_hours": 168,
        "stale_after_days": 14,
        "archive_after_days": 30,
        "backup_keep": 5,
    }
    cfg_path = get_hermes_config_path()
    if not cfg_path.exists():
        return settings
    try:
        raw = cfg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return settings

    parsed: Dict[str, Any] = {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            parsed = loaded
    except Exception:
        parsed = {}

    cur = parsed.get("curator") if isinstance(parsed.get("curator"), dict) else {}
    if cur:
        if isinstance(cur.get("enabled"), bool):
            settings["enabled"] = cur["enabled"]
        for key in ("interval_hours", "stale_after_days", "archive_after_days"):
            if isinstance(cur.get(key), (int, float)) and cur[key] > 0:
                settings[key] = int(cur[key])
        bak = cur.get("backup") if isinstance(cur.get("backup"), dict) else {}
        if isinstance(bak.get("keep"), int) and bak["keep"] > 0:
            settings["backup_keep"] = bak["keep"]
        return settings

    for key in ("interval_hours", "stale_after_days", "archive_after_days"):
        m = re.search(rf"^\s*{key}:\s*(\d+)\b", raw, re.MULTILINE)
        if m and int(m.group(1)) > 0:
            settings[key] = int(m.group(1))
    return settings


def load_usage() -> Dict[str, Dict[str, Any]]:
    path = _usage_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_usage(data: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_text(_usage_file(), json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def get_record(skill_name: str) -> Dict[str, Any]:
    return _backfilled(load_usage().get(skill_name))


def _mutate_record(skill_name: str, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    with _usage_lock():
        data = load_usage()
        rec = _backfilled(data.get(skill_name))
        mutator(rec)
        data[skill_name] = rec
        _save_usage(data)
        return rec


def bump_view(skill_name: str) -> Optional[Dict[str, Any]]:
    if not skill_name:
        return None

    def _apply(rec: Dict[str, Any]) -> None:
        rec["view_count"] = _non_negative_int(rec.get("view_count")) + 1
        rec["last_viewed_at"] = _now_iso()

    return _mutate_record(skill_name, _apply)


def bump_use(skill_name: str) -> Optional[Dict[str, Any]]:
    if not skill_name:
        return None

    def _apply(rec: Dict[str, Any]) -> None:
        uses = _non_negative_int(rec.get("use_count"))
        gen = _non_negative_int(rec.get("patch_generation"))
        last_reused = min(_non_negative_int(rec.get("last_reused_patch_generation")), gen)
        reused_after_patch = uses > 0 and gen > last_reused
        rec["use_count"] = uses + 1
        rec["last_used_at"] = _now_iso()
        rec["patch_generation"] = gen
        if reused_after_patch:
            rec["last_reused_patch_generation"] = gen
        if rec.get("state") == STATE_STALE:
            rec["state"] = STATE_ACTIVE

    return _mutate_record(skill_name, _apply)


def bump_patch(skill_name: str) -> Optional[Dict[str, Any]]:
    if not skill_name:
        return None

    def _apply(rec: Dict[str, Any]) -> None:
        rec["patch_count"] = _non_negative_int(rec.get("patch_count")) + 1
        rec["patch_generation"] = _non_negative_int(rec.get("patch_generation")) + 1
        rec["last_patched_at"] = _now_iso()
        if rec.get("state") == STATE_STALE:
            rec["state"] = STATE_ACTIVE

    return _mutate_record(skill_name, _apply)


def record_created(skill_name: str, created_by: str = "agent") -> Optional[Dict[str, Any]]:
    if not skill_name:
        return None

    def _apply(rec: Dict[str, Any]) -> None:
        rec.clear()
        rec.update(_empty_record())
        rec["created_by"] = created_by
        rec["state"] = STATE_ACTIVE

    return _mutate_record(skill_name, _apply)


def record_archived(skill_name: str) -> Optional[Dict[str, Any]]:
    if not skill_name:
        return None

    def _apply(rec: Dict[str, Any]) -> None:
        rec["state"] = STATE_ARCHIVED
        rec["archived_at"] = _now_iso()

    return _mutate_record(skill_name, _apply)


def record_restored(skill_name: str) -> Optional[Dict[str, Any]]:
    if not skill_name:
        return None

    def _apply(rec: Dict[str, Any]) -> None:
        rec["state"] = STATE_ACTIVE
        rec["archived_at"] = None
        rec["last_used_at"] = _now_iso()

    return _mutate_record(skill_name, _apply)


def set_pinned(skill_name: str, pinned: bool) -> Dict[str, Any]:
    if not skill_name:
        return {"success": False, "error": "skill name cannot be empty."}
    rec = _mutate_record(skill_name, lambda r: r.__setitem__("pinned", bool(pinned)))
    return {"success": True, "name": skill_name, "pinned": bool(rec.get("pinned")), "record": rec}


def is_pinned(skill_name: str) -> bool:
    return bool(load_usage().get(skill_name, {}).get("pinned", False))


def _latest_activity_dt(rec: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[str]]:
    candidates: List[Tuple[datetime, str]] = []
    for k in ("last_used_at", "last_viewed_at", "last_patched_at"):
        dt = _parse_iso(rec.get(k))
        if dt is not None:
            candidates.append((dt, str(rec.get(k))))
    if not candidates:
        return None, None
    return max(candidates, key=lambda x: x[0])


def _days_inactive(rec: Dict[str, Any], now_utc: datetime) -> Optional[float]:
    act_dt, _ = _latest_activity_dt(rec)
    ref_dt = act_dt or _parse_iso(rec.get("created_at"))
    if ref_dt is None:
        return None
    return round(max(0.0, (now_utc - ref_dt).total_seconds() / 86400.0), 1)


def usage_report(active_skills: Dict[str, Dict[str, Any]], name: Optional[str] = None) -> Dict[str, Any]:
    usage = load_usage()
    now_utc = datetime.now(timezone.utc)

    if name:
        clean = name.strip()
        if clean not in active_skills and clean not in usage:
            return {"success": False, "error": f"Skill '{clean}' not found."}
        rec = _backfilled(usage.get(clean))
        _, act_iso = _latest_activity_dt(rec)
        return {
            "success": True,
            "skill": {
                "name": clean,
                "active_on_disk": clean in active_skills,
                "category": active_skills.get(clean, {}).get("category"),
                "state": rec.get("state", STATE_ACTIVE),
                "pinned": bool(rec.get("pinned", False)),
                "created_by": rec.get("created_by"),
                "use_count": _non_negative_int(rec.get("use_count")),
                "view_count": _non_negative_int(rec.get("view_count")),
                "patch_count": _non_negative_int(rec.get("patch_count")),
                "last_activity_at": act_iso,
                "days_inactive": _days_inactive(rec, now_utc),
                "archived_at": rec.get("archived_at"),
            },
        }

    rows: List[Dict[str, Any]] = []
    for skill_name, info in active_skills.items():
        rec = _backfilled(usage.get(skill_name))
        _, act_iso = _latest_activity_dt(rec)
        rows.append(
            {
                "name": skill_name,
                "category": info.get("category"),
                "state": rec.get("state", STATE_ACTIVE),
                "pinned": bool(rec.get("pinned", False)),
                "created_by": rec.get("created_by"),
                "use_count": _non_negative_int(rec.get("use_count")),
                "view_count": _non_negative_int(rec.get("view_count")),
                "patch_count": _non_negative_int(rec.get("patch_count")),
                "last_activity_at": act_iso,
                "days_inactive": _days_inactive(rec, now_utc),
            }
        )
    rows.sort(key=lambda r: (0 if r["pinned"] else 1, -(r["use_count"]), r["name"]))
    return {"success": True, "count": len(rows), "skills": rows}


def create_curator_backup(reason: str = "curate") -> Path:
    skills_dir = get_hermes_skills_dir().resolve()
    backups_root = _backups_dir()
    backups_root.mkdir(parents=True, exist_ok=True)
    settings = load_curator_settings()
    keep = max(1, int(settings["backup_keep"]))

    base_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    snap_dir = backups_root / base_id
    suffix = 1
    while snap_dir.exists():
        snap_dir = backups_root / f"{base_id}-{suffix:02d}"
        suffix += 1
    snap_dir.mkdir(parents=True, exist_ok=True)

    archive_path = snap_dir / "skills.tar.gz"
    skills_counted = 0

    def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        parts = Path(tarinfo.name).parts
        if any(p in _BACKUP_EXCLUDES for p in parts):
            return None
        return tarinfo

    with tarfile.open(archive_path, "w:gz") as tf:
        for item in sorted(skills_dir.iterdir()):
            if item.name in _BACKUP_EXCLUDES:
                continue
            tf.add(item, arcname=item.name, filter=_filter)
            if item.is_dir():
                skills_counted += sum(1 for _ in item.rglob("SKILL.md"))

    manifest = {
        "created_at": _now_iso(),
        "reason": reason,
        "archive": archive_path.name,
        "skills_counted": skills_counted,
    }
    atomic_write_text(snap_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")

    existing = sorted((p for p in backups_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for old_dir in existing[keep:]:
        with suppress(OSError):
            shutil.rmtree(old_dir)

    return snap_dir


def run_curator(
    active_skills: Dict[str, Dict[str, Any]],
    *,
    dry_run: bool = True,
    archive_callback: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Evaluate active agent-created skills for stale/archive/reactivate transitions."""
    settings = load_curator_settings()
    stale_days = float(settings["stale_after_days"])
    archive_days = float(settings["archive_after_days"])
    now_utc = datetime.now(timezone.utc)
    usage = load_usage()

    to_stale: List[Dict[str, Any]] = []
    to_archive: List[Dict[str, Any]] = []
    to_activate: List[Dict[str, Any]] = []
    pinned_skipped: List[str] = []

    for skill_name in sorted(active_skills):
        rec = usage.get(skill_name)
        if not isinstance(rec, dict) or rec.get("created_by") != "agent":
            continue
        if rec.get("pinned"):
            pinned_skipped.append(skill_name)
            continue
        days = _days_inactive(rec, now_utc)
        if days is None:
            continue
        cur_state = rec.get("state", STATE_ACTIVE)
        entry_info = {"name": skill_name, "days_inactive": days, "state": cur_state}
        if days >= archive_days:
            to_archive.append(entry_info)
        elif days >= stale_days and cur_state != STATE_STALE:
            to_stale.append(entry_info)
        elif days < stale_days and cur_state == STATE_STALE:
            to_activate.append(entry_info)

    result: Dict[str, Any] = {
        "success": True,
        "dry_run": bool(dry_run),
        "thresholds": {"stale_after_days": int(stale_days), "archive_after_days": int(archive_days)},
        "to_stale": to_stale,
        "to_archive": to_archive,
        "to_activate": to_activate,
        "pinned_skipped": pinned_skipped,
    }
    if dry_run:
        return result

    backup_path: Optional[Path] = None
    if to_archive:
        backup_path = create_curator_backup(reason="curate")

    if to_stale or to_activate:
        with _usage_lock():
            live_usage = load_usage()
            for item in to_stale:
                rec = _backfilled(live_usage.get(item["name"]))
                rec["state"] = STATE_STALE
                live_usage[item["name"]] = rec
            for item in to_activate:
                rec = _backfilled(live_usage.get(item["name"]))
                rec["state"] = STATE_ACTIVE
                live_usage[item["name"]] = rec
            _save_usage(live_usage)

    archived_names: List[str] = []
    archive_errors: List[Dict[str, Any]] = []
    if archive_callback is not None:
        for item in to_archive:
            res = archive_callback(item["name"])
            if res.get("success"):
                archived_names.append(item["name"])
            else:
                archive_errors.append({"name": item["name"], "error": res.get("error")})

    result["archived"] = archived_names
    if archive_errors:
        result["archive_errors"] = archive_errors
    if backup_path is not None:
        result["backup_path"] = str(backup_path)
    return result
