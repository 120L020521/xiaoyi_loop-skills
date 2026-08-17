"""Recompute a profile's batch_summary.json with optional task exclusions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path, help="results/<profile> directory")
    parser.add_argument("--exclude", action="append", default=[], help="Task ID to exclude; repeatable")
    args = parser.parse_args()

    root = args.results_dir.expanduser().resolve()
    excluded = {
        value.removeprefix("task_").removeprefix("task")
        for value in args.exclude
    }
    previous_path = root / "batch_summary.json"
    previous = (
        json.loads(previous_path.read_text(encoding="utf-8"))
        if previous_path.is_file()
        else {}
    )
    rows: list[dict[str, object]] = []
    scores: list[float] = []
    for task_dir in sorted(path for path in root.glob("task*") if path.is_dir()):
        task_id = task_dir.name.removeprefix("task_").removeprefix("task")
        if task_id in excluded:
            continue
        result_path = task_dir / "judge_result.json"
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        score = float(result.get("score", 0.0))
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        row = {
            "taskId": task_id,
            "judgeProfile": result.get("judgeProfile"),
            "judgeModel": result.get("judgeModel"),
            "traceMode": result.get("traceMode"),
            "status": result.get("status", "error"),
            "score": score,
            "total": summary.get("total", 0),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
        }
        rows.append(row)
        scores.append(score)

    successful = sum(row["status"] == "success" for row in rows)
    report = {
        "version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preparedDir": previous.get("preparedDir"),
        "resultsDir": str(root),
        "profile": previous.get("profile") or root.name,
        "traceMode": previous.get("traceMode"),
        "excludedTaskIds": sorted(excluded),
        "summary": {
            "totalRuns": len(rows),
            "successful": successful,
            "failed": len(rows) - successful,
            "averageScore": sum(scores) / len(scores) if scores else 0.0,
            "perfectRuns": sum(score >= 1.0 for score in scores),
        },
        "results": rows,
    }
    previous_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
