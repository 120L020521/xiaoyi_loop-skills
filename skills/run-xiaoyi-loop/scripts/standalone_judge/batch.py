"""Batch preparation and scoring for external Runner JSONL logs."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from standalone_judge import generate_judge_prompt as prompt_tools
from standalone_judge.config import JudgeProfile, apply_profile

Json = Any
logger = logging.getLogger(__name__)


def _task_dir_name(task_id: object) -> str:
    """Return the canonical directory name shared with XiaoYi logs."""
    return f"task{_safe_task_id(task_id)}"


def _task_id_from_dir_name(name: str) -> str | None:
    """Read task IDs from canonical ``task<ID>`` or legacy ``task_<ID>`` names."""
    match = re.fullmatch(r"task[_-]?(.+)", name, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return _safe_task_id(match.group(1))
    except ValueError:
        return None


def _prepared_input_fingerprint(task_dir: Path) -> dict[str, object]:
    """Hash every prepared artifact that can affect a Judge decision."""
    included: list[Path] = []
    for name in ("metadata.json", "agent.json", "normalized_runner_log.jsonl"):
        path = task_dir / name
        if path.is_file():
            included.append(path)
    for directory_name in ("data", "output"):
        directory = task_dir / directory_name
        if directory.is_dir():
            included.extend(path for path in directory.rglob("*") if path.is_file())
    included.sort(key=lambda path: path.relative_to(task_dir).as_posix().casefold())

    digest = hashlib.sha256()
    for path in included:
        relative = path.relative_to(task_dir).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return {
        "algorithm": "sha256",
        "value": digest.hexdigest(),
        "fileCount": len(included),
    }


def _prepared_task_dirs(prepared_root: Path) -> list[Path]:
    """Discover canonical and legacy prepared directories without duplicates."""
    available: dict[str, Path] = {}
    for path in prepared_root.iterdir():
        if not path.is_dir():
            continue
        task_id = _task_id_from_dir_name(path.name)
        if task_id is None:
            continue
        if not (path / "metadata.json").is_file() or not (path / "agent.json").is_file():
            continue
        previous = available.get(task_id)
        if previous is not None:
            raise ValueError(
                f"Duplicate prepared directories for task {task_id}: "
                f"{previous.name}, {path.name}"
            )
        available[task_id] = path
    return [
        available[task_id]
        for task_id in sorted(
            available,
            key=lambda value: (0, int(value)) if value.isdigit() else (1, value.casefold()),
        )
    ]


@dataclass(frozen=True)
class CaseSpec:
    """Resolved input paths for one external Runner task."""

    task_id: str
    log_path: Path
    metadata_path: Path
    output_paths: tuple[Path, ...]
    discovery_error: str | None = None


def _iso_now() -> str:
    """Return the current UTC time in a stable format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, value: object) -> None:
    """Write one UTF-8 JSON artifact.

    Args:
        path: Destination path.
        value: JSON-compatible value.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_task_id(value: object) -> str:
    """Validate a task identifier before using it in a directory name.

    Args:
        value: Manifest task ID.

    Returns:
        Validated string task ID.

    Raises:
        ValueError: If the identifier is empty or path-like.
    """
    task_id = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}")
    return task_id


def _resolve_case_path(value: object, *, base_dir: Path) -> Path:
    """Resolve a manifest path relative to its `cases.jsonl`.

    Args:
        value: String path from the manifest.
        base_dir: Directory containing the manifest.

    Returns:
        Absolute normalized path.

    Raises:
        ValueError: If the value is not a non-empty string.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected a non-empty path string, got: {value!r}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_cases(*, cases_path: Path, task_root: Path | None) -> list[CaseSpec]:
    """Load and validate a batch manifest.

    Args:
        cases_path: JSONL file with `task_id`, `log`, and optional output paths.
        task_root: Root containing `<task_id>/metadata.json`.

    Returns:
        Resolved case specifications.

    Raises:
        FileNotFoundError: If an input file is missing.
        ValueError: If the manifest is malformed or has duplicate tasks.
    """
    resolved_cases = cases_path.expanduser().resolve()
    if not resolved_cases.is_file():
        raise FileNotFoundError(f"Cases manifest not found: {resolved_cases}")
    base_dir = resolved_cases.parent
    resolved_task_root = (
        task_root.expanduser().resolve()
        if task_root is not None
        else None
    )
    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        resolved_cases.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{resolved_cases.name} line {line_number} is invalid JSON: "
                f"{exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(
                f"{resolved_cases.name} line {line_number} must be an object"
            )
        task_id = _safe_task_id(value.get("task_id"))
        if task_id in seen:
            raise ValueError(f"Duplicate task_id in cases manifest: {task_id}")
        seen.add(task_id)
        log_path = _resolve_case_path(value.get("log"), base_dir=base_dir)
        metadata_value = value.get("metadata")
        if metadata_value is not None:
            metadata_path = _resolve_case_path(
                metadata_value,
                base_dir=base_dir,
            )
        elif resolved_task_root is not None:
            metadata_path = (
                resolved_task_root / task_id / "metadata.json"
            ).resolve()
        else:
            raise ValueError(
                f"Task {task_id} needs a metadata path or --task-root"
            )
        raw_outputs = value.get("output")
        if raw_outputs is None:
            raw_outputs = value.get("outputs")
        if raw_outputs is None:
            output_values: list[object] = []
        elif isinstance(raw_outputs, list):
            output_values = list(raw_outputs)
        else:
            output_values = [raw_outputs]
        output_paths = tuple(
            _resolve_case_path(item, base_dir=base_dir)
            for item in output_values
        )
        cases.append(
            CaseSpec(
                task_id=task_id,
                log_path=log_path,
                metadata_path=metadata_path,
                output_paths=output_paths,
            )
        )
    if not cases:
        raise ValueError(f"No cases found in {resolved_cases}")
    return cases


