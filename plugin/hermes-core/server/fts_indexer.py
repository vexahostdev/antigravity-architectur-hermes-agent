"""Unified SQLite FTS5 cross-session recall over Antigravity transcripts (plus optional Hermes state.db)."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import get_brain_dir, get_data_dir, get_hermes_state_db, redact_secrets

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

_USER_REQ_OPEN_RE = re.compile(r"^\s*<USER_REQUEST>\s*", re.IGNORECASE)
_USER_REQ_CLOSE_RE = re.compile(r"\s*</USER_REQUEST>\s*$", re.IGNORECASE)
_META_BLOCK_RE = re.compile(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", re.IGNORECASE)
_RELATIVE_BOUND_RE = re.compile(r"^(\d+)\s*(h|d|w)$", re.IGNORECASE)
_RELATIVE_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800}

_HIDDEN_HERMES_SOURCES = ("kanban", "subagent", "tool")
_READ_MAX_CONTENT = 2000


def _fts_db_path() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "sessions_fts.db"


def _open_fts_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_fts_db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS indexed_files (
            path TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ag_sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            started_at TEXT,
            started_at_ts REAL,
            ended_at TEXT,
            ended_at_ts REAL,
            message_count INTEGER DEFAULT 0,
            transcript_path TEXT
        );

        CREATE TABLE IF NOT EXISTS ag_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT,
            created_at_ts REAL
        );

        CREATE INDEX IF NOT EXISTS idx_ag_messages_session_step
            ON ag_messages (session_id, step_index);

        CREATE VIRTUAL TABLE IF NOT EXISTS ag_messages_fts USING fts5(
            content,
            content='ag_messages',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS ag_messages_ai AFTER INSERT ON ag_messages BEGIN
            INSERT INTO ag_messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS ag_messages_ad AFTER DELETE ON ag_messages BEGIN
            INSERT INTO ag_messages_fts(ag_messages_fts, rowid, content)
            VALUES('delete', old.id, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS ag_messages_au AFTER UPDATE ON ag_messages BEGIN
            INSERT INTO ag_messages_fts(ag_messages_fts, rowid, content)
            VALUES('delete', old.id, old.content);
            INSERT INTO ag_messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    return conn


def _open_hermes_ro() -> Optional[sqlite3.Connection]:
    db_path = get_hermes_state_db()
    if db_path is None or not db_path.exists():
        return None
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn
    except sqlite3.Error:
        return None


def _parse_iso_to_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.timestamp()
    except Exception:
        return None


def _format_ts(ts: Optional[float], fallback: Optional[str] = None) -> str:
    if ts is not None:
        try:
            return datetime.fromtimestamp(float(ts), tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    if fallback:
        parsed = _parse_iso_to_ts(fallback)
        if parsed is not None:
            return datetime.fromtimestamp(parsed, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        return fallback[:16]
    return "unknown"


def _parse_time_bound(value: Optional[str], *, is_end: bool = False) -> Optional[float]:
    if not value:
        return None
    s = value.strip()
    m = _RELATIVE_BOUND_RE.match(s)
    if m:
        qty = int(m.group(1))
        unit = m.group(2).lower()
        return time.time() - qty * _RELATIVE_UNIT_SECONDS[unit]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, mo, d = (int(part) for part in s.split("-"))
        dt = datetime(y, mo, d, 23 if is_end else 0, 59 if is_end else 0, 59 if is_end else 0, tzinfo=LOCAL_TZ)
        return dt.timestamp()
    return _parse_iso_to_ts(s)


def _clean_user_content(raw: str) -> str:
    text = _META_BLOCK_RE.sub("", raw or "")
    text = _USER_REQ_OPEN_RE.sub("", text)
    text = _USER_REQ_CLOSE_RE.sub("", text)
    return text.strip()


def _extract_session_id(transcript_path: Path) -> str:
    parts = transcript_path.parts
    try:
        idx = parts.index(".system_generated")
        if idx > 0:
            return parts[idx - 1]
    except ValueError:
        pass
    return transcript_path.parent.name


def _sync_one_with_conn(conn: sqlite3.Connection, transcript_path: Path) -> bool:
    if not transcript_path.exists():
        return False
    try:
        st = transcript_path.stat()
    except OSError:
        return False

    path_key = str(transcript_path.resolve())
    row = conn.execute(
        "SELECT mtime_ns, size FROM indexed_files WHERE path = ?",
        (path_key,),
    ).fetchone()
    if row and row["mtime_ns"] == st.st_mtime_ns and row["size"] == st.st_size:
        return False

    session_id = _extract_session_id(transcript_path)
    records: List[Tuple[str, int, str, str, Optional[str], Optional[float]]] = []
    title: Optional[str] = None
    started_at: Optional[str] = None
    started_ts: Optional[float] = None
    ended_at: Optional[str] = None
    ended_ts: Optional[float] = None

    try:
        raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    for line in raw_lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        step_type = obj.get("type")
        if step_type == "USER_INPUT":
            role = "user"
            content = _clean_user_content(str(obj.get("content") or ""))
        elif step_type == "PLANNER_RESPONSE":
            role = "assistant"
            content = str(obj.get("content") or "").strip()
        else:
            continue

        if not content:
            continue

        created_at = str(obj.get("created_at") or "") or None
        created_ts = _parse_iso_to_ts(created_at)
        step_idx = int(obj.get("step_index") or 0)

        if started_ts is None and created_ts is not None:
            started_at, started_ts = created_at, created_ts
        if created_ts is not None:
            ended_at, ended_ts = created_at, created_ts
        if title is None and role == "user":
            one_line = " ".join(content.split())
            title = one_line[:117] + "..." if len(one_line) > 120 else one_line

        records.append((session_id, step_idx, role, content, created_at, created_ts))

    if started_ts is None:
        started_ts = st.st_mtime
        started_at = datetime.fromtimestamp(started_ts, tz=LOCAL_TZ).isoformat()
    if ended_ts is None:
        ended_ts = st.st_mtime
        ended_at = datetime.fromtimestamp(ended_ts, tz=LOCAL_TZ).isoformat()
    if title is None:
        title = f"Session {session_id[:8]}"

    with conn:
        conn.execute("DELETE FROM ag_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM ag_sessions WHERE id = ?", (session_id,))
        if records:
            conn.executemany(
                """
                INSERT INTO ag_messages (session_id, step_index, role, content, created_at, created_at_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            conn.execute(
                """
                INSERT INTO ag_sessions (id, title, started_at, started_at_ts, ended_at, ended_at_ts, message_count, transcript_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, title, started_at, started_ts, ended_at, ended_ts, len(records), path_key),
            )
        conn.execute(
            """
            INSERT INTO indexed_files (path, session_id, mtime_ns, size, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                session_id = excluded.session_id,
                mtime_ns = excluded.mtime_ns,
                size = excluded.size,
                updated_at = excluded.updated_at
            """,
            (path_key, session_id, st.st_mtime_ns, st.st_size, time.time()),
        )
    return True


def sync_single_transcript(transcript_path: Path) -> bool:
    """Incrementally sync one Antigravity transcript into sessions_fts.db."""
    conn = _open_fts_db()
    try:
        return _sync_one_with_conn(conn, transcript_path)
    finally:
        conn.close()


def sync_all_transcripts(max_seconds: float = 0.0) -> Dict[str, Any]:
    """Scan brain/*/.system_generated/logs/transcript.jsonl newest-first and sync changes."""
    brain_dir = get_brain_dir()
    if not brain_dir.exists():
        return {"total_files": 0, "scanned": 0, "updated": 0, "ag_sessions": 0, "ag_messages": 0}

    candidates: List[Tuple[int, Path]] = []
    for p in brain_dir.glob("*/.system_generated/logs/transcript.jsonl"):
        try:
            candidates.append((p.stat().st_mtime_ns, p))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)

    t0 = time.monotonic()
    scanned = 0
    updated = 0
    conn = _open_fts_db()
    try:
        for _, path in candidates:
            if max_seconds > 0 and (time.monotonic() - t0) >= max_seconds:
                break
            scanned += 1
            if _sync_one_with_conn(conn, path):
                updated += 1
        ag_sess = conn.execute("SELECT COUNT(*) FROM ag_sessions").fetchone()[0]
        ag_msgs = conn.execute("SELECT COUNT(*) FROM ag_messages").fetchone()[0]
    finally:
        conn.close()

    return {
        "total_files": len(candidates),
        "scanned": scanned,
        "updated": updated,
        "ag_sessions": ag_sess,
        "ag_messages": ag_msgs,
    }


def _build_fts_queries(raw_query: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+", raw_query or "")
    if not tokens:
        return []
    dedup = list(dict.fromkeys(tokens))
    quoted = [f'"{tok}"' for tok in dedup[:12]]
    queries = [" ".join(quoted)]
    if len(quoted) > 1:
        queries.append(" OR ".join(quoted))
    return queries


def _truncate_clean(text: str, limit: int = _READ_MAX_CONTENT) -> str:
    clean = redact_secrets(text or "")
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _make_snippet(content: str, query_tokens: List[str], max_len: int = 360) -> str:
    clean = redact_secrets(" ".join((content or "").split()))
    if len(clean) <= max_len:
        return clean
    low = clean.lower()
    pos = -1
    for tok in query_tokens:
        idx = low.find(tok.lower())
        if idx != -1:
            pos = idx
            break
    if pos == -1:
        return clean[: max_len - 3] + "..."
    start = max(0, pos - 120)
    end = min(len(clean), start + max_len)
    snippet = clean[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(clean):
        snippet = snippet + "..."
    return snippet


def _discover_antigravity(
    conn: sqlite3.Connection,
    fts_queries: List[str],
    raw_tokens: List[str],
    role_filter: Optional[str],
    after_ts: Optional[float],
    before_ts: Optional[float],
) -> List[Dict[str, Any]]:
    by_session: Dict[str, Dict[str, Any]] = {}
    for tier, fts_q in enumerate(fts_queries):
        sql = [
            """
            SELECT
                m.id AS msg_id,
                m.session_id,
                m.step_index,
                m.role,
                m.content,
                m.created_at_ts,
                s.title,
                s.started_at_ts,
                bm25(ag_messages_fts) AS rank
            FROM ag_messages_fts
            JOIN ag_messages m ON m.id = ag_messages_fts.rowid
            JOIN ag_sessions s ON s.id = m.session_id
            WHERE ag_messages_fts MATCH ?
            """
        ]
        params: List[Any] = [fts_q]
        if role_filter in ("user", "assistant"):
            sql.append("AND m.role = ?")
            params.append(role_filter)
        if after_ts is not None:
            sql.append("AND COALESCE(m.created_at_ts, s.started_at_ts) >= ?")
            params.append(after_ts)
        if before_ts is not None:
            sql.append("AND COALESCE(m.created_at_ts, s.started_at_ts) <= ?")
            params.append(before_ts)
        sql.append("ORDER BY rank LIMIT 150")

        try:
            rows = conn.execute("\n".join(sql), params).fetchall()
        except sqlite3.Error:
            continue

        for r in rows:
            sid = r["session_id"]
            if sid not in by_session:
                by_session[sid] = {
                    "origin": "antigravity",
                    "session_id": sid,
                    "title": redact_secrets(r["title"] or f"Session {sid[:8]}"),
                    "started_at": _format_ts(r["started_at_ts"]),
                    "started_at_ts": float(r["started_at_ts"] or 0.0),
                    "source": "antigravity",
                    "best_message_id": int(r["step_index"]),
                    "best_role": r["role"],
                    "snippet": _make_snippet(r["content"], raw_tokens),
                    "hit_count": 1,
                    "tier": tier,
                    "is_cron": False,
                    "bm25": float(r["rank"]),
                }
            else:
                by_session[sid]["hit_count"] += 1
        if by_session:
            break
    return list(by_session.values())


def _discover_hermes(
    conn: sqlite3.Connection,
    fts_queries: List[str],
    raw_tokens: List[str],
    role_filter: Optional[str],
    after_ts: Optional[float],
    before_ts: Optional[float],
) -> List[Dict[str, Any]]:
    by_session: Dict[str, Dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in _HIDDEN_HERMES_SOURCES)
    for tier, fts_q in enumerate(fts_queries):
        sql = [
            f"""
            SELECT
                m.id AS msg_id,
                m.session_id,
                m.role,
                m.content,
                m.timestamp,
                s.source,
                s.title,
                s.started_at,
                bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
              AND COALESCE(s.hidden, 0) = 0
              AND s.source NOT IN ({placeholders})
              AND COALESCE(m.active, 1) = 1
              AND COALESCE(m._compressed_summary, 0) = 0
            """
        ]
        params: List[Any] = [fts_q, *_HIDDEN_HERMES_SOURCES]
        if role_filter:
            sql.append("AND m.role = ?")
            params.append(role_filter)
        else:
            sql.append("AND m.role != 'tool'")
        if after_ts is not None:
            sql.append("AND COALESCE(m.timestamp, s.started_at) >= ?")
            params.append(after_ts)
        if before_ts is not None:
            sql.append("AND COALESCE(m.timestamp, s.started_at) <= ?")
            params.append(before_ts)
        sql.append("ORDER BY rank LIMIT 200")

        try:
            rows = conn.execute("\n".join(sql), params).fetchall()
        except sqlite3.Error:
            continue

        for r in rows:
            sid = r["session_id"]
            if sid not in by_session:
                src = r["source"] or "hermes"
                by_session[sid] = {
                    "origin": "hermes",
                    "session_id": sid,
                    "title": redact_secrets(r["title"] or f"Hermes {sid}"),
                    "started_at": _format_ts(r["started_at"]),
                    "started_at_ts": float(r["started_at"] or 0.0),
                    "source": src,
                    "best_message_id": int(r["msg_id"]),
                    "best_role": r["role"],
                    "snippet": _make_snippet(r["content"] or "", raw_tokens),
                    "hit_count": 1,
                    "tier": tier,
                    "is_cron": src == "cron",
                    "bm25": float(r["rank"]),
                }
            else:
                by_session[sid]["hit_count"] += 1
        if by_session:
            break
    return list(by_session.values())


def _read_or_scroll(
    session_id: str,
    around_message_id: Optional[int],
    window: int,
    offset: int,
    limit: int,
) -> Dict[str, Any]:
    ag_conn = _open_fts_db()
    try:
        sess = ag_conn.execute(
            "SELECT id, title, started_at_ts, message_count FROM ag_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if sess:
            if around_message_id is not None:
                rows = ag_conn.execute(
                    """
                    SELECT id, step_index, role, content, created_at_ts
                    FROM ag_messages
                    WHERE session_id = ? AND step_index BETWEEN ? AND ?
                    ORDER BY step_index ASC
                    """,
                    (session_id, around_message_id - window, around_message_id + window),
                ).fetchall()
                mode = "scroll"
            else:
                rows = ag_conn.execute(
                    """
                    SELECT id, step_index, role, content, created_at_ts
                    FROM ag_messages
                    WHERE session_id = ?
                    ORDER BY step_index ASC
                    LIMIT ? OFFSET ?
                    """,
                    (session_id, limit, offset),
                ).fetchall()
                mode = "read"
            return {
                "success": True,
                "mode": mode,
                "origin": "antigravity",
                "session_id": session_id,
                "title": redact_secrets(sess["title"] or ""),
                "started_at": _format_ts(sess["started_at_ts"]),
                "total_messages": int(sess["message_count"] or 0),
                "messages": [
                    {
                        "message_id": int(r["step_index"]),
                        "role": r["role"],
                        "timestamp": _format_ts(r["created_at_ts"]),
                        "content": _truncate_clean(r["content"]),
                    }
                    for r in rows
                ],
            }
    finally:
        ag_conn.close()

    h_conn = _open_hermes_ro()
    if h_conn is None:
        return {"success": False, "error": f"Session '{session_id}' not found."}
    try:
        sess = h_conn.execute(
            "SELECT id, source, title, started_at, message_count FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not sess:
            return {"success": False, "error": f"Session '{session_id}' not found."}

        all_rows = h_conn.execute(
            """
            SELECT id, role, content, timestamp
            FROM messages
            WHERE session_id = ?
              AND COALESCE(active, 1) = 1
              AND COALESCE(_compressed_summary, 0) = 0
              AND content IS NOT NULL AND length(trim(content)) > 0
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()

        if around_message_id is not None:
            mode = "scroll"
            anchor_idx = 0
            for idx, r in enumerate(all_rows):
                if int(r["id"]) >= around_message_id:
                    anchor_idx = idx
                    break
            lo = max(0, anchor_idx - window)
            hi = min(len(all_rows), anchor_idx + window + 1)
            sliced = all_rows[lo:hi]
        else:
            mode = "read"
            sliced = all_rows[offset : offset + limit]

        return {
            "success": True,
            "mode": mode,
            "origin": "hermes",
            "source": sess["source"],
            "session_id": session_id,
            "title": redact_secrets(sess["title"] or ""),
            "started_at": _format_ts(sess["started_at"]),
            "total_messages": len(all_rows),
            "messages": [
                {
                    "message_id": int(r["id"]),
                    "role": r["role"],
                    "timestamp": _format_ts(r["timestamp"]),
                    "content": _truncate_clean(r["content"]),
                }
                for r in sliced
            ],
        }
    finally:
        h_conn.close()


def _browse_sessions(
    origin: str,
    after_ts: Optional[float],
    before_ts: Optional[float],
    limit: int,
    *,
    exclude_session_id: str = "",
    include_cron: bool = True,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if origin in ("all", "antigravity"):
        ag_conn = _open_fts_db()
        try:
            sql = [
                "SELECT id, title, started_at_ts, ended_at_ts, message_count FROM ag_sessions WHERE message_count > 0"
            ]
            params: List[Any] = []
            if exclude_session_id:
                sql.append("AND id != ?")
                params.append(exclude_session_id)
            if after_ts is not None:
                sql.append("AND started_at_ts >= ?")
                params.append(after_ts)
            if before_ts is not None:
                sql.append("AND started_at_ts <= ?")
                params.append(before_ts)
            sql.append("ORDER BY COALESCE(ended_at_ts, started_at_ts) DESC LIMIT ?")
            params.append(limit)
            for r in ag_conn.execute("\n".join(sql), params).fetchall():
                ts = float(r["ended_at_ts"] or r["started_at_ts"] or 0.0)
                items.append(
                    {
                        "origin": "antigravity",
                        "source": "antigravity",
                        "session_id": r["id"],
                        "title": redact_secrets(r["title"] or f"Session {r['id'][:8]}"),
                        "started_at": _format_ts(ts),
                        "ts": ts,
                        "message_count": int(r["message_count"] or 0),
                    }
                )
        finally:
            ag_conn.close()

    if origin in ("all", "hermes"):
        h_conn = _open_hermes_ro()
        if h_conn is not None:
            try:
                placeholders = ",".join("?" for _ in _HIDDEN_HERMES_SOURCES)
                sql = [
                    f"""
                    SELECT id, source, title, started_at, last_activity_at, message_count
                    FROM sessions
                    WHERE COALESCE(hidden, 0) = 0
                      AND source NOT IN ({placeholders})
                    """
                ]
                params = list(_HIDDEN_HERMES_SOURCES)
                if not include_cron:
                    sql.append("AND source != 'cron'")
                if exclude_session_id:
                    sql.append("AND id != ?")
                    params.append(exclude_session_id)
                if after_ts is not None:
                    sql.append("AND COALESCE(last_activity_at, started_at) >= ?")
                    params.append(after_ts)
                if before_ts is not None:
                    sql.append("AND COALESCE(last_activity_at, started_at) <= ?")
                    params.append(before_ts)
                sql.append("ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?")
                params.append(limit)
                for r in h_conn.execute("\n".join(sql), params).fetchall():
                    ts = float(r["last_activity_at"] or r["started_at"] or 0.0)
                    items.append(
                        {
                            "origin": "hermes",
                            "source": r["source"],
                            "session_id": r["id"],
                            "title": redact_secrets(r["title"] or f"Hermes {r['id']}"),
                            "started_at": _format_ts(ts),
                            "ts": ts,
                            "message_count": int(r["message_count"] or 0),
                        }
                    )
            finally:
                h_conn.close()

    items.sort(key=lambda x: x["ts"], reverse=True)
    for it in items[:limit]:
        it.pop("ts", None)
    return items[:limit]


def session_search(
    query: Optional[str] = None,
    session_id: Optional[str] = None,
    around_message_id: Optional[int] = None,
    window: int = 5,
    origin: str = "all",
    role_filter: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    offset: int = 0,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Unified session search across Antigravity transcripts and optional Hermes state.db."""
    if origin not in ("all", "antigravity", "hermes"):
        return {"success": False, "error": "origin must be 'all', 'antigravity', or 'hermes'."}

    after_ts = _parse_time_bound(after, is_end=False)
    before_ts = _parse_time_bound(before, is_end=True)

    if session_id:
        win = max(1, min(15, int(window)))
        lim = max(1, min(40, int(limit or 20)))
        off = max(0, int(offset))
        return _read_or_scroll(session_id.strip(), around_message_id, win, off, lim)

    if not query or not query.strip():
        lim = max(1, min(25, int(limit or 10)))
        sessions = _browse_sessions(origin, after_ts, before_ts, lim)
        return {"success": True, "mode": "browse", "count": len(sessions), "sessions": sessions}

    raw_tokens = re.findall(r"[A-Za-z0-9_]+", query)
    fts_queries = _build_fts_queries(query)
    if not fts_queries:
        return {"success": False, "error": "Query contains no searchable terms."}

    lim = max(1, min(15, int(limit or 5)))
    results: List[Dict[str, Any]] = []

    if origin in ("all", "antigravity"):
        ag_conn = _open_fts_db()
        try:
            results.extend(_discover_antigravity(ag_conn, fts_queries, raw_tokens, role_filter, after_ts, before_ts))
        finally:
            ag_conn.close()

    if origin in ("all", "hermes"):
        h_conn = _open_hermes_ro()
        if h_conn is not None:
            try:
                results.extend(_discover_hermes(h_conn, fts_queries, raw_tokens, role_filter, after_ts, before_ts))
            finally:
                h_conn.close()

    results.sort(
        key=lambda r: (
            1 if r["is_cron"] else 0,
            r["tier"],
            -min(r["hit_count"], 10),
            r["bm25"],
            -r["started_at_ts"],
        )
    )
    top = results[:lim]
    for item in top:
        item.pop("tier", None)
        item.pop("is_cron", None)
        item.pop("bm25", None)
        item.pop("started_at_ts", None)

    return {
        "success": True,
        "mode": "discover",
        "query": query,
        "total_matched_sessions": len(results),
        "results": top,
    }


def get_recent_sessions_digest(exclude_session_id: str = "", limit: int = 5) -> str:
    """Compact digest of recent conversations for SessionStart injection."""
    items = _browse_sessions(
        "all",
        None,
        None,
        limit,
        exclude_session_id=exclude_session_id,
        include_cron=False,
    )
    if not items:
        return ""
    lines = ["RECENT SESSIONS (use hermes-core_hermes-core `session_search` to load full context):"]
    for it in items:
        title = (it["title"] or "").replace("\n", " ").strip()
        if len(title) > 75:
            title = title[:72] + "..."
        lines.append(f"- [{it['started_at']}] [{it['origin']}] {it['session_id']}: {title}")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--full" in sys.argv:
        stats = sync_all_transcripts(max_seconds=0.0)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print("Usage: python fts_indexer.py --full")
