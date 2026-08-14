"""Print compact scores for selected tasks below a Judge result directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path, help="results/<profile> directory")
    parser.add_argument("task_ids", nargs="+", help="Task IDs to inspect")
    args = parser.parse_args()

    root = args.results_dir.expanduser().resolve()
    failed = 0
    for raw_task_id in args.task_ids:
        task_id = raw_task_id.removeprefix("task_").removeprefix("task")
        path = root / f"task{task_id}" / "judge_result.json"
        if not path.is_file():
            legacy_path = root / f"task_{task_id}" / "judge_result.json"
            if legacy_path.is_file():
                path = legacy_path
        if not path.is_file():
            print(f"task{task_id}: missing ({path})")
            failed += 1
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        rubrics = result.get("rubrics") or []
        passed = sum(
            1 for rubric in rubrics
            if isinstance(rubric, dict) and rubric.get("passed") is True
        )
        print(
            f"task{task_id}: status={result.get('status')}, "
            f"score={result.get('score')}, passed={passed}/{len(rubrics)}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
