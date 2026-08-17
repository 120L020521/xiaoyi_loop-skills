"""Print a human-readable view of one judge_result.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="Path to judge_result.json")
    args = parser.parse_args()

    path = args.result.expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"result: {path}")
    print(f"taskId: {data.get('taskId')}")
    print(f"status: {data.get('status')}")
    print(f"score: {data.get('score')}")
    print(f"summary: {json.dumps(data.get('summary'), ensure_ascii=False)}")
    print("\n--- rubrics ---")
    for rubric in data.get("rubrics", []):
        if not isinstance(rubric, dict):
            continue
        print(
            f"\n[{rubric.get('index')}] passed={rubric.get('passed')} "
            f"conf={rubric.get('confidence')}"
        )
        print(f"  rubric: {str(rubric.get('rubric') or '')[:250]}")
        print(f"  evidence: {str(rubric.get('evidence') or '')[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

