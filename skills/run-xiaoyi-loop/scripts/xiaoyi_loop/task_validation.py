"""Preflight XiaoYi Task metadata before Runner or Judge preparation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from batch_runner import TaskSpec


class TaskPreflightError(ValueError):
    """Raised when one or more Tasks cannot safely enter the workflow."""


@dataclass(frozen=True)
class TaskPreflightResult:
    """Non-blocking findings for one validated Task."""

    task_id: int
    metadata_path: Path
    warnings: tuple[str, ...]


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise TaskPreflightError(f"无法读取 metadata.json：{path}：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise TaskPreflightError(
            f"metadata.json 不是有效的 UTF-8 文件：{path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TaskPreflightError(f"metadata.json 格式错误：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise TaskPreflightError(f"metadata.json 顶层必须是对象：{path}")
    return value


def _validate_rubrics(
    metadata: dict[str, object],
    *,
    metadata_path: Path,
) -> list[str]:
    rubrics = metadata.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        return [
            f"{metadata_path} 缺少非空 rubrics 数组；请补充评分标准后重试。"
        ]
    invalid = [
        str(index)
        for index, rubric in enumerate(rubrics)
        if not isinstance(rubric, str) or not rubric.strip()
    ]
    if invalid:
        return [
            f"{metadata_path} 的 rubrics[{', '.join(invalid)}] 不是非空字符串；"
            "请修正后重试。"
        ]
    return []


def _resolve_manifest_path(metadata_path: Path, stored_relpath: str) -> Path | None:
    task_dir = metadata_path.parent.resolve()
    candidate = (task_dir / stored_relpath).resolve()
    try:
        candidate.relative_to(task_dir)
    except ValueError:
        return None
    return candidate


def _validate_data(
    metadata: dict[str, object],
    *,
    metadata_path: Path,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    data_dir = metadata_path.parent / "data"
    manifest = metadata.get("data_manifest")
    if manifest in (None, []):
        if not data_dir.is_dir():
            warnings.append(
                f"{metadata_path.parent} 下没有 data/；metadata 未声明 data_manifest，"
                "如果该 Task 不依赖输入文件可以继续。"
            )
        elif not any(path.is_file() for path in data_dir.rglob("*")):
            warnings.append(
                f"{data_dir} 为空；如果 Task 或 rubrics 依赖输入文件，请先补齐。"
            )
        return errors, warnings

    if not isinstance(manifest, list):
        errors.append(
            f"{metadata_path} 的 data_manifest 必须是数组；请修正后重试。"
        )
        return errors, warnings

    missing: list[str] = []
    invalid_entries: list[str] = []
    unsafe_paths: list[str] = []
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            invalid_entries.append(str(index))
            continue
        raw_path = entry.get("stored_relpath")
        if not isinstance(raw_path, str) or not raw_path.strip():
            invalid_entries.append(str(index))
            continue
        resolved = _resolve_manifest_path(metadata_path, raw_path.strip())
        if resolved is None:
            unsafe_paths.append(raw_path.strip())
        elif not resolved.is_file():
            missing.append(raw_path.strip())

    if invalid_entries:
        errors.append(
            f"{metadata_path} 的 data_manifest[{', '.join(invalid_entries)}] "
            "缺少非空 stored_relpath。"
        )
    if unsafe_paths:
        errors.append(
            f"{metadata_path} 的 data_manifest 包含 Task 目录外路径："
            + "，".join(unsafe_paths)
        )
    if missing:
        errors.append(
            f"Task 输入文件不存在：{metadata_path.parent}："
            + "，".join(missing)
            + "。请补齐 data/ 后重试。"
        )
    return errors, warnings


def validate_task_specs(
    specs: Sequence[TaskSpec],
) -> tuple[TaskPreflightResult, ...]:
    """Validate Judge-critical metadata and declared source files for all Tasks."""
    errors: list[str] = []
    results: list[TaskPreflightResult] = []
    for spec in specs:
        try:
            metadata = _read_metadata(spec.metadata_path)
        except TaskPreflightError as exc:
            errors.append(f"task{spec.task_id}：{exc}")
            continue
        task_errors = _validate_rubrics(
            metadata,
            metadata_path=spec.metadata_path,
        )
        data_errors, warnings = _validate_data(
            metadata,
            metadata_path=spec.metadata_path,
        )
        task_errors.extend(data_errors)
        errors.extend(f"task{spec.task_id}：{message}" for message in task_errors)
        results.append(
            TaskPreflightResult(
                task_id=spec.task_id,
                metadata_path=spec.metadata_path,
                warnings=tuple(warnings),
            )
        )
    if errors:
        raise TaskPreflightError("\n".join(errors))
    return tuple(results)