def discover_cases(
    *,
    logs_dir: Path,
    task_root: Path,
) -> list[CaseSpec]:
    """Discover XiaoYi cases from ``task<ID>`` directories.

    Each task directory must contain one JSONL log. If several JSONL files are
    present, an exact ``task<ID>.jsonl``, ``task_<ID>.jsonl``, or
    ``<ID>.jsonl`` name is preferred. An ``outputs`` or ``output`` directory
    is optional. Metadata is resolved as ``<task_root>/<ID>/metadata.json``.

    Discovery problems are stored on the individual case so one malformed
    task does not prevent the remaining batch from being prepared.
    """
    resolved_logs = logs_dir.expanduser().resolve()
    resolved_tasks = task_root.expanduser().resolve()
    if not resolved_logs.is_dir():
        raise NotADirectoryError(f"Runner logs directory not found: {resolved_logs}")
    if not resolved_tasks.is_dir():
        raise NotADirectoryError(f"Task metadata root not found: {resolved_tasks}")

    discovered: list[tuple[int, str, Path]] = []
    for path in resolved_logs.iterdir():
        if not path.is_dir():
            continue
        match = re.fullmatch(r"task[_-]?(\d+)", path.name, flags=re.IGNORECASE)
        if match:
            task_id = match.group(1)
            discovered.append((int(task_id), task_id, path.resolve()))
    if not discovered:
        raise ValueError(
            f"No task<ID> directories found in Runner logs directory: "
            f"{resolved_logs}"
        )

    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for _, task_id, task_dir in sorted(
        discovered,
        key=lambda row: (row[0], row[2].name.casefold()),
    ):
        if task_id in seen:
            raise ValueError(
                f"Duplicate task directory for task ID {task_id} in "
                f"{resolved_logs}"
            )
        seen.add(task_id)

        logs = sorted(
            (
                path.resolve()
                for path in task_dir.iterdir()
                if path.is_file() and path.suffix.casefold() == ".jsonl"
            ),
            key=lambda path: path.name.casefold(),
        )
        error: str | None = None
        if len(logs) == 1:
            log_path = logs[0]
        elif not logs:
            log_path = task_dir / f"task{task_id}.jsonl"
            error = f"No JSONL log found directly under {task_dir}"
        else:
            preferred_names = {
                f"task{task_id}.jsonl",
                f"task_{task_id}.jsonl",
                f"{task_id}.jsonl",
            }
            preferred = [
                path
                for path in logs
                if path.name.casefold() in preferred_names
            ]
            if len(preferred) == 1:
                log_path = preferred[0]
            else:
                log_path = logs[0]
                error = (
                    f"Multiple JSONL logs found under {task_dir}; cannot "
                    f"choose automatically: "
                    + ", ".join(path.name for path in logs)
                )

        output_dirs = {
            path.name.casefold(): path.resolve()
            for path in task_dir.iterdir()
            if path.is_dir()
            and path.name.casefold() in {"output", "outputs"}
        }
        if "outputs" in output_dirs:
            output_paths = (output_dirs["outputs"],)
        elif "output" in output_dirs:
            output_paths = (output_dirs["output"],)
        else:
            output_paths = ()

        cases.append(
            CaseSpec(
                task_id=task_id,
                log_path=log_path,
                metadata_path=(
                    resolved_tasks / task_id / "metadata.json"
                ).resolve(),
                output_paths=output_paths,
                discovery_error=error,
            )
        )
    return cases


