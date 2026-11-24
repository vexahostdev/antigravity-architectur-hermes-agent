"""Cross-platform paths, file locking, atomic writes, config limits, and secret redaction."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

_SETTINGS_CACHE: Optional[Dict[str, Any]] = None


def get_config_root() -> Path:
    return Path(os.environ.get("HERMES_CORE_CONFIG_ROOT", str(Path.home() / ".gemini" / "config")))


def get_data_dir() -> Path:
    return Path(os.environ.get("HERMES_CORE_DATA_DIR", str(PLUGIN_ROOT / "data")))


def get_plugin_skills_dir() -> Path:
    return Path(os.environ.get("HERMES_CORE_PLUGIN_SKILLS_DIR", str(PLUGIN_ROOT / "skills")))


def get_global_skills_json() -> Path:
    override = os.environ.get("HERMES_CORE_SKILLS_JSON")
    if override:
        return Path(override)
    return get_config_root() / "skills.json"


def get_brain_dir() -> Path:
    return Path(os.environ.get("HERMES_CORE_BRAIN_DIR", str(Path.home() / ".gemini" / "antigravity" / "brain")))


def _load_plugin_settings() -> Dict[str, Any]:
    path = get_data_dir() / "plugin_settings.json"
    if not path.exists():
        return {"mode": "standalone", "hermes_home": None}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except (OSError, json.JSONDecodeError):
        pass
    return {"mode": "standalone", "hermes_home": None}


def _shared_hermes_home() -> Optional[Path]:
    settings = _load_plugin_settings()
    if settings.get("mode") != "shared":
        return None
    raw = settings.get("hermes_home")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def get_memory_dir() -> Path:
    override = os.environ.get("HERMES_CORE_MEMORY_DIR")
    if override:
        return Path(override)
    hermes_home = _shared_hermes_home()
    if hermes_home is not None:
        return hermes_home / "memories"
    return get_data_dir() / "memories"


def get_hermes_skills_dir() -> Path:
    override = os.environ.get("HERMES_CORE_SKILLS_DIR")
    if override:
        return Path(override)
    hermes_home = _shared_hermes_home()
    if hermes_home is not None:
        return hermes_home / "skills"
    return get_data_dir() / "skills"


def get_hermes_state_db() -> Optional[Path]:
    override = os.environ.get("HERMES_CORE_STATE_DB")
    if override:
        return Path(override)
    hermes_home = _shared_hermes_home()
    if hermes_home is not None:
        return hermes_home / "state.db"
    return None


def get_hermes_config_path() -> Path:
    override = os.environ.get("HERMES_CORE_CONFIG_YAML")
    if override:
        return Path(override)
    hermes_home = _shared_hermes_home()
    if hermes_home is not None:
        return hermes_home / "config.yaml"
    return get_data_dir() / "config.yaml"


_HELD_LOCKS = threading.local()


@contextmanager
def file_lock(lock_path: Path, timeout: float = 3.0):
    """Cross-platform exclusive file lock (fcntl on POSIX, msvcrt on Windows). Re-entrant per thread."""
    lock_path = Path(lock_path)
    held = getattr(_HELD_LOCKS, "paths", None)
    if held is None:
        held = _HELD_LOCKS.paths = set()

    norm_key = os.path.normcase(os.path.abspath(str(lock_path)))
    if (fcntl is None and msvcrt is None) or norm_key in held:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if msvcrt is not None and (not lock_path.exists() or lock_path.stat().st_size == 0):
        with suppress(OSError):
            lock_path.write_text(" ", encoding="utf-8")

    raw_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fd = os.fdopen(raw_fd, "r+", encoding="utf-8")
    locked = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {lock_path.name}")
                time.sleep(0.04)

        held.add(norm_key)
        try:
            yield
        finally:
            held.discard(norm_key)
    finally:
        if locked:
            with suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                else:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        fd.close()


def load_hermes_limits() -> Dict[str, Any]:
    """Read memory & skills limits from config.yaml with safe defaults (stdlib regex fallback included)."""
    limits: Dict[str, Any] = {
        "memory_char_limit": 2200,
        "user_char_limit": 1375,
        "memory_nudge_interval": 10,
        "skill_nudge_interval": 15,
        "memory_enabled": True,
        "user_profile_enabled": True,
    }
    cfg_path = get_hermes_config_path()
    if not cfg_path.exists():
        return limits
    try:
        raw = cfg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return limits

    parsed: Dict[str, Any] = {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            parsed = loaded
    except Exception:
        parsed = {}

    if parsed:
        mem = parsed.get("memory") if isinstance(parsed.get("memory"), dict) else {}
        sk = parsed.get("skills") if isinstance(parsed.get("skills"), dict) else {}
        if isinstance(mem.get("memory_char_limit"), int) and mem["memory_char_limit"] > 0:
            limits["memory_char_limit"] = mem["memory_char_limit"]
        if isinstance(mem.get("user_char_limit"), int) and mem["user_char_limit"] > 0:
            limits["user_char_limit"] = mem["user_char_limit"]
        if isinstance(mem.get("nudge_interval"), int) and mem["nudge_interval"] > 0:
            limits["memory_nudge_interval"] = mem["nudge_interval"]
        if isinstance(mem.get("memory_enabled"), bool):
            limits["memory_enabled"] = mem["memory_enabled"]
        if isinstance(mem.get("user_profile_enabled"), bool):
            limits["user_profile_enabled"] = mem["user_profile_enabled"]
        if isinstance(sk.get("creation_nudge_interval"), int) and sk["creation_nudge_interval"] > 0:
            limits["skill_nudge_interval"] = sk["creation_nudge_interval"]
        return limits

    for key, target in (
        ("memory_char_limit", "memory_char_limit"),
        ("user_char_limit", "user_char_limit"),
        ("nudge_interval", "memory_nudge_interval"),
        ("creation_nudge_interval", "skill_nudge_interval"),
    ):
        m = re.search(rf"^\s*{key}:\s*(\d+)\b", raw, re.MULTILINE)
        if m and int(m.group(1)) > 0:
            limits[target] = int(m.group(1))
    return limits


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 LF text via sibling temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()


_SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\b(?:pk|rk)_(?:live|test)_[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{25,}\b"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\b\d+\|[A-Za-z0-9]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(api_?key|client_secret|db_password|password|passwd|pass|access_token|auth_token|bot_token|token)\s*([:=])\s*\S{8,}"), r"\1\2[REDACTED]"),
)


def redact_secrets(text: str) -> str:
    """Scrub tokens, private keys, and credentials from session search excerpts."""
    if not text:
        return ""
    out = text
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out
