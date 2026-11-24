#!/usr/bin/env python3
"""Isolated FTS5 recall and latency benchmark for hermes-core (never touches real user data)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "plugin" / "hermes-core" / "server"
sys.path.insert(0, str(SERVER_DIR))

import fts_indexer  # noqa: E402

SCALES = (100, 500, 1000, 2500, 5000, 10000)
MSGS_PER_SESSION = 20
FTS_RUNS = 20
SCAN_RUNS = 5


def _seed_transcripts(brain_dir: Path, total_msgs: int) -> Tuple[int, str]:
    sessions = max(5, total_msgs // MSGS_PER_SESSION)
    target_s = sessions // 2
    target_m = MSGS_PER_SESSION // 2
    target_query = f"deployment_token_marker_{target_s}_{target_m}"

    for s in range(sessions):
        log_dir = brain_dir / f"conv-{s:04d}" / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for m in range(MSGS_PER_SESSION):
            needle = (
                target_query
                if (s == target_s and m == target_m)
                else f"routine discussion topic {s} iteration {m} architecture notes memory cache"
            )
            lines.append(
                json.dumps(
                    {
                        "step_index": m,
                        "type": "USER_INPUT" if m % 2 == 0 else "PLANNER_RESPONSE",
                        "created_at": f"2026-11-24T10:{(m % 60):02d}:00Z",
                        "content": f"Notes about {needle} and SQLite FTS5 indexing pipeline across workspaces.",
                    }
                )
            )
        (log_dir / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return sessions, target_query


def run_scale_benchmark(total_msgs: int) -> Dict[str, object]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        brain = tmp / "brain"
        data = tmp / "data"
        os.environ["HERMES_CORE_BRAIN_DIR"] = str(brain)
        os.environ["HERMES_CORE_DATA_DIR"] = str(data)
        os.environ["HERMES_CORE_STATE_DB"] = str(tmp / "unused_state.db")

        sessions, target_query = _seed_transcripts(brain, total_msgs)
        fts_indexer.sync_all_transcripts()

        t0 = time.perf_counter()
        hits = 0
        for _ in range(FTS_RUNS):
            res = fts_indexer.session_search(query=target_query, limit=5)
            if res.get("results"):
                hits += 1
        fts_ms = ((time.perf_counter() - t0) * 1000.0) / FTS_RUNS

        t0 = time.perf_counter()
        for _ in range(SCAN_RUNS):
            found = 0
            for path in brain.rglob("transcript.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if target_query in line:
                        found += 1
            assert found >= 1
        scan_ms = ((time.perf_counter() - t0) * 1000.0) / SCAN_RUNS

        speedup = round(scan_ms / max(fts_ms, 0.01), 1)
        recall_pct = int(round((hits / FTS_RUNS) * 100))
        return {
            "messages": total_msgs,
            "sessions": sessions,
            "fts_ms": round(fts_ms, 2),
            "scan_ms": round(scan_ms, 2),
            "speedup": speedup,
            "recall_pct": recall_pct,
        }


def main() -> int:
    rows = [run_scale_benchmark(scale) for scale in SCALES]

    print("| Messages | Sessions (rooms) | FTS5 `session_search` | Raw JSONL scan | Speedup | Exact recall |")
    print("| :--- | :--- | :--- | :--- | :--- | :--- |")
    for row in rows:
        print(
            f"| {row['messages']:,} | {row['sessions']} | "
            f"{row['fts_ms']:.2f} ms | {row['scan_ms']:.2f} ms | "
            f"{row['speedup']}x | {row['recall_pct']}% |"
        )

    speedups = ", ".join(str(row["speedup"]) for row in rows)
    print(f"\nxychart-beta line array:\n    line [{speedups}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