def _reset_output_dir(
    path: Path,
    *,
    overwrite: bool,
    preserve_files: Sequence[str] = (),
) -> None:
    """Create an empty case directory with explicit overwrite protection.

    Args:
        path: Case directory.
        overwrite: Allow replacing a previous generated case.
        preserve_files: Direct child files to restore after replacing the directory.

    Raises:
        FileExistsError: If the directory is non-empty without `overwrite`.
        ValueError: If a destructive target fails its parent containment check.
    """
    resolved = path.resolve()
    parent = path.parent.resolve()
    if resolved.parent != parent:
        raise ValueError(f"Unsafe generated case path: {resolved}")
    preserved: dict[str, bytes] = {}
    for name in preserve_files:
        relative = Path(name)
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name != name:
            raise ValueError(f"Unsafe preserved filename: {name}")
        candidate = resolved / name
        if candidate.is_file():
            preserved[name] = candidate.read_bytes()
    if resolved.exists() and any(resolved.iterdir()):
        if not overwrite:
            raise FileExistsError(
                f"Prepared case is not empty: {resolved}; pass --overwrite"
            )
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    for name, content in preserved.items():
        (resolved / name).write_bytes(content)


def _write_jsonl(path: Path, events: Sequence[dict[str, object]]) -> None:
    """Write normalized or sanitized trace events.

    Args:
        path: Destination JSONL path.
        events: Trace events.
    """
    path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


