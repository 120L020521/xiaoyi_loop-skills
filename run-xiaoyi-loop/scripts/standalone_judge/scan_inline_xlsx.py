"""Find prepared xlsx outputs that use inline strings without sharedStrings.xml."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prepared_dir", type=Path, help="Run prepared directory")
    parser.add_argument("--results-dir", type=Path, help="Optional results/<profile> directory")
    args = parser.parse_args()

    prepared_root = args.prepared_dir.expanduser().resolve()
    results_root = args.results_dir.expanduser().resolve() if args.results_dir else None
    task_dirs = sorted(
        path for path in prepared_root.glob("task*") if path.is_dir()
    )
    print(f"Total prepared tasks: {len(task_dirs)}\n")
    print(f"{'Task':<12} {'inlineStr':<12} {'noShared':<10} {'Score':<8} xlsx file")
    print("-" * 100)

    affected: list[tuple[str, str, str]] = []
    for task_dir in task_dirs:
        for xlsx_path in sorted((task_dir / "output").glob("*.xlsx")):
            try:
                with zipfile.ZipFile(xlsx_path) as archive:
                    names = archive.namelist()
                    has_shared = "xl/sharedStrings.xml" in names
                    has_inline = any(
                        "inlineStr" in archive.read(name).decode("utf-8", errors="ignore")
                        or "<is>" in archive.read(name).decode("utf-8", errors="ignore")
                        for name in names
                        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                    )
            except Exception as exc:
                print(f"{task_dir.name:<12} ERROR: {exc}")
                continue

            score = "?"
            if results_root is not None:
                result_path = results_root / task_dir.name / "judge_result.json"
                if result_path.is_file():
                    try:
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                        if result.get("score") is not None:
                            score = f"{float(result['score']):.3f}"
                        elif result.get("status") == "error":
                            score = "error"
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        pass
            print(
                f"{task_dir.name:<12} {str(has_inline):<12} "
                f"{str(not has_shared):<10} {score:<8} {xlsx_path.name}"
            )
            if has_inline and not has_shared:
                affected.append((task_dir.name, xlsx_path.name, score))

    print(f"\nAffected tasks: {len(affected)}")
    for task_id, filename, score in affected:
        print(f"  {task_id}: score={score}, file={filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
