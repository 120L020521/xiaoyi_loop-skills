#!/usr/bin/env python3
"""Deterministically Judge a FileOrganization final output tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT_NAMES = ("Desktop", "Download", "Documents")
DIRECT_CHILDREN_RE = re.compile(
    r"^(?P<path>.+?) 的直接子项是否恰好为 (?P<count>\d+) 个，"
    r"且完整名称集合为 (?P<names>.*?)？$"
)
DIRECTORY_RE = re.compile(
    r"^(?P<path>.+?) 是否存在且类型为目录，其直接子项是否恰好为 "
    r"(?P<count>\d+) 个，且完整名称集合为 (?P<names>.*?)？$"
)
FILES_MD5_PREFIX = "以下文件是否存在且类型为文件，且 MD5 分别正确："
FILE_MD5_RE = re.compile(r"^(?P<path>.+?)（(?P<md5>[0-9a-fA-F]{32})）$")


class JudgeInputError(ValueError):
    """Raised when evidence or a rubric cannot be evaluated safely."""


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JudgeInputError(f"cannot read valid JSON object from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JudgeInputError(f"expected JSON object: {path}")
    return value


def _logical_parts(logical_path: str) -> tuple[str, ...]:
    normalized = logical_path.strip().replace("\\", "/").strip("/")
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or parts[0] not in ROOT_NAMES:
        raise JudgeInputError(f"rubric path must start with one of {ROOT_NAMES}: {logical_path!r}")
    if any(part in {".", ".."} for part in parts):
        raise JudgeInputError(f"unsafe rubric path: {logical_path!r}")
    return parts


def _resolve_logical(outputs: Path, logical_path: str) -> Path:
    parts = _logical_parts(logical_path)
    candidate = outputs.joinpath(*parts)
    resolved_outputs = outputs.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_outputs)
    except ValueError as exc:
        raise JudgeInputError(f"rubric path escapes outputs: {logical_path!r}") from exc
    return candidate


def _parse_expected_names(text: str, expected_count: int) -> list[str]:
    stripped = text.strip()
    if expected_count == 0 and stripped in {"", "无", "空", "空集合"}:
        return []
    names = [name.strip() for name in stripped.split("、") if name.strip()]
    if len(names) != expected_count:
        raise JudgeInputError(
            f"rubric declares {expected_count} children but lists {len(names)}: {text!r}"
        )
    return names


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evaluate_children(
    outputs: Path,
    logical_path: str,
    expected_count: int,
    expected_names_text: str,
    *,
    require_directory_phrase: bool,
) -> tuple[bool, str]:
    expected_names = _parse_expected_names(expected_names_text, expected_count)
    target = _resolve_logical(outputs, logical_path)
    if not target.exists():
        return False, f"{logical_path}: missing directory; expected children={expected_names}"
    if not target.is_dir():
        return False, f"{logical_path}: expected directory but found non-directory"
    actual_names = sorted(entry.name for entry in target.iterdir())
    expected_sorted = sorted(expected_names)
    passed = actual_names == expected_sorted
    label = "directory and direct children" if require_directory_phrase else "direct children"
    return (
        passed,
        f"{logical_path}: {label}; expected={expected_sorted}, actual={actual_names}",
    )


def _evaluate_files(outputs: Path, rubric: str) -> tuple[bool, str]:
    body = rubric[len(FILES_MD5_PREFIX):]
    if not body.endswith("？"):
        raise JudgeInputError(f"unsupported MD5 rubric punctuation: {rubric}")
    body = body[:-1]
    specifications: list[tuple[str, str]] = []
    for chunk in body.split("、"):
        match = FILE_MD5_RE.fullmatch(chunk.strip())
        if match is None:
            raise JudgeInputError(f"unsupported file/MD5 specification: {chunk!r}")
        specifications.append((match.group("path"), match.group("md5").lower()))
    if not specifications:
        raise JudgeInputError(f"MD5 rubric contains no file specification: {rubric}")

    passed = True
    evidence: list[str] = []
    for logical_path, expected_md5 in specifications:
        target = _resolve_logical(outputs, logical_path)
        if not target.exists():
            passed = False
            evidence.append(f"{logical_path}: missing file; expected md5={expected_md5}")
            continue
        if not target.is_file():
            passed = False
            evidence.append(f"{logical_path}: expected file but found non-file")
            continue
        actual_md5 = _md5(target)
        if actual_md5 != expected_md5:
            passed = False
        evidence.append(
            f"{logical_path}: expected md5={expected_md5}, actual md5={actual_md5}"
        )
    return passed, "; ".join(evidence)


def evaluate_rubric(outputs: Path, rubric: str) -> tuple[bool, str]:
    directory_match = DIRECTORY_RE.fullmatch(rubric)
    if directory_match is not None:
        return _evaluate_children(
            outputs,
            directory_match.group("path"),
            int(directory_match.group("count")),
            directory_match.group("names"),
            require_directory_phrase=True,
        )

    direct_match = DIRECT_CHILDREN_RE.fullmatch(rubric)
    if direct_match is not None:
        return _evaluate_children(
            outputs,
            direct_match.group("path"),
            int(direct_match.group("count")),
            direct_match.group("names"),
            require_directory_phrase=False,
        )

    if rubric.startswith(FILES_MD5_PREFIX):
        return _evaluate_files(outputs, rubric)
    raise JudgeInputError(f"unsupported FileOrganization rubric: {rubric}")


def _fingerprint(metadata_path: Path, outputs: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    file_count = 0
    for root_name in ROOT_NAMES:
        root = outputs / root_name
        records.append({"path": root_name, "type": "dir"})
        for entry in sorted(root.rglob("*"), key=lambda item: item.relative_to(outputs).as_posix()):
            relative = entry.relative_to(outputs).as_posix()
            if entry.is_dir():
                records.append({"path": relative, "type": "dir"})
            elif entry.is_file():
                file_count += 1
                records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "size": entry.stat().st_size,
                        "sha256": _sha256(entry),
                    }
                )
            else:
                records.append({"path": relative, "type": "other"})
    payload = {
        "metadataSha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "entries": records,
    }
    value = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"algorithm": "sha256", "value": value, "fileCount": file_count}


def judge_file_organization(metadata_path: Path, outputs: Path) -> dict[str, Any]:
    metadata_path = metadata_path.expanduser().resolve()
    outputs = outputs.expanduser().resolve()
    if not metadata_path.is_file():
        raise JudgeInputError(f"metadata.json not found: {metadata_path}")
    if not outputs.is_dir():
        raise JudgeInputError(f"outputs directory not found: {outputs}")
    missing_roots = [name for name in ROOT_NAMES if not (outputs / name).is_dir()]
    if missing_roots:
        raise JudgeInputError(
            "incomplete outputs snapshot; missing roots: " + ", ".join(missing_roots)
        )
    outputs_manifest = outputs / "outputs_manifest.json"
    if outputs_manifest.is_file():
        manifest = _load_json_object(outputs_manifest)
        if manifest.get("snapshot_complete") is False:
            raise JudgeInputError("incomplete outputs snapshot: outputs_manifest.json reports failure")

    metadata = _load_json_object(metadata_path)
    case_id = metadata.get("absolute_id")
    if not isinstance(case_id, str) or re.fullmatch(
        r"FileOrganization_[0-9]+_[0-9]+", case_id
    ) is None:
        raise JudgeInputError(f"invalid FileOrganization absolute_id: {case_id!r}")
    rubrics = metadata.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics or not all(
        isinstance(item, str) and item.strip() for item in rubrics
    ):
        raise JudgeInputError("metadata.json must contain a non-empty string rubric list")

    rubric_results: list[dict[str, Any]] = []
    for index, rubric in enumerate(rubrics):
        passed, evidence = evaluate_rubric(outputs, rubric)
        rubric_results.append(
            {
                "index": index,
                "rubric": rubric,
                "passed": passed,
                "confidence": 1.0,
                "evidence": evidence,
            }
        )

    passed_count = sum(1 for item in rubric_results if item["passed"])
    total = len(rubric_results)
    all_passed = passed_count == total
    return {
        "version": 1,
        "datasetType": "file-organization",
        "taskId": case_id,
        "caseId": case_id,
        "status": "success",
        "judgeType": "deterministic-file-organization",
        "inputFingerprint": _fingerprint(metadata_path, outputs),
        "rubrics": rubric_results,
        "summary": {
            "total": total,
            "passed": passed_count,
            "failed": total - passed_count,
        },
        "passed": all_passed,
        "score": passed_count / total,
        "feedback": f"{passed_count}/{total} rubrics passed.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Judge one FileOrganization outputs tree against metadata rubrics."
    )
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _parser().parse_args()
    try:
        result = judge_file_organization(args.metadata, args.outputs)
    except JudgeInputError as exc:
        error_result = {
            "version": 1,
            "datasetType": "file-organization",
            "status": "error",
            "judgeType": "deterministic-file-organization",
            "error": str(exc),
        }
        _write_result(args.result, error_result)
        print(str(exc), file=sys.stderr)
        return 2
    _write_result(args.result, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
