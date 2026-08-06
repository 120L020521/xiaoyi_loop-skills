#!/usr/bin/env python3
"""Generate a Workspace-Bench judge summary workbook using pure Python."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


JUDGE_SIGNAL_FIELDS = ("passed", "score", "judgeModel")
REPORT_JUDGE_FIELDS = (
    "overallPassed",
    "score",
    "judgeModel",
    "judgeRubrics",
    "total",
    "passed",
    "failed",
)
ID_KEYS = ("absolute_id", "absoluteId", "task_id", "taskId", "case_id", "caseId", "id")
COMMON_JUDGE_CONTAINERS = ("result", "judge", "evaluation", "metrics", "summary")
EXCEL_CELL_TEXT_LIMIT = 32767


class ReportError(RuntimeError):
    """Raised for a user-facing report generation error."""


@dataclass
class JudgeEntry:
    file_path: Path
    relative_path: Path
    data: Any


@dataclass
class JudgeCandidate:
    entry: JudgeEntry
    record: Any
    model: Any
    rank: int


def natural_key(value: str) -> List[Tuple[int, Any]]:
    """Return a stable natural-sort key, so 2 sorts before 10."""
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    ]


def parse_task_ids(tokens: Sequence[str]) -> List[str]:
    ids: List[str] = []
    seen = set()

    def add(value: Any) -> None:
        task_id = str(value).strip()
        if task_id and task_id not in seen:
            ids.append(task_id)
            seen.add(task_id)

    for token in tokens:
        for part in (item.strip() for item in token.split(",")):
            if not part:
                continue
            match = re.fullmatch(r"(\d+)-(\d+)", part)
            if not match:
                add(part)
                continue
            start = int(match.group(1))
            end = int(match.group(2))
            step = 1 if start <= end else -1
            for task_id in range(start, end + step, step):
                add(task_id)

    return ids


def resolve_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def assert_directory(directory: Path, label: str) -> None:
    if not directory.exists():
        raise ReportError(f"{label} does not exist: {directory}")
    if not directory.is_dir():
        raise ReportError(f"{label} is not a directory: {directory}")


def read_json(file_path: Path) -> Any:
    try:
        with file_path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except OSError as error:
        raise ReportError(f"Cannot read JSON: {file_path}\n{error}") from error
    except json.JSONDecodeError as error:
        raise ReportError(f"Invalid JSON: {file_path}\n{error}") from error


def list_json_files(root: Path) -> List[Path]:
    results: List[Path] = []
    for current, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(
            (name for name in directory_names if not name.startswith(".")),
            key=natural_key,
        )
        for name in sorted(file_names, key=natural_key):
            if name.startswith(".") or not name.casefold().endswith(".json"):
                continue
            results.append(Path(current) / name)

    return sorted(
        results,
        key=lambda item: natural_key(str(item.relative_to(root))),
    )


def list_metadata_task_ids(tasks_root: Path) -> List[str]:
    task_ids: List[str] = []
    try:
        entries = list(tasks_root.iterdir())
    except OSError as error:
        raise ReportError(f"Cannot scan task root: {tasks_root}\n{error}") from error

    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if (entry / "metadata.json").is_file():
            task_ids.append(entry.name)

    return sorted(task_ids, key=natural_key)


def normalize_id(value: Any) -> str:
    normalized = str("" if value is None else value).strip().casefold()
    normalized = re.sub(r"^task[_-]?", "", normalized, count=1)
    return re.sub(r"^0+(?=\d)", "", normalized, count=1)


def id_matches(value: Any, task_id: str) -> bool:
    return normalize_id(value) == normalize_id(task_id)


def get_case_insensitive(obj: Any, names: Iterable[str]) -> Any:
    if not isinstance(obj, dict):
        return None
    wanted = {name.casefold() for name in names}
    for key, value in obj.items():
        if str(key).casefold() in wanted:
            return value
    return None


def object_has_task_id(obj: Any, task_id: str) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in ID_KEYS:
        value = get_case_insensitive(obj, (key,))
        if value is not None and id_matches(value, task_id):
            return True
    return False


def extract_judge_field(obj: Any, field: str) -> Any:
    if not isinstance(obj, dict):
        return None

    aliases = ("judgeModel", "judge_model") if field == "judgeModel" else (field,)
    direct = get_case_insensitive(obj, aliases)
    if direct is not None:
        return direct

    for container_name in COMMON_JUDGE_CONTAINERS:
        container = get_case_insensitive(obj, (container_name,))
        nested = get_case_insensitive(container, aliases)
        if nested is not None:
            return nested
    return None


def extract_overall_passed(obj: Any) -> Any:
    """Extract the overall boolean passed value without reading summary.passed."""
    if not isinstance(obj, dict):
        return None

    direct = get_case_insensitive(obj, ("passed",))
    if direct is not None:
        return direct
    for container_name in ("result", "judge", "evaluation", "metrics"):
        container = get_case_insensitive(obj, (container_name,))
        nested = get_case_insensitive(container, ("passed",))
        if nested is not None:
            return nested
    return None


def extract_judge_details(obj: Any) -> Tuple[Any, Any, Any, Any]:
    """Read judgeRubrics and summary counts directly from a judge result record."""
    if not isinstance(obj, dict):
        return None, None, None, None

    rubrics = get_case_insensitive(obj, ("rubrics",))
    summary = get_case_insensitive(obj, ("summary",))
    if not isinstance(summary, dict):
        return rubrics, None, None, None
    return (
        rubrics,
        get_case_insensitive(summary, ("total",)),
        get_case_insensitive(summary, ("passed",)),
        get_case_insensitive(summary, ("failed",)),
    )


def has_judge_signal(obj: Any) -> bool:
    return any(
        extract_judge_field(obj, field) is not None
        for field in JUDGE_SIGNAL_FIELDS
    )


def find_task_record(root: Any, task_id: str) -> Any:
    if not isinstance(root, (dict, list)):
        return None

    if isinstance(root, dict):
        for key, value in root.items():
            if id_matches(key, task_id) and isinstance(value, (dict, list)) and has_judge_signal(value):
                return value

    queue = deque([root])
    visited = set()
    while queue:
        current = queue.popleft()
        if not isinstance(current, (dict, list)):
            continue
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)

        if isinstance(current, dict) and object_has_task_id(current, task_id) and has_judge_signal(current):
            return current

        children = current if isinstance(current, list) else current.values()
        for child in children:
            if isinstance(child, (dict, list)):
                queue.append(child)

    return None


def path_signals_task(relative_path: Path, task_id: str) -> bool:
    normalized_task = normalize_id(task_id)
    raw_segments = [
        segment[:-5] if segment.casefold().endswith(".json") else segment
        for segment in relative_path.parts
    ]
    if any(normalize_id(segment) == normalized_task for segment in raw_segments):
        return True
    if not normalized_task.isdigit():
        return False

    escaped = re.escape(normalized_task)
    embedded_id = re.compile(
        rf"(?:^|[^0-9])(?:task[_-]?)?0*{escaped}(?:$|[^0-9])",
        re.IGNORECASE,
    )
    return any(embedded_id.search(segment) for segment in raw_segments)


def infer_judge_model(file_path: Path) -> Optional[str]:
    match = re.fullmatch(r"rubrics_judge--(.+)\.json", file_path.name, re.IGNORECASE)
    return match.group(1).strip() if match else None


def build_judge_index(judge_root: Path) -> List[JudgeEntry]:
    index: List[JudgeEntry] = []
    for file_path in list_json_files(judge_root):
        try:
            data = read_json(file_path)
        except ReportError as error:
            first_line = str(error).splitlines()[0]
            print(f"[WARN] Ignoring unreadable judge candidate: {first_line}", file=sys.stderr)
            continue
        index.append(
            JudgeEntry(
                file_path=file_path,
                relative_path=file_path.relative_to(judge_root),
                data=data,
            )
        )
    return index


def judge_candidate(entry: JudgeEntry, task_id: str) -> Optional[JudgeCandidate]:
    task_record = find_task_record(entry.data, task_id)
    path_match = path_signals_task(entry.relative_path, task_id)
    root_signal = has_judge_signal(entry.data)
    record = task_record if task_record is not None else (entry.data if path_match and root_signal else None)
    if record is None:
        return None

    rank = 0
    if task_record is not None:
        rank += 300
    if path_match:
        rank += 160
    if has_judge_signal(record):
        rank += 60
    if re.search(r"judge|result|score|eval", entry.file_path.name, re.IGNORECASE):
        rank += 20

    model = extract_judge_field(record, "judgeModel")
    if model is None:
        model = infer_judge_model(entry.file_path)
    return JudgeCandidate(entry=entry, record=record, model=model, rank=rank)


def select_judge_result(judge_index: Sequence[JudgeEntry], task_id: str) -> Optional[JudgeCandidate]:
    candidates = [
        candidate
        for entry in judge_index
        for candidate in [judge_candidate(entry, task_id)]
        if candidate is not None
    ]
    candidates.sort(
        key=lambda candidate: (
            -candidate.rank,
            natural_key(str(candidate.entry.file_path)),
        )
    )
    if not candidates:
        return None

    best_rank = candidates[0].rank
    best = [candidate for candidate in candidates if candidate.rank == best_rank]
    if len(best) > 1:
        paths = "\n".join(f"  - {candidate.entry.file_path}" for candidate in best)
        raise ReportError(
            f"Task {task_id} matches multiple judge JSON files with equal priority:\n"
            f"{paths}\n"
            "Place one result under a task-specific directory, or remove duplicate result files."
        )
    return candidates[0]


def discover_task_ids_from_judges(
    judge_index: Sequence[JudgeEntry],
    available_task_ids: Sequence[str],
) -> List[str]:
    by_normalized_id: Dict[str, str] = {}
    for task_id in available_task_ids:
        normalized = normalize_id(task_id)
        existing = by_normalized_id.get(normalized)
        if existing is not None and existing != task_id:
            raise ReportError(
                f"Task directories {existing} and {task_id} normalize to the same ID."
            )
        by_normalized_id[normalized] = task_id

    discovered = set()

    def add_known_id(value: Any) -> None:
        task_id = by_normalized_id.get(normalize_id(value))
        if task_id is not None:
            discovered.add(task_id)

    for entry in judge_index:
        if isinstance(entry.data, dict):
            for key, value in entry.data.items():
                if isinstance(value, (dict, list)) and has_judge_signal(value):
                    add_known_id(key)

        queue = deque([(entry.data, 0)])
        visited = set()
        while queue:
            current, depth = queue.popleft()
            if not isinstance(current, (dict, list)):
                continue
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)

            if isinstance(current, dict) and has_judge_signal(current):
                for key in ID_KEYS:
                    if key.casefold() == "id" and depth > 2:
                        continue
                    value = get_case_insensitive(current, (key,))
                    if value is not None:
                        add_known_id(value)

            children = current if isinstance(current, list) else current.values()
            for child in children:
                if isinstance(child, (dict, list)):
                    queue.append((child, depth + 1))

        if has_judge_signal(entry.data):
            for task_id in available_task_ids:
                if path_signals_task(entry.relative_path, task_id):
                    discovered.add(task_id)

    return [task_id for task_id in available_task_ids if task_id in discovered]


def excel_text_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def checked_text(value: str, label: str) -> str:
    length = excel_text_length(value)
    if length > EXCEL_CELL_TEXT_LIMIT:
        raise ReportError(
            f"{label} is {length} characters, exceeding Excel's "
            f"{EXCEL_CELL_TEXT_LIMIT}-character cell limit."
        )
    return value


def to_cell_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return checked_text(value, "A text field")
    if isinstance(value, (bool, int, float)):
        return value

    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ReportError(f"Cannot serialize a metadata field as JSON: {error}") from error
    return checked_text(text, "A JSON field")


def preferred_metadata_columns(metadata_list: Sequence[Mapping[str, Any]]) -> List[str]:
    keys: List[str] = []
    seen = set()

    def add(key: str) -> None:
        if key not in seen:
            keys.append(key)
            seen.add(key)

    for key in ("absolute_id", "persona"):
        if any(key in metadata for metadata in metadata_list):
            add(key)
    for metadata in metadata_list:
        for key in metadata:
            add(key)
    return keys


def build_report_metadata(
    english_metadata: Mapping[str, Any],
    chinese_metadata: Mapping[str, Any],
    english_path: Path,
    chinese_path: Path,
) -> Dict[str, Any]:
    if "task" not in english_metadata:
        raise ReportError(f"English metadata is missing the task field: {english_path}")
    if "task_cn" in english_metadata:
        raise ReportError(
            "English metadata already contains task_cn, so the generated column "
            f"would be ambiguous: {english_path}"
        )
    if not isinstance(chinese_metadata.get("task"), str):
        raise ReportError(
            f"Chinese metadata must contain a string task field: {chinese_path}"
        )

    report_metadata: Dict[str, Any] = {}
    for key, value in english_metadata.items():
        if key.casefold() == "language":
            continue
        report_metadata[key] = value
        if key == "task":
            report_metadata["task_cn"] = chinese_metadata["task"]
    return report_metadata


def set_literal_cell(worksheet: Any, row: int, column: int, value: Any) -> None:
    cell = worksheet.cell(row=row, column=column, value=value)
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"


def create_workbook(
    rows: Sequence[Mapping[str, Any]],
    metadata_columns: Sequence[str],
    output_path: Path,
) -> int:
    try:
        from openpyxl import Workbook
        from openpyxl.utils.exceptions import IllegalCharacterError
    except ModuleNotFoundError as error:
        raise ReportError(
            "The Python package openpyxl is required. Install it with: "
            "python -m pip install openpyxl"
        ) from error

    headers = [
        *metadata_columns,
        *REPORT_JUDGE_FIELDS,
        "日志链接",
    ]
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "WorkspaceBench Results"

    matrix = [headers]
    for row in rows:
        metadata = row["metadata"]
        matrix.append(
            [
                *(to_cell_value(metadata.get(key)) for key in metadata_columns),
                to_cell_value(row.get("overallPassed")),
                to_cell_value(row.get("score")),
                to_cell_value(row.get("judgeModel")),
                to_cell_value(row.get("judgeRubrics")),
                to_cell_value(row.get("total")),
                to_cell_value(row.get("passed")),
                to_cell_value(row.get("failed")),
                None,
            ]
        )

    try:
        for row_index, values in enumerate(matrix, start=1):
            for column_index, value in enumerate(values, start=1):
                set_literal_cell(worksheet, row_index, column_index, value)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(output_path)
    except IllegalCharacterError as error:
        raise ReportError(f"An Excel cell contains an illegal control character: {error}") from error
    except OSError as error:
        raise ReportError(f"Cannot write Excel file: {output_path}\n{error}") from error
    finally:
        workbook.close()

    return len(headers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a Workspace-Bench Excel report from bilingual metadata "
            "and judge JSON files."
        )
    )
    parser.add_argument(
        "--tasks-root",
        required=True,
        help="English root containing <task_id>/metadata.json",
    )
    parser.add_argument(
        "--tasks-cn-root",
        help="Chinese metadata root (default: sibling task_clean_cn)",
    )
    parser.add_argument(
        "--judge-root",
        required=True,
        help="Root containing judge result JSON files",
    )
    parser.add_argument(
        "--judge-model",
        "--judge_model",
        dest="judge_model",
        help="Fill judgeModel with this value for every task row",
    )
    parser.add_argument(
        "--out",
        help="Output .xlsx path (default: timestamped file in the current directory)",
    )
    parser.add_argument(
        "task_tokens",
        nargs="*",
        metavar="task_id",
        help=(
            "Optional task IDs separated by spaces or commas; numeric ranges are "
            "supported. Omit them to discover judged tasks automatically."
        ),
    )
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    task_ids = parse_task_ids(args.task_tokens)

    tasks_root = resolve_path(args.tasks_root)
    tasks_cn_root = resolve_path(
        args.tasks_cn_root
        if args.tasks_cn_root
        else str(tasks_root.parent / "task_clean_cn")
    )
    judge_root = resolve_path(args.judge_root)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = resolve_path(args.out or f"workspacebench_results_{stamp}.xlsx")
    if output_path.suffix.casefold() != ".xlsx":
        raise ReportError(f"--out must end in .xlsx: {output_path}")

    assert_directory(tasks_root, "Task root")
    assert_directory(tasks_cn_root, "Chinese task root")
    assert_directory(judge_root, "Judge root")

    print(f"[INFO] Scanning judge JSON files under: {judge_root}")
    judge_index = build_judge_index(judge_root)
    print(f"[INFO] Indexed {len(judge_index)} JSON file(s).")

    selected_judges: Dict[str, JudgeCandidate] = {}
    if not task_ids:
        available_task_ids = list_metadata_task_ids(tasks_root)
        noun = "directory" if len(available_task_ids) == 1 else "directories"
        print(
            f"[INFO] Found {len(available_task_ids)} task {noun} "
            "with metadata.json."
        )
        task_ids = discover_task_ids_from_judges(judge_index, available_task_ids)
        if not task_ids:
            raise ReportError("No task IDs with matching judge results were discovered.")
        for task_id in task_ids:
            judge = select_judge_result(judge_index, task_id)
            if judge is None:
                raise ReportError(
                    f"Task {task_id} was discovered but no judge result could be selected."
                )
            selected_judges[task_id] = judge
        print(
            f"[INFO] Auto-discovered {len(task_ids)} judged task ID(s): "
            f"{', '.join(task_ids)}"
        )
    else:
        print(
            f"[INFO] Using {len(task_ids)} task ID(s) supplied on the command line."
        )

    metadata_list: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for task_id in task_ids:
        english_path = tasks_root / task_id / "metadata.json"
        chinese_path = tasks_cn_root / task_id / "metadata.json"
        english_metadata = read_json(english_path)
        chinese_metadata = read_json(chinese_path)
        if not isinstance(english_metadata, dict):
            raise ReportError(
                f"metadata.json must contain a JSON object: {english_path}"
            )
        if not isinstance(chinese_metadata, dict):
            raise ReportError(
                f"metadata.json must contain a JSON object: {chinese_path}"
            )

        metadata = build_report_metadata(
            english_metadata,
            chinese_metadata,
            english_path,
            chinese_path,
        )
        metadata_list.append(metadata)

        judge = selected_judges.get(task_id)
        if judge is None:
            judge = select_judge_result(judge_index, task_id)
        if judge is None:
            print(
                f"[WARN] Task {task_id}: no judge JSON matched; "
                "judge result fields will be blank.",
                file=sys.stderr,
            )
            rows.append(
                {
                    "task_id": task_id,
                    "metadata": metadata,
                    "overallPassed": None,
                    "score": None,
                    "judgeModel": args.judge_model,
                    "judgeRubrics": None,
                    "total": None,
                    "passed": None,
                    "failed": None,
                }
            )
            continue

        overall_passed = extract_overall_passed(judge.record)
        score = extract_judge_field(judge.record, "score")
        judge_rubrics, total, passed, failed = extract_judge_details(judge.record)
        judge_model = args.judge_model
        if judge_model is None:
            judge_model = extract_judge_field(judge.record, "judgeModel")
        if judge_model is None:
            judge_model = judge.model
        print(f"[INFO] Task {task_id}: {judge.entry.file_path}")
        rows.append(
            {
                "task_id": task_id,
                "metadata": metadata,
                "overallPassed": overall_passed,
                "score": score,
                "judgeModel": judge_model,
                "judgeRubrics": judge_rubrics,
                "total": total,
                "passed": passed,
                "failed": failed,
            }
        )

    metadata_columns = preferred_metadata_columns(metadata_list)
    conflicting = [
        field for field in REPORT_JUDGE_FIELDS if field in metadata_columns
    ]
    if conflicting:
        raise ReportError(
            f"Metadata fields conflict with judge columns: {', '.join(conflicting)}"
        )

    column_count = create_workbook(rows, metadata_columns, output_path)
    print(f"[OK] Wrote {len(rows)} task row(s), {column_count} column(s).")
    print(f"[OK] Excel: {output_path}")
    return 0


def main() -> int:
    try:
        return run()
    except KeyboardInterrupt:
        return 130
    except ReportError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"[ERROR] Unexpected failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