def _events_jsonl_size(events: Sequence[dict[str, object]]) -> int:
    """Return the UTF-8 JSONL size of trace events without writing a file."""
    return sum(
        len(
            (
                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        )
        for event in events
    )


def _compact_trace_for_judge(
    events: Sequence[dict[str, object]],
    *,
    source_format: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Remove XiaoYi structural duplication from the Judge-facing trace.

    XiaoYi emits one cumulative ``model_input`` before every model response.
    Those records repeat the full conversation, system prompt, and tool
    definitions.  Every normalized event also embeds its source record under
    ``rawEvent`` even though the canonical fields already contain the evidence
    needed by the Judge.

    The complete normalized trace remains as a separate audit artifact. This
    function does not truncate unique model outputs, tool calls, or results.
    """
    before = [dict(event) for event in events]
    before_bytes = _events_jsonl_size(before)
    is_xiaoyi = source_format == "event-stream"
    if not is_xiaoyi:
        report: dict[str, object] = {
            "enabled": False,
            "policy": "none",
            "reason": (
                "Structural compaction currently applies only to XiaoYi "
                "event streams."
            ),
            "sourceEvents": len(before),
            "judgeEvents": len(before),
            "omittedCumulativeModelInputEvents": 0,
            "removedRawEventCopies": 0,
            "uniquePayloadTruncation": False,
            "bytesBefore": before_bytes,
            "bytesAfter": before_bytes,
            "reductionRatio": 0.0,
        }
        return before, report

    compacted: list[dict[str, object]] = []
    omitted_model_inputs = 0
    removed_raw_events = 0
    for event in before:
        item = dict(event)
        if "rawEvent" in item:
            item.pop("rawEvent")
            removed_raw_events += 1
        event_type = str(item.get("eventType") or "").strip().casefold()
        if event_type == "model_input":
            omitted_model_inputs += 1
            continue
        compacted.append(item)

    # Keep a small provenance marker if an unusual trace contains only
    # cumulative model inputs.  This avoids presenting an empty execution.
    if before and not compacted:
        compacted.append(
            {
                "schemaVersion": 1,
                "schema": prompt_tools._NORMALIZED_LOG_SCHEMA,
                "sequence": 1,
                "eventType": "trace_compaction",
                "sourceFormat": source_format,
                "content": {
                    "note": (
                        "The source trace contained only cumulative model_input "
                        "events. Full audit logs remain attached locally."
                    ),
                    "omittedModelInputEvents": omitted_model_inputs,
                },
            }
        )

    after_bytes = _events_jsonl_size(compacted)
    event_counts = Counter(
        str(event.get("eventType") or "unknown")
        for event in compacted
    )
    report = {
        "enabled": True,
        "policy": "xiaoyi-structural-dedup-v1",
        "sourceEvents": len(before),
        "judgeEvents": len(compacted),
        "omittedCumulativeModelInputEvents": omitted_model_inputs,
        "removedRawEventCopies": removed_raw_events,
        "uniquePayloadTruncation": False,
        "preservedEventTypes": dict(sorted(event_counts.items())),
        "bytesBefore": before_bytes,
        "bytesAfter": after_bytes,
        "reductionRatio": (
            round(1.0 - (after_bytes / before_bytes), 6)
            if before_bytes
            else 0.0
        ),
        "auditArtifacts": [
            "normalized_runner_log.jsonl",
        ],
    }
    return compacted, report


def _copy_outputs(
    *,
    output_paths: Sequence[Path],
    metadata: dict[str, object],
    task_dir: Path,
) -> tuple[list[Path], list[dict[str, object]], list[str]]:
    """Collect and copy declared Runner outputs into a Judge task directory.

    Args:
        output_paths: Files or work/output directories supplied by the user.
        metadata: Task metadata with expected output names.
        task_dir: Generated Judge task directory.

    Returns:
        Copied paths, copy manifest entries, and warnings.
    """
    files, warnings = prompt_tools._expand_output_paths(
        output_paths,
        metadata=metadata,
    )
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    entries: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for source in files:
        name_key = source.name.casefold()
        if name_key in seen_names:
            raise ValueError(
                f"Multiple Runner outputs have the same filename: {source.name}"
            )
        seen_names.add(name_key)
        destination = output_dir / source.name
        shutil.copy2(source, destination)
        copied.append(destination)
        entries.append(
            {
                "source": str(source),
                "preparedPath": str(destination.relative_to(task_dir)),
                "sizeBytes": destination.stat().st_size,
            }
        )
    if not copied:
        warnings.append(
            "No Runner output files were supplied or matched metadata. "
            "Result-based rubrics may fail because evidence is missing."
        )
    return copied, entries, warnings


def prepare_case(
    *,
    case: CaseSpec,
    prepared_dir: Path,
    log_format: str,
    overwrite: bool,
    preserve_files: Sequence[str] = (),
) -> dict[str, object]:
    """Convert one external Runner case into Better Harness Judge inputs.

    Args:
        case: Resolved external Runner inputs.
        prepared_dir: Batch preparation root.
        log_format: Requested source-log adapter.
        overwrite: Replace an existing generated case.
        preserve_files: Direct child files to keep while refreshing prepared inputs.

    Returns:
        Per-case preparation manifest.
    """
    if case.discovery_error:
        raise ValueError(case.discovery_error)
    task_dir = prepared_dir / _task_dir_name(case.task_id)
    _reset_output_dir(
        task_dir,
        overwrite=overwrite,
        preserve_files=preserve_files,
    )
    metadata = prompt_tools._load_json_object(case.metadata_path)
    rubrics = metadata.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        raise ValueError(
            f"Task {case.task_id} metadata does not contain non-empty rubrics"
        )
    raw_events, warnings = prompt_tools._load_jsonl(case.log_path)
    normalized, detected_format, normalization_warnings = (
        prompt_tools._normalize_events(
            raw_events,
            requested_format=log_format,
        )
    )
    warnings.extend(normalization_warnings)
    sanitized_normalized = [
        prompt_tools._sanitize_event(event)
        for event in normalized
    ]
    applied_format = (
        detected_format
        if log_format == "auto"
        else "event-stream"
        if log_format == "xiaoyi"
        else log_format
    )
    judge_trace, compaction_report = _compact_trace_for_judge(
        sanitized_normalized,
        source_format=applied_format,
    )

    shutil.copy2(case.metadata_path, task_dir / "metadata.json")
    source_data_dir = case.metadata_path.parent / "data"
    if source_data_dir.is_dir():
        dest_data_dir = task_dir / "data"
        if dest_data_dir.exists():
            shutil.rmtree(dest_data_dir)
        shutil.copytree(source_data_dir, dest_data_dir)
    _write_jsonl(
        task_dir / "normalized_runner_log.jsonl",
        sanitized_normalized,
    )
    copied_outputs, output_entries, output_warnings = _copy_outputs(
        output_paths=case.output_paths,
        metadata=metadata,
        task_dir=task_dir,
    )
    warnings.extend(output_warnings)
    output_manifest = [
        {
            # Judge inputs must not depend on the workstation's absolute
            # source path. The audit manifest below still keeps that path.
            "sourcePath": entry["preparedPath"],
            "outputPath": entry["preparedPath"],
            "sizeBytes": entry["sizeBytes"],
        }
        for entry in output_entries
    ]
    agent_json = {
        "trace": {
            "prompt": {"user": str(metadata.get("task") or "")},
            "executionTrace": judge_trace,
            "audit": {
                "compactTraceEmbedded": True,
                "fullNormalizedTrace": "normalized_runner_log.jsonl",
                "compactionPolicy": compaction_report["policy"],
            },
            "outputs": {
                "returnedPaths": [
                    str(path.relative_to(task_dir))
                    for path in copied_outputs
                ],
                "outputManifest": output_manifest,
            },
        }
    }
    _write_json(task_dir / "agent.json", agent_json)
    manifest: dict[str, object] = {
        "version": 1,
        "createdAt": _iso_now(),
        "status": "prepared",
        "taskId": case.task_id,
        "taskDir": str(task_dir.resolve()),
        "inputs": {
            "log": str(case.log_path),
            "metadata": str(case.metadata_path),
            "outputs": [str(path) for path in case.output_paths],
        },
        "logNormalization": {
            "schema": prompt_tools._NORMALIZED_LOG_SCHEMA,
            "requestedFormat": log_format,
            "detectedFormat": detected_format,
            "appliedFormat": applied_format,
            "sourceEvents": len(raw_events),
            "normalizedEvents": len(sanitized_normalized),
        },
        "logCompaction": compaction_report,
        "outputFiles": output_entries,
        "rubricCount": len(rubrics),
        "warnings": warnings,
    }
    manifest["inputFingerprint"] = _prepared_input_fingerprint(task_dir)
    _write_json(task_dir / "case_manifest.json", manifest)
    return manifest


def prepare_batch(
    *,
    prepared_dir: Path,
    cases_path: Path | None = None,
    logs_dir: Path | None = None,
    task_root: Path | None = None,
    log_format: str = "auto",
    overwrite: bool = False,
    task_ids: Sequence[str] | None = None,
    metadata_paths: Sequence[Path] | None = None,
    preserve_task_files: Sequence[str] = (),
) -> dict[str, object]:
    """Prepare all external Runner cases for the native Judge.

    Args:
        cases_path: Optional batch JSONL manifest.
        logs_dir: Optional root containing ``task<ID>`` XiaoYi directories.
        task_root: Workspace-Bench task metadata root.
        prepared_dir: Generated task-directory root.
        log_format: Source log adapter.
        overwrite: Replace previous generated task directories.
        task_ids: Optional task IDs to prepare instead of every discovered case.
        metadata_paths: Explicit metadata files for Task directories that are
            not arranged as ``<task_root>/<ID>/metadata.json``.
        preserve_task_files: Direct child files to keep in each refreshed task directory.

    Returns:
        Batch preparation report.
    """
    if (cases_path is None) == (logs_dir is None):
        raise ValueError("Provide exactly one of cases_path or logs_dir")
    if logs_dir is not None:
        if task_root is None and not metadata_paths:
            raise ValueError("logs_dir discovery requires task_root")
        cases = discover_cases(
            logs_dir=logs_dir,
            task_root=task_root or logs_dir,
        )
        if metadata_paths:
            metadata_by_id: dict[str, Path] = {}
            for raw_path in metadata_paths:
                metadata_path = raw_path.expanduser().resolve()
                metadata = prompt_tools._load_json_object(metadata_path)
                raw_id = metadata.get("absolute_id")
                if isinstance(raw_id, bool):
                    raise ValueError(
                        f"metadata absolute_id must be an integer: {metadata_path}"
                    )
                if isinstance(raw_id, int):
                    task_id = _safe_task_id(raw_id)
                elif isinstance(raw_id, str) and raw_id.isdigit():
                    task_id = _safe_task_id(raw_id)
                elif metadata_path.parent.name.isdigit():
                    task_id = _safe_task_id(metadata_path.parent.name)
                else:
                    raise ValueError(
                        "Cannot determine Task ID from absolute_id or directory: "
                        f"{metadata_path}"
                    )
                previous = metadata_by_id.get(task_id)
                if previous is not None and previous != metadata_path:
                    raise ValueError(
                        f"Duplicate metadata for Task {task_id}: "
                        f"{previous}, {metadata_path}"
                    )
                metadata_by_id[task_id] = metadata_path
            logs_by_id = {case.task_id: case for case in cases}
            missing_logs = sorted(metadata_by_id.keys() - logs_by_id.keys())
            if missing_logs:
                raise ValueError(
                    "Log cases not found for metadata tasks: "
                    + ", ".join(missing_logs)
                )
            cases = [
                replace(
                    logs_by_id[task_id],
                    metadata_path=metadata_by_id[task_id],
                )
                for task_id in sorted(
                    metadata_by_id,
                    key=lambda value: (
                        (0, int(value))
                        if value.isdigit()
                        else (1, value.casefold())
                    ),
                )
            ]
    else:
        assert cases_path is not None
        cases = load_cases(cases_path=cases_path, task_root=task_root)
    requested_task_ids: set[str] | None = None
    if task_ids:
        requested_task_ids = {
            _task_id_from_dir_name(str(task_id)) or _safe_task_id(task_id)
            for task_id in task_ids
        }
        available = {case.task_id: case for case in cases}
        missing = sorted(requested_task_ids - available.keys())
        if missing:
            raise ValueError(
                "Log cases not found for requested tasks: " + ", ".join(missing)
            )
        cases = [
            available[task_id]
            for task_id in sorted(
                requested_task_ids,
                key=lambda value: (
                    (0, int(value))
                    if value.isdigit()
                    else (1, value.casefold())
                ),
            )
        ]
    prepared_root = prepared_dir.expanduser().resolve()
    prepared_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for case in cases:
        try:
            rows.append(
                prepare_case(
                    case=case,
                    prepared_dir=prepared_root,
                    log_format=log_format,
                    overwrite=overwrite,
                    preserve_files=preserve_task_files,
                )
            )
        except Exception as exc:
            task_dir = prepared_root / _task_dir_name(case.task_id)
            error_row = {
                "version": 1,
                "createdAt": _iso_now(),
                "status": "error",
                "taskId": case.task_id,
                "taskDir": str(task_dir),
                "error": f"{type(exc).__name__}: {exc}",
            }
            rows.append(error_row)
            _write_json(
                prepared_root / "_errors" / f"{_task_dir_name(case.task_id)}.json",
                error_row,
            )
    successful = sum(row.get("status") == "prepared" for row in rows)
    report: dict[str, object] = {
        "version": 1,
        "createdAt": _iso_now(),
        "casesFile": (
            str(cases_path.expanduser().resolve())
            if cases_path is not None
            else None
        ),
        "logsDir": (
            str(logs_dir.expanduser().resolve())
            if logs_dir is not None
            else None
        ),
        "taskRoot": (
            str(task_root.expanduser().resolve())
            if task_root is not None
            else None
        ),
        "requestedTaskIds": (
            sorted(requested_task_ids) if requested_task_ids is not None else None
        ),
        "preparedDir": str(prepared_root),
        "summary": {
            "total": len(rows),
            "prepared": successful,
            "failed": len(rows) - successful,
        },
        "cases": rows,
    }
    _write_json(prepared_root / "prepare_report.json", report)
    return report


def _normalize_judge_result(
    *,
    metadata: dict[str, object],
    result: dict[str, Json],
) -> dict[str, Json]:
    """Ensure every metadata rubric has exactly one conservative result.

    Args:
        metadata: Original task metadata.
        result: Result returned by the copied Judge core.

    Returns:
        Validated result with a stable score.
    """
    expected = metadata.get("rubrics")
    rubrics = expected if isinstance(expected, list) else []
    raw_rows = result.get("rubrics")
    rows = raw_rows if isinstance(raw_rows, list) else []
    by_index: dict[int, dict[str, Json]] = {}
    warnings: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            warnings.append("Ignored a non-object rubric result.")
            continue
        index = row.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(rubrics):
            warnings.append(f"Ignored out-of-range rubric index: {index!r}")
            continue
        if index in by_index:
            warnings.append(f"Duplicate rubric index {index}; marked as failed.")
            by_index[index] = {
                "index": index,
                "rubric": rubrics[index],
                "passed": False,
                "confidence": 0.0,
                "evidence": "Judge returned duplicate results for this rubric.",
            }
            continue
        confidence = row.get("confidence")
        normalized_confidence = (
            max(0.0, min(1.0, float(confidence)))
            if isinstance(confidence, (int, float))
            else None
        )
        by_index[index] = {
            "index": index,
            "rubric": rubrics[index],
            "passed": row.get("passed") is True,
            "confidence": normalized_confidence,
            "evidence": str(row.get("evidence") or ""),
        }
    normalized_rows: list[dict[str, Json]] = []
    for index, rubric in enumerate(rubrics):
        if index not in by_index:
            warnings.append(f"Missing rubric index {index}; marked as failed.")
            by_index[index] = {
                "index": index,
                "rubric": rubric,
                "passed": False,
                "confidence": 0.0,
                "evidence": "Judge did not return a result for this rubric.",
            }
        normalized_rows.append(by_index[index])
    passed = sum(row.get("passed") is True for row in normalized_rows)
    total = len(normalized_rows)
    score = passed / total if total else 0.0
    normalized = dict(result)
    normalized["rubrics"] = normalized_rows
    normalized["summary"] = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
    }
    normalized["score"] = score
    normalized["passed"] = score >= 1.0
    normalized["feedback"] = f"Score: {score:.4f}. {passed}/{total} rubrics passed."
    normalized["validationWarnings"] = warnings
    return normalized


def judge_case(
    *,
    task_dir: Path,
    results_dir: Path,
    profile: JudgeProfile,
    trace_mode: str,
    resume: bool,
    overwrite: bool,
    delay_before_request_s: float = 0.0,
) -> dict[str, Json]:
    """Judge one prepared case and persist normalized artifacts.

    Args:
        task_dir: Prepared task directory.
        results_dir: Batch result root.
        profile: Judge model configuration.
        trace_mode: Judge-facing trace selection (``compact`` or ``full``).
        resume: Reuse an existing successful result only when its Judge
            settings and prepared-input fingerprint still match.
        overwrite: Replace an existing profile result.
        delay_before_request_s: Provider cooldown applied only when an API
            request is actually needed.

    Returns:
        Normalized task result.
    """
    task_manifest = prompt_tools._load_json_object(
        task_dir / "case_manifest.json"
    )
    task_id = str(
        task_manifest.get("taskId")
        or _task_id_from_dir_name(task_dir.name)
        or task_dir.name
    )
    result_dir = results_dir / _task_dir_name(task_id)
    result_path = result_dir / "judge_result.json"
    input_fingerprint = _prepared_input_fingerprint(task_dir)
    if task_manifest.get("inputFingerprint") != input_fingerprint:
        task_manifest["inputFingerprint"] = input_fingerprint
        _write_json(task_dir / "case_manifest.json", task_manifest)
    if resume and result_path.is_file():
        existing = prompt_tools._load_json_object(result_path)
        if (
            existing.get("status") == "success"
            and existing.get("traceMode") == trace_mode
            and existing.get("judgeProfile") == profile.name
            and existing.get("judgeModel") == profile.model
            and existing.get("judgeBaseUrl") == profile.base_url
            and existing.get("inputFingerprint") == input_fingerprint
        ):
            resumed = dict(existing)
            resumed["_resumed"] = True
            return resumed
    if delay_before_request_s:
        logger.info(
            "Cooling down %.1fs before the next %s request",
            delay_before_request_s,
            profile.name,
        )
        time.sleep(delay_before_request_s)
    _reset_output_dir(result_dir, overwrite=overwrite or resume)

    apply_profile(profile)
    from standalone_judge.judge_core.judge_agent import run_judge

    metadata = prompt_tools._load_json_object(task_dir / "metadata.json")
    result = run_judge(
        task_dir=task_dir,
        metadata=metadata,
        api_key=profile.api_key,
        base_url=profile.base_url,
        model=profile.model,
        trace_mode=trace_mode,
    )
    normalized = _normalize_judge_result(
        metadata=metadata,
        result=result,
    )
    judge_metadata = normalized.get("judge")
    judge_error = (
        judge_metadata.get("error")
        if isinstance(judge_metadata, dict)
        else None
    )
    normalized["status"] = (
        "error"
        if normalized.get("_judge_error") or judge_error
        else "success"
    )
    normalized["taskId"] = task_id
    normalized["judgeProfile"] = profile.name
    normalized["judgeModel"] = profile.model
    normalized["judgeBaseUrl"] = profile.base_url
    normalized["traceMode"] = trace_mode
    normalized["inputFingerprint"] = input_fingerprint
    normalized["createdAt"] = _iso_now()
    _write_json(result_path, normalized)
    return normalized


def judge_batch(
    *,
    prepared_dir: Path,
    results_dir: Path,
    profile: JudgeProfile,
    trace_mode: str = "compact",
    resume: bool = False,
    overwrite: bool = False,
    task_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    """Run one Judge profile over all prepared cases.

    Args:
        prepared_dir: Root produced by `prepare_batch`.
        results_dir: Final artifact root.
        profile: Judge model configuration.
        trace_mode: Judge-facing trace selection (``compact`` or ``full``).
        resume: Skip successful profile/task results only when their Judge
            settings and prepared-input fingerprints still match.
        overwrite: Replace existing result directories.
        task_ids: Optional task IDs to evaluate instead of every prepared task.

    Returns:
        Batch scoring report.
    """
    if resume and overwrite:
        raise ValueError("--resume and --overwrite cannot be used together")
    if trace_mode not in {"compact", "full"}:
        raise ValueError(f"Unsupported trace mode: {trace_mode!r}")
    prepared_root = prepared_dir.expanduser().resolve()
    result_root = results_dir.expanduser().resolve()
    result_root.mkdir(parents=True, exist_ok=True)
    task_dirs = _prepared_task_dirs(prepared_root)
    if task_ids:
        requested = {
            _task_id_from_dir_name(str(task_id)) or str(task_id)
            for task_id in task_ids
        }
        available = {
            str(_task_id_from_dir_name(path.name)): path
            for path in task_dirs
        }
        missing = sorted(requested - available.keys())
        if missing:
            raise ValueError(
                "Prepared task directories not found for: "
                + ", ".join(missing)
            )
        task_dirs = [
            available[task_id]
            for task_id in sorted(requested)
        ]
    if not task_dirs:
        raise ValueError(f"No prepared task directories found in {prepared_root}")
    rows: list[dict[str, Json]] = []
    completed_requests = 0
    for task_dir in task_dirs:
        try:
            result = judge_case(
                task_dir=task_dir,
                results_dir=result_root,
                profile=profile,
                trace_mode=trace_mode,
                resume=resume,
                overwrite=overwrite,
                delay_before_request_s=(
                    profile.inter_task_delay_s
                    if completed_requests
                    else 0.0
                ),
            )
            resumed = result.pop("_resumed", False) is True
            if not resumed:
                completed_requests += 1
            rows.append(
                {
                    "taskId": result.get("taskId"),
                    "judgeProfile": profile.name,
                    "judgeModel": profile.model,
                    "traceMode": trace_mode,
                    "status": result.get("status"),
                    "score": result.get("score", 0.0),
                    "resumed": resumed,
                    **(
                        result.get("summary")
                        if isinstance(result.get("summary"), dict)
                        else {}
                    ),
                }
            )
        except Exception as exc:
            completed_requests += 1
            task_id = _task_id_from_dir_name(task_dir.name) or task_dir.name
            error_result = {
                "taskId": task_id,
                "judgeProfile": profile.name,
                "judgeModel": profile.model,
                "traceMode": trace_mode,
                "status": "error",
                "score": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            rows.append(error_result)
            error_dir = result_root / "_errors" / _task_dir_name(task_id)
            error_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                error_dir / "error.json",
                error_result,
            )
    scores = [
        float(row.get("score", 0.0))
        for row in rows
        if isinstance(row.get("score"), (int, float))
    ]
    report: dict[str, object] = {
        "version": 1,
        "createdAt": _iso_now(),
        "preparedDir": str(prepared_root),
        "resultsDir": str(result_root),
        "profile": profile.name,
        "traceMode": trace_mode,
        "summary": {
            "totalRuns": len(rows),
            "successful": sum(row.get("status") == "success" for row in rows),
            "failed": sum(row.get("status") != "success" for row in rows),
            "judged": completed_requests,
            "resumed": sum(row.get("resumed") is True for row in rows),
            "averageScore": sum(scores) / len(scores) if scores else 0.0,
            "perfectRuns": sum(score >= 1.0 for score in scores),
        },
        "results": rows,
    }
    _write_json(result_root / "batch_summary.json", report)
    return report
