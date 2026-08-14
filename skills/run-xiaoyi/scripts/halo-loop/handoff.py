"""Create and validate root-level XiaoYi -> Judge -> HALO batch handoffs."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from render_batch_report import (
    DEFAULT_ARCHIVE_THRESHOLD,
    read_batch_payload,
    render_batch_report,
)


SCHEMA_VERSION = 3
PRODUCER = "run-xiaoyi"
LEGACY_PRODUCERS = ("run-xiaoyi-halo-loop",)
DIAGNOSE_MODES = ("failed", "all")
ROOT_FIELDS = ("logs", "judge_run", "halo_output")
TOP_LEVEL_FIELDS = (
    "schema_version",
    "producer",
    "task_ids",
    "diagnose_mode",
    "roots",
)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{label} is invalid JSON at line {exc.lineno}, column {exc.colno}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_keys(value: dict[str, Any], expected: tuple[str, ...], label: str) -> None:
    missing = sorted(set(expected).difference(value))
    extra = sorted(set(value).difference(expected))
    if missing:
        raise ValueError(f"{label} missing fields: {', '.join(missing)}")
    if extra:
        raise ValueError(f"{label} unsupported fields: {', '.join(extra)}")


def normalize_handoff(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(value, TOP_LEVEL_FIELDS, "handoff")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"handoff schema_version must be {SCHEMA_VERSION}")
    if value["producer"] not in (PRODUCER, *LEGACY_PRODUCERS):
        allowed = ", ".join((PRODUCER, *LEGACY_PRODUCERS))
        raise ValueError(f"handoff producer must be one of: {allowed}")
    if value["diagnose_mode"] not in DIAGNOSE_MODES:
        raise ValueError("handoff diagnose_mode must be failed or all")

    raw_ids = value["task_ids"]
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("handoff task_ids must be a non-empty array")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in raw_ids):
        raise ValueError("handoff task_ids must contain positive integers")
    task_ids = sorted(set(raw_ids))

    roots = value["roots"]
    if not isinstance(roots, dict):
        raise ValueError("handoff roots must be a JSON object")
    _require_exact_keys(roots, ROOT_FIELDS, "handoff roots")
    normalized_roots: dict[str, str] = {}
    for field in ROOT_FIELDS:
        raw = roots[field]
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"handoff roots.{field} must be a non-empty string")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError(f"handoff roots.{field} must be an absolute path: {raw}")
        normalized_roots[field] = str(path.resolve())

    distinct_roots = {path.casefold() for path in normalized_roots.values()}
    if len(distinct_roots) != len(ROOT_FIELDS):
        raise ValueError("handoff runtime roots must be three distinct directories")

    return {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "task_ids": task_ids,
        "diagnose_mode": value["diagnose_mode"],
        "roots": normalized_roots,
    }


def task_candidate_paths(handoff: dict[str, Any], task_id: int) -> dict[str, Path]:
    roots = {name: Path(path) for name, path in handoff["roots"].items()}
    task_name = f"task{task_id}"
    log_task_dir = roots["logs"] / task_name
    judge_task_dir = roots["judge_run"] / task_name
    return {
        "trace_jsonl": log_task_dir / f"{task_name}.jsonl",
        "runner_meta_json": log_task_dir / f"{task_name}.meta.json",
        "task_json": judge_task_dir / "metadata.json",
        "case_manifest": judge_task_dir / "case_manifest.json",
        "judge_result_json": judge_task_dir / "judge_result.json",
        "halo_artifact_dir": roots["halo_output"] / f"{task_name}_halo",
    }


def _as_task_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def resolve_handoff(value: dict[str, Any]) -> dict[str, Any]:
    handoff = normalize_handoff(value)
    errors: list[str] = []
    roots = {name: Path(path) for name, path in handoff["roots"].items()}
    if not roots["logs"].is_dir():
        errors.append(f"roots.logs is not a directory: {roots['logs']}")

    judge_batch_summary = roots["judge_run"] / "batch_summary.json"
    tasks: list[dict[str, Any]] = []
    for task_id in handoff["task_ids"]:
        paths = task_candidate_paths(handoff, task_id)
        context_warnings: list[str] = []
        trace_exists = paths["trace_jsonl"].is_file()
        metadata: dict[str, Any] = {}
        manifest: dict[str, Any] = {}
        judge: dict[str, Any] = {}
        judge_status = "missing"
        task_text: str | None = None
        expected_output_files: list[str] | None = None
        runner_status = "unknown"

        if paths["runner_meta_json"].is_file():
            try:
                runner_meta = _read_json(paths["runner_meta_json"], "Runner metadata")
                raw_status = runner_meta.get("status")
                if raw_status == "completed":
                    runner_status = "completed"
                elif isinstance(raw_status, str) and raw_status:
                    runner_status = "failed"
            except ValueError as exc:
                context_warnings.append(str(exc))

        if paths["task_json"].is_file():
            try:
                candidate = _read_json(paths["task_json"], "task metadata")
                if _as_task_id(candidate.get("absolute_id")) != task_id:
                    raise ValueError(f"metadata absolute_id does not match task {task_id}")
                metadata = candidate
            except ValueError as exc:
                context_warnings.append(str(exc))

        if metadata:
            raw_task = metadata.get("task") or metadata.get("description")
            if isinstance(raw_task, str) and raw_task.strip():
                task_text = raw_task
            else:
                context_warnings.append(
                    f"task metadata task/description is missing for task {task_id}"
                )
            raw_output_files = metadata.get("output_files") or metadata.get(
                "expected_output_files"
            )
            if isinstance(raw_output_files, list) and all(
                isinstance(item, str) and item.strip() for item in raw_output_files
            ):
                expected_output_files = raw_output_files
            elif raw_output_files is not None:
                context_warnings.append(
                    f"task metadata output_files/expected_output_files is invalid for task {task_id}"
                )

        if paths["case_manifest"].is_file() and paths["judge_result_json"].is_file():
            try:
                candidate_manifest = _read_json(paths["case_manifest"], "case manifest")
                candidate_judge = _read_json(paths["judge_result_json"], "Judge result")
                if _as_task_id(candidate_manifest.get("taskId")) != task_id:
                    raise ValueError(f"case manifest taskId does not match task {task_id}")
                if _as_task_id(candidate_judge.get("taskId")) != task_id:
                    raise ValueError(f"Judge taskId does not match task {task_id}")
                raw_judge_status = candidate_judge.get("status")
                if raw_judge_status != "success":
                    judge_status = "error" if raw_judge_status == "error" else "invalid"
                    raise ValueError(f"Judge status is not success for task {task_id}")
                if candidate_manifest.get("inputFingerprint") != candidate_judge.get(
                    "inputFingerprint"
                ):
                    raise ValueError(f"inputFingerprint mismatch for task {task_id}")
                manifest = candidate_manifest
                judge = candidate_judge
                judge_status = "success"
            except ValueError as exc:
                if judge_status == "missing":
                    judge_status = "invalid"
                context_warnings.append(str(exc))
        elif paths["case_manifest"].is_file() or paths["judge_result_json"].is_file():
            judge_status = "invalid"
            context_warnings.append(
                f"incomplete Judge context for task {task_id}; using trace without Judge"
            )

        judge_passed = judge.get("passed") if judge else None
        if judge and not isinstance(judge_passed, bool):
            context_warnings.append(f"Judge passed is invalid for task {task_id}")
            judge = {}
            judge_status = "invalid"
            judge_passed = None
        judge_score = judge.get("score") if judge else None
        if judge and (
            isinstance(judge_score, bool)
            or not isinstance(judge_score, (int, float))
            or not 0 <= float(judge_score) <= 1
        ):
            context_warnings.append(f"Judge score is invalid for task {task_id}")
            judge = {}
            judge_status = "invalid"
            judge_passed = None
            judge_score = None

        selected_by_mode = (
            handoff["diagnose_mode"] == "all"
            or runner_status == "failed"
            or judge_passed is False
            or judge_status != "success"
        )
        eligible = trace_exists and selected_by_mode
        skip_reason: str | None = None
        if not trace_exists:
            skip_reason = "trace_missing"
        elif not selected_by_mode:
            skip_reason = "filtered_by_diagnose_mode"

        resolved_paths = {
            "trace_jsonl": str(paths["trace_jsonl"]),
            "halo_artifact_dir": str(paths["halo_artifact_dir"]),
        }
        if metadata:
            resolved_paths["task_json"] = str(paths["task_json"])
        if judge:
            resolved_paths["case_manifest"] = str(paths["case_manifest"])
            resolved_paths["judge_result_json"] = str(paths["judge_result_json"])

        build_prompt_inputs: dict[str, str] = {}
        if "task_json" in resolved_paths:
            build_prompt_inputs["task_json"] = resolved_paths["task_json"]
        if judge:
            build_prompt_inputs["judge_result_json"] = str(paths["judge_result_json"])

        record = {
            "task_id": task_id,
            "runner_status": runner_status,
            "judge_status": judge_status,
            "eligible": eligible,
            "skip_reason": skip_reason,
            "judge_passed": judge_passed,
            "judge_score": judge_score,
            "paths": resolved_paths,
            "build_prompt_inputs": build_prompt_inputs,
            "prompt_context": {
                "task": task_text,
                "expected_output_files": expected_output_files,
            },
            "context_warnings": context_warnings,
        }
        tasks.append(record)

    return {
        "schema_version": SCHEMA_VERSION,
        "diagnose_mode": handoff["diagnose_mode"],
        "roots": handoff["roots"],
        "judge_batch_summary": str(judge_batch_summary),
        "tasks": tasks,
        "eligible_task_ids": [item["task_id"] for item in tasks if item["eligible"]],
        "skipped_task_ids": [
            item["task_id"] for item in tasks if item["skip_reason"] == "trace_missing"
        ],
        "errors": errors,
    }


def _resolve_from_workspace_config(
    workspace: Path,
    config: Path | None,
) -> tuple[Path, Path]:
    """Reuse run-xiaoyi-loop as the only source of runtime-path semantics."""
    skills_root = Path(__file__).resolve().parents[3]
    runner_skill_root = skills_root / "run-xiaoyi-loop"
    runner_scripts = runner_skill_root / "scripts"
    if not (runner_skill_root / "SKILL.md").is_file() or not runner_scripts.is_dir():
        raise ValueError(f"sibling run-xiaoyi-loop skill is missing: {runner_skill_root}")

    scripts_text = str(runner_scripts)
    inserted = scripts_text not in sys.path
    if inserted:
        sys.path.insert(0, scripts_text)
    try:
        from xiaoyi_loop.runtime_paths import resolve_runtime_paths
        from xiaoyi_loop.settings import ConfigError, load_local_settings
        from xiaoyi_loop.workspace_runtime import resolve_workspace_config

        config_path = resolve_workspace_config(workspace, explicit=config)
        settings = load_local_settings(
            project_root=runner_skill_root,
            config_path=config_path,
            discover_default_config=False,
        )
        runtime = resolve_runtime_paths(settings, workspace=workspace)
    except (ConfigError, OSError, ValueError) as exc:
        raise ValueError(f"unable to resolve run-xiaoyi-loop workspace config: {exc}") from exc
    finally:
        if inserted:
            sys.path.remove(scripts_text)
    return runtime.logs_dir, runtime.run_dir


def _workspace_path(path: Path, workspace: Path) -> Path:
    candidate = path.expanduser()
    return (
        candidate.resolve()
        if candidate.is_absolute()
        else (workspace / candidate).resolve()
    )


def create_handoff(args: argparse.Namespace) -> int:
    workspace = getattr(args, "workspace", Path.cwd()).expanduser().resolve()
    configured_logs: Path | None = None
    configured_judge: Path | None = None
    if args.logs_root is None or args.judge_run_root is None:
        configured_logs, configured_judge = _resolve_from_workspace_config(
            workspace,
            getattr(args, "config", None),
        )
    logs_root = (
        _workspace_path(args.logs_root, workspace)
        if args.logs_root is not None
        else configured_logs
    )
    judge_run_root = (
        _workspace_path(args.judge_run_root, workspace)
        if args.judge_run_root is not None
        else configured_judge
    )
    if logs_root is None or judge_run_root is None:
        raise ValueError("unable to resolve XiaoYi logs and Judge roots")
    halo_output_root = (
        _workspace_path(args.halo_output_root, workspace)
        if args.halo_output_root is not None
        else judge_run_root.parent / "xiaoyi_halo"
    )
    value = normalize_handoff({
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "task_ids": args.task_ids,
        "diagnose_mode": args.diagnose_mode,
        "roots": {
            "logs": str(logs_root),
            "judge_run": str(judge_run_root),
            "halo_output": str(halo_output_root),
        },
    })
    output = (
        _workspace_path(args.output, workspace)
        if args.output is not None
        else halo_output_root / "handoff.json"
    )
    _write_json(output, value)
    print(json.dumps({"status": "ok", "handoff": str(output)}, ensure_ascii=False))
    return 0


def validate_command(args: argparse.Namespace, *, show_paths: bool) -> int:
    try:
        value = _read_json(args.handoff.resolve(), "handoff")
        resolved = resolve_handoff(value)
    except ValueError as exc:
        print(json.dumps({"status": "error", "errors": [str(exc)]}, ensure_ascii=False))
        return 2
    if show_paths:
        output = resolved
    else:
        output = {
            "schema_version": resolved["schema_version"],
            "diagnose_mode": resolved["diagnose_mode"],
            "task_count": len(resolved["tasks"]),
            "eligible_task_ids": resolved["eligible_task_ids"],
            "skipped_task_ids": resolved["skipped_task_ids"],
            "errors": resolved["errors"],
        }
    output["status"] = "ok" if not resolved["errors"] else "error"
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not resolved["errors"] else 2


def _read_current_halo_report(
    task: dict[str, Any],
    *,
    handoff_path: Path,
) -> tuple[Path, dict[str, Any]]:
    artifact_dir = Path(task["paths"]["halo_artifact_dir"])
    manifest_path = artifact_dir / "halo-prepared-manifest.json"
    report_path = artifact_dir / "halo_report.json"
    for label, path in (("HALO manifest", manifest_path), ("HALO report", report_path)):
        if not path.is_file():
            raise ValueError(f"{label} does not exist: {path}")
        if path.stat().st_mtime_ns < handoff_path.stat().st_mtime_ns:
            raise ValueError(f"{label} predates the current handoff: {path}")

    manifest = _read_json(manifest_path, "HALO manifest")
    entries = manifest.get("prepared_traces")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError(f"HALO manifest must contain exactly one prepared trace: {manifest_path}")
    entry = entries[0]
    source = entry.get("source")
    manifest_report = entry.get("report_path")
    if not isinstance(source, str) or Path(source).resolve() != Path(
        task["paths"]["trace_jsonl"]
    ).resolve():
        raise ValueError(f"HALO manifest source does not match the current trace: {manifest_path}")
    if not isinstance(manifest_report, str) or Path(manifest_report).resolve() != report_path.resolve():
        raise ValueError(f"HALO manifest report_path does not match: {manifest_path}")
    return report_path, _read_json(report_path, "HALO report")


def summarize_command(args: argparse.Namespace) -> int:
    try:
        handoff_path = args.handoff.resolve()
        resolved = resolve_handoff(_read_json(handoff_path, "handoff"))
    except ValueError as exc:
        print(json.dumps({"status": "error", "errors": [str(exc)]}, ensure_ascii=False))
        return 2
    if resolved["errors"]:
        print(json.dumps({"status": "error", "errors": resolved["errors"]}, ensure_ascii=False))
        return 2

    summary_tasks: list[dict[str, Any]] = []
    errors: list[str] = []
    reported = 0
    for task in resolved["tasks"]:
        task_id = task["task_id"]
        trace_path = Path(task["paths"]["trace_jsonl"])
        judge_result_path = (
            Path(resolved["roots"]["judge_run"])
            / f"task{task_id}"
            / "judge_result.json"
        )
        record: dict[str, Any] = {
            "task_id": task_id,
            "task": task["prompt_context"].get("task"),
            "trace_fingerprint": _sha256_file(trace_path) if trace_path.is_file() else "",
            "runner_status": task["runner_status"],
            "judge_status": task["judge_status"],
            "judge_passed": task["judge_passed"],
            "judge_score": task["judge_score"],
            "halo_status": "not_selected",
            "execution_classification": "",
            "primary_failure_mode": "",
            "error_findings": [],
            "proposed_changes": [],
            "report_path": "",
            "report_uri": "",
            "judge_result_uri": (
                judge_result_path.resolve().as_uri()
                if judge_result_path.is_file()
                else ""
            ),
            "trace_uri": trace_path.resolve().as_uri() if trace_path.is_file() else "",
            "halo_message": "",
        }
        if task["eligible"]:
            try:
                report_path, report = _read_current_halo_report(
                    task,
                    handoff_path=handoff_path,
                )
                record["report_path"] = str(report_path)
                record["report_uri"] = report_path.resolve().as_uri()
                if report.get("schema_version") != 7:
                    raise ValueError(f"HALO report schema_version must be 7: {report_path}")
                report_summary = report.get("report_summary")
                if not isinstance(report_summary, dict):
                    raise ValueError(f"HALO report summary must be an object: {report_path}")
                diagnosis = report.get("diagnosis")
                if not isinstance(diagnosis, dict):
                    raise ValueError(f"HALO report diagnosis must be an object: {report_path}")
                classification = diagnosis.get("execution_classification")
                if not isinstance(classification, str) or not classification:
                    raise ValueError(f"HALO report classification is missing: {report_path}")
                primary_failure_mode = diagnosis.get("primary_failure_mode")
                findings = diagnosis.get("error_findings")
                changes = report.get("proposed_changes")
                if not isinstance(primary_failure_mode, str):
                    raise ValueError(f"HALO report primary_failure_mode is invalid: {report_path}")
                if not isinstance(findings, list):
                    raise ValueError(f"HALO report error_findings must be an array: {report_path}")
                if not isinstance(changes, list):
                    raise ValueError(f"HALO report proposed_changes must be an array: {report_path}")
                record["halo_status"] = "success"
                record["execution_classification"] = classification
                record["primary_failure_mode"] = primary_failure_mode
                record["error_findings"] = findings
                record["proposed_changes"] = changes
                report_task = report_summary.get("task")
                if isinstance(report_task, str) and report_task:
                    record["task"] = report_task
                reported += 1
            except ValueError as exc:
                record["halo_status"] = "error"
                record["halo_message"] = str(exc)
                errors.append(f"task {task_id}: {exc}")
        elif task["skip_reason"] == "trace_missing":
            record["halo_status"] = "skipped_missing_trace"
            record["halo_message"] = "Trace 缺失，已跳过诊断。"
        elif task["skip_reason"] == "filtered_by_diagnose_mode":
            record["halo_status"] = "skipped_by_mode"
            record["halo_message"] = "未命中当前诊断模式，已跳过。"
        summary_tasks.append(record)

    output_path = (
        args.output.resolve()
        if args.output is not None
        else Path(resolved["roots"]["halo_output"]) / "batch_diagnosis_report.html"
    )
    archive_paths = render_batch_report(
        output_path,
        handoff_path=handoff_path,
        diagnose_mode=resolved["diagnose_mode"],
        tasks=summary_tasks,
        errors=errors,
        archive_threshold=getattr(args, "archive_threshold", DEFAULT_ARCHIVE_THRESHOLD),
    )
    print(json.dumps({
        "status": "ok" if not errors else "partial",
        "html_report": str(output_path),
        "html_archives_created": [str(path) for path in archive_paths],
        "totals": {
            "requested": len(resolved["tasks"]),
            "eligible": len(resolved["eligible_task_ids"]),
            "skipped_missing_trace": len(resolved["skipped_task_ids"]),
            "reported": reported,
            "failed": len(errors),
        },
        "tasks": [
            {
                "task_id": item["task_id"],
                "judge_status": item["judge_status"],
                "halo_status": item["halo_status"],
                "report_path": item["report_path"],
            }
            for item in summary_tasks
        ],
        "errors": errors,
    }, ensure_ascii=False))
    return 0


def self_test() -> int:
    checks = 0

    def expect_value_error(value: dict[str, Any], expected: str) -> None:
        nonlocal checks
        try:
            normalize_handoff(value)
        except ValueError as exc:
            if expected not in str(exc):
                raise AssertionError(f"unexpected validation error: {exc}") from exc
            checks += 1
            return
        raise AssertionError(f"expected validation error containing: {expected}")

    with tempfile.TemporaryDirectory(prefix="xiaoyi-halo-handoff-") as temp:
        root = Path(temp)
        roots = {
            "logs": root / "xiaoyi_logs",
            "judge_run": root / "xiaoyi_judge",
            "halo_output": root / "xiaoyi_halo",
        }
        for name, path in roots.items():
            if name != "halo_output":
                path.mkdir(parents=True)

        fingerprint = {"algorithm": "sha256", "value": "abc", "fileCount": 3}
        for task_id, passed, score, runner_state in (
            (14, False, 0.5, "completed"),
            (15, True, 1.0, "completed"),
            (16, True, 1.0, "timeout"),
        ):
            task_name = f"task{task_id}"
            log_dir = roots["logs"] / task_name
            judge_dir = roots["judge_run"] / task_name
            log_dir.mkdir(parents=True)
            judge_dir.mkdir(parents=True)
            (log_dir / f"{task_name}.jsonl").write_text("{}\n", encoding="utf-8")
            _write_json(log_dir / f"{task_name}.meta.json", {
                "task_id": task_id,
                "status": runner_state,
            })
            _write_json(judge_dir / "metadata.json", {
                "absolute_id": task_id,
                "task": f"task {task_id}",
                "output_files": [f"result-{task_id}.txt"],
            })
            _write_json(judge_dir / "case_manifest.json", {
                "taskId": str(task_id),
                "inputFingerprint": fingerprint,
            })
            _write_json(judge_dir / "judge_result.json", {
                "taskId": str(task_id),
                "status": "success",
                "inputFingerprint": fingerprint,
                "passed": passed,
                "score": score,
            })

        trace_only_dir = roots["logs"] / "task17"
        trace_only_dir.mkdir(parents=True)
        (trace_only_dir / "task17.jsonl").write_text("{}\n", encoding="utf-8")

        judge_error_log_dir = roots["logs"] / "task19"
        judge_error_dir = roots["judge_run"] / "task19"
        judge_error_log_dir.mkdir(parents=True)
        judge_error_dir.mkdir(parents=True)
        (judge_error_log_dir / "task19.jsonl").write_text("{}\n", encoding="utf-8")
        _write_json(judge_error_log_dir / "task19.meta.json", {
            "task_id": 19,
            "status": "completed",
        })
        _write_json(judge_error_dir / "metadata.json", {
            "absolute_id": 19,
            "task": "task 19",
            "output_files": ["result-19.txt"],
        })
        _write_json(judge_error_dir / "case_manifest.json", {
            "taskId": "19",
            "inputFingerprint": fingerprint,
        })
        _write_json(judge_error_dir / "judge_result.json", {
            "taskId": "19",
            "status": "error",
            "inputFingerprint": fingerprint,
            "error": "judge unavailable",
        })

        judge_batch_summary = roots["judge_run"] / "batch_summary.json"
        _write_json(judge_batch_summary, {
            "profile": "agent",
            "taskIds": [14, 15, 16, 19],
        })

        handoff = normalize_handoff({
            "schema_version": SCHEMA_VERSION,
            "producer": PRODUCER,
            "task_ids": [19, 18, 17, 16, 15, 14, 17],
            "diagnose_mode": "failed",
            "roots": {name: str(path.resolve()) for name, path in roots.items()},
        })

        default_handoff_path = roots["halo_output"] / "handoff.json"
        with contextlib.redirect_stdout(io.StringIO()):
            create_status = create_handoff(argparse.Namespace(
                output=None,
                workspace=root,
                config=root / "missing-but-overridden.toml",
                logs_root=roots["logs"],
                judge_run_root=roots["judge_run"],
                halo_output_root=None,
                diagnose_mode="all",
                task_ids=[14, 15, 16, 17, 18, 19],
            ))
        if create_status != 0 or not default_handoff_path.is_file():
            raise AssertionError("create did not derive xiaoyi_halo/handoff.json")
        created = _read_json(default_handoff_path, "default handoff")
        if Path(created["roots"]["halo_output"]) != roots["halo_output"]:
            raise AssertionError("create did not derive the xiaoyi_halo root")
        checks += 1
        if created["diagnose_mode"] != "all":
            raise AssertionError("create did not preserve all-task diagnosis mode")
        checks += 1
        parser_defaults = build_parser().parse_args([
            "create",
            "--logs-root", str(roots["logs"]),
            "--judge-run-root", str(roots["judge_run"]),
            "--task-id", "14",
        ])
        if parser_defaults.diagnose_mode != "all":
            raise AssertionError("create parser default diagnosis mode is not all")
        checks += 1

        default_workspace = root / "default-workspace"
        default_workspace.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            default_status = create_handoff(argparse.Namespace(
                output=None,
                workspace=default_workspace,
                config=None,
                logs_root=None,
                judge_run_root=None,
                halo_output_root=None,
                diagnose_mode="all",
                task_ids=[14],
            ))
        default_created = _read_json(
            default_workspace / "xiaoyi_halo" / "handoff.json",
            "workspace-default handoff",
        )
        if default_status != 0 or default_created["roots"] != {
            "logs": str((default_workspace / "xiaoyi_logs").resolve()),
            "judge_run": str((default_workspace / "xiaoyi_judge").resolve()),
            "halo_output": str((default_workspace / "xiaoyi_halo").resolve()),
        }:
            raise AssertionError(f"workspace defaults did not resolve: {default_created}")
        checks += 1

        configured_workspace = root / "configured-workspace"
        configured_runtime = configured_workspace / ".xiaoyi-loop"
        configured_runtime.mkdir(parents=True)
        (configured_runtime / "local.toml").write_text(
            """version = 1

[paths]
logs_dir = "artifacts/custom_logs"
run_dir = "artifacts/custom_judge"
state_file = "artifacts/custom_state.json"
""",
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            configured_status = create_handoff(argparse.Namespace(
                output=None,
                workspace=configured_workspace,
                config=None,
                logs_root=None,
                judge_run_root=None,
                halo_output_root=None,
                diagnose_mode="all",
                task_ids=[14],
            ))
        configured_created = _read_json(
            configured_workspace / "artifacts" / "xiaoyi_halo" / "handoff.json",
            "configured handoff",
        )
        if configured_status != 0 or configured_created["roots"] != {
            "logs": str((configured_workspace / "artifacts" / "custom_logs").resolve()),
            "judge_run": str((configured_workspace / "artifacts" / "custom_judge").resolve()),
            "halo_output": str((configured_workspace / "artifacts" / "xiaoyi_halo").resolve()),
        }:
            raise AssertionError(f"workspace configuration did not resolve: {configured_created}")
        checks += 1
        if list(handoff) != list(TOP_LEVEL_FIELDS):
            raise AssertionError("handoff key order is not canonical")
        checks += 1
        if any(key not in ROOT_FIELDS for key in handoff["roots"]):
            raise AssertionError("handoff contains a non-root path")
        checks += 1
        if handoff["task_ids"] != [14, 15, 16, 17, 18, 19]:
            raise AssertionError("handoff task IDs were not sorted and deduplicated")
        checks += 1

        legacy = dict(handoff)
        legacy["schema_version"] = 2
        expect_value_error(legacy, "schema_version must be 3")

        extra_field = dict(handoff)
        extra_field["task_paths"] = {}
        expect_value_error(extra_field, "unsupported fields: task_paths")

        relative_root = dict(handoff)
        relative_root["roots"] = dict(handoff["roots"])
        relative_root["roots"]["logs"] = "xiaoyi_logs"
        expect_value_error(relative_root, "roots.logs must be an absolute path")

        scattered_roots = dict(handoff)
        scattered_roots["roots"] = dict(handoff["roots"])
        scattered_roots["roots"]["halo_output"] = str(
            (root / "other" / "xiaoyi_halo").resolve()
        )
        normalized_scattered = normalize_handoff(scattered_roots)
        if normalized_scattered["roots"]["halo_output"] != scattered_roots["roots"]["halo_output"]:
            raise AssertionError("configured independent root was not preserved")
        checks += 1

        duplicate_roots = dict(handoff)
        duplicate_roots["roots"] = dict(handoff["roots"])
        duplicate_roots["roots"]["halo_output"] = duplicate_roots["roots"]["judge_run"]
        expect_value_error(duplicate_roots, "must be three distinct directories")

        judge_batch_summary.unlink()

        resolved = resolve_handoff(handoff)
        if (
            resolved["errors"]
            or resolved["eligible_task_ids"] != [14, 16, 17, 19]
            or resolved["skipped_task_ids"] != [18]
        ):
            raise AssertionError(f"unexpected resolution: {resolved}")
        checks += 1
        expected_result = roots["judge_run"] / "task14" / "judge_result.json"
        if Path(resolved["tasks"][0]["paths"]["judge_result_json"]) != expected_result:
            raise AssertionError("Judge result did not resolve from judge_run/task<ID>")
        checks += 1
        prompt_context = resolved["tasks"][0]["prompt_context"]
        if prompt_context != {
            "task": "task 14",
            "expected_output_files": ["result-14.txt"],
        }:
            raise AssertionError(f"unexpected build-prompt context: {prompt_context}")
        checks += 1

        failed_task = resolved["tasks"][2]
        if (
            failed_task["runner_status"] != "failed"
            or failed_task["judge_status"] != "success"
            or "judge_result_json" not in failed_task["build_prompt_inputs"]
        ):
            raise AssertionError(f"unexpected failed-runner resolution: {failed_task}")
        checks += 1
        if Path(failed_task["paths"]["trace_jsonl"]) != roots["logs"] / "task16" / "task16.jsonl":
            raise AssertionError("failed Runner trace did not use the normal task<ID> layout")
        checks += 1

        trace_only_task = resolved["tasks"][3]
        if (
            trace_only_task["judge_status"] != "missing"
            or trace_only_task["build_prompt_inputs"]
            or trace_only_task["skip_reason"] is not None
        ):
            raise AssertionError(f"unexpected trace-only resolution: {trace_only_task}")
        checks += 1

        judge_error_task = resolved["tasks"][5]
        if judge_error_task["judge_status"] != "error" or judge_error_task["skip_reason"] is not None:
            raise AssertionError(f"unexpected Judge-error resolution: {judge_error_task}")
        checks += 1

        all_handoff = dict(handoff)
        all_handoff["diagnose_mode"] = "all"
        all_resolution = resolve_handoff(all_handoff)
        if (
            all_resolution["errors"]
            or all_resolution["eligible_task_ids"] != [14, 15, 16, 17, 19]
            or all_resolution["skipped_task_ids"] != [18]
        ):
            raise AssertionError(f"all-task diagnosis did not select every trace: {all_resolution}")
        checks += 1

        handoff_path = root / "handoff.json"
        summary_path = roots["halo_output"] / "batch_diagnosis_report.html"
        _write_json(handoff_path, handoff)

        def write_halo_artifacts(task_id: int, classification: str) -> tuple[Path, Path]:
            task = next(item for item in resolved["tasks"] if item["task_id"] == task_id)
            artifact_dir = Path(task["paths"]["halo_artifact_dir"])
            report_path = artifact_dir / "halo_report.json"
            manifest_path = artifact_dir / "halo-prepared-manifest.json"
            findings = [] if classification == "SUCCEEDED_CLEANLY" else [{
                "error_id": "ERR1",
                "priority": "P0",
                "category": "TOOL_FAILURE",
                "title": "测试工具调用失败",
                "occurrence_count": 1,
                "summary": "工具调用返回错误。",
                "evidence": [{
                    "source": "TRACE",
                    "reference": "span-1",
                    "tool": "bash",
                    "fact": "工具返回非零状态。",
                    "raw_log_excerpt": "exit code 1",
                    "error": "exit code 1",
                }],
                "root_cause": "测试环境与工具参数不兼容。",
                "recovery_status": "UNRECOVERED",
                "impact": "任务未完成。",
            }]
            changes = [] if classification == "SUCCEEDED_CLEANLY" else [{
                "priority": "P0",
                "component": "tool_impl",
                "target": "test_tool.py",
                "title": "修复工具参数校验",
                "error_refs": ["ERR1"],
                "problem": "错误参数未被提前拦截。",
                "implementation": "在调用前验证参数。",
                "acceptance_criteria": ["错误参数返回明确提示"],
                "expected_impact": "避免无效工具调用。",
            }]
            _write_json(report_path, {
                "schema_version": 7,
                "report_summary": {
                    "task_id": f"task{task_id}",
                    "task": f"测试任务 {task_id}",
                    "trace_ids": ["trace-1"],
                },
                "diagnosis": {
                    "execution_classification": classification,
                    "primary_failure_mode": (
                        "无显著失败。"
                        if classification == "SUCCEEDED_CLEANLY"
                        else "工具参数与运行环境不兼容。"
                    ),
                    "error_findings": findings,
                },
                "proposed_changes": changes,
            })
            _write_json(manifest_path, {
                "schema_version": 3,
                "prepared_traces": [{
                    "source": task["paths"]["trace_jsonl"],
                    "report_path": str(report_path),
                }],
            })
            return manifest_path, report_path

        stale_manifest, _ = write_halo_artifacts(14, "SUCCEEDED_CLEANLY")
        write_halo_artifacts(16, "FAILED")
        write_halo_artifacts(17, "FAILED")
        write_halo_artifacts(19, "FAILED")
        old_ns = handoff_path.stat().st_mtime_ns - 1_000_000_000
        os.utime(stale_manifest, ns=(old_ns, old_ns))
        stale_stdout = io.StringIO()
        with contextlib.redirect_stdout(stale_stdout):
            stale_status = summarize_command(argparse.Namespace(
                handoff=handoff_path,
                output=summary_path,
                archive_threshold=DEFAULT_ARCHIVE_THRESHOLD,
            ))
        stale_summary = json.loads(stale_stdout.getvalue())
        if stale_status != 0 or stale_summary["totals"]["failed"] != 1:
            raise AssertionError("stale HALO artifacts were not isolated as one Task failure")
        checks += 1

        legacy_html = summary_path.read_text(encoding="utf-8").replace(
            '"payload_schema_version":2,',
            "",
            1,
        )
        summary_path.write_text(legacy_html, encoding="utf-8")
        write_halo_artifacts(14, "SUCCEEDED_CLEANLY")
        summary_stdout = io.StringIO()
        with contextlib.redirect_stdout(summary_stdout):
            summary_status = summarize_command(argparse.Namespace(
                handoff=handoff_path,
                output=summary_path,
                archive_threshold=DEFAULT_ARCHIVE_THRESHOLD,
            ))
        if summary_status != 0:
            raise AssertionError("summary command failed")
        checks += 1
        summary = json.loads(summary_stdout.getvalue())
        if summary["totals"] != {
            "requested": 6,
            "eligible": 4,
            "skipped_missing_trace": 1,
            "reported": 4,
            "failed": 0,
        }:
            raise AssertionError(f"unexpected summary totals: {summary['totals']}")
        checks += 1
        html = summary_path.read_text(encoding="utf-8")
        if (
            "<!doctype html>" not in html
            or "小艺批次诊断报告" not in html
            or "position: sticky" not in html
            or "task color-" not in html
            or "task-nav-list" not in html
            or "filter-toggle" not in html
            or "问题是什么" not in html
            or "怎么解决" not in html
            or "是根据什么修改的" not in html
            or "span_id:" not in html
            or "raw-trace" not in html
            or "关键日志原文" not in html
            or "evidence-context" not in html
            or "evidence-index" not in html
            or "log-block" not in html
            or "修改依据来自" in html
            or "const raw =" in html
            or "__BATCH_DATA__" in html
        ):
            raise AssertionError("fixed-format HTML report is incomplete")
        checks += 1
        cumulative = read_batch_payload(summary_path)
        if cumulative is None:
            raise AssertionError("cumulative HTML payload is missing")
        cumulative_ids = [item["task_id"] for item in cumulative["tasks"]]
        task14 = next((item for item in cumulative["tasks"] if item["task_id"] == 14), None)
        if task14 is None:
            raise AssertionError(f"cumulative HTML lost task 14: {cumulative_ids}")
        if (
            cumulative["payload_schema_version"] != 2
            or
            cumulative["batch_runs"] != 2
            or len(cumulative_ids) != len(set(cumulative_ids))
            or task14["halo_status"] != "success"
        ):
            raise AssertionError("same-Task cumulative HTML replacement failed")
        checks += 1
        render_batch_report(
            summary_path,
            handoff_path=handoff_path,
            diagnose_mode="all",
            tasks=[{
                "task_id": 99,
                "task": "新增累计任务",
                "trace_fingerprint": "99" * 32,
                "runner_status": "completed",
                "judge_status": "missing",
                "judge_passed": None,
                "judge_score": None,
                "halo_status": "success",
                "execution_classification": "SUCCEEDED_CLEANLY",
                "primary_failure_mode": "无显著失败。",
                "error_findings": [],
                "proposed_changes": [],
                "report_path": "",
                "report_uri": "",
                "judge_result_uri": "",
                "trace_uri": "",
                "halo_message": "",
            }],
            errors=[],
        )
        cumulative = read_batch_payload(summary_path)
        if cumulative is None:
            raise AssertionError("updated cumulative HTML payload is missing")
        cumulative_ids = [item["task_id"] for item in cumulative["tasks"]]
        if (
            cumulative["batch_runs"] != 3
            or 99 not in cumulative_ids
            or 14 not in cumulative_ids
            or len(cumulative_ids) != len(set(cumulative_ids))
        ):
            raise AssertionError("new-Task cumulative HTML append failed")
        checks += 1
        original_task14 = next(item for item in cumulative["tasks"] if item["task_id"] == 14)
        alternate_task14 = dict(original_task14)
        alternate_task14["trace_fingerprint"] = "14" * 32
        alternate_task14["task"] = "相同 Task ID 的另一条 Trace"
        render_batch_report(
            summary_path,
            handoff_path=handoff_path,
            diagnose_mode="all",
            tasks=[alternate_task14],
            errors=[],
        )
        cumulative = read_batch_payload(summary_path)
        if cumulative is None:
            raise AssertionError("fingerprint-aware cumulative HTML payload is missing")
        task14_records = [item for item in cumulative["tasks"] if item["task_id"] == 14]
        if cumulative["batch_runs"] != 4 or len(task14_records) != 2:
            raise AssertionError("Trace fingerprint did not distinguish reused Task IDs")
        checks += 1

        render_batch_report(
            summary_path,
            handoff_path=handoff_path,
            diagnose_mode="all",
            tasks=[{
                **alternate_task14,
                "task_id": 100,
                "trace_fingerprint": "10" * 32,
            }],
            errors=[],
            archive_threshold=2,
        )
        cumulative = read_batch_payload(summary_path)
        archive_files = list(summary_path.parent.glob(
            f"{summary_path.stem}.archive-*{summary_path.suffix}"
        ))
        if (
            cumulative is None
            or len(cumulative["tasks"]) != 2
            or not cumulative.get("archives")
            or not archive_files
            or read_batch_payload(archive_files[0]) is None
        ):
            raise AssertionError("HTML archive threshold did not preserve overflow Tasks")
        checks += 1

    print(json.dumps({"status": "ok", "checks": checks}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Write a canonical root-level handoff")
    create.add_argument(
        "--output",
        type=Path,
        help="Handoff path; defaults to handoff.json under the resolved HALO root.",
    )
    create.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Agent workspace; defaults to the current directory.",
    )
    create.add_argument(
        "--config",
        type=Path,
        help="Optional XiaoYi local.toml; otherwise discover workspace configuration.",
    )
    create.add_argument(
        "--logs-root",
        type=Path,
        help="Explicit logs root; overrides workspace configuration.",
    )
    create.add_argument(
        "--judge-run-root",
        type=Path,
        help="Explicit Judge root; overrides workspace configuration.",
    )
    create.add_argument(
        "--halo-output-root",
        type=Path,
        help="HALO root; defaults to xiaoyi_halo beside the Judge root.",
    )
    create.add_argument("--diagnose-mode", choices=DIAGNOSE_MODES, default="all")
    create.add_argument("--task-id", type=int, action="append", dest="task_ids", required=True)
    create.set_defaults(func=create_handoff)

    validate = subparsers.add_parser("validate", help="Validate roots and current task artifacts")
    validate.add_argument("handoff", type=Path)
    validate.set_defaults(func=lambda args: validate_command(args, show_paths=False))

    resolve = subparsers.add_parser("resolve", help="Validate and print exact transient task paths")
    resolve.add_argument("handoff", type=Path)
    resolve.set_defaults(func=lambda args: validate_command(args, show_paths=True))

    summarize = subparsers.add_parser("summarize", help="Render the combined Judge/HALO HTML report")
    summarize.add_argument("handoff", type=Path)
    summarize.add_argument("--output", type=Path)
    summarize.add_argument(
        "--archive-threshold",
        type=int,
        default=DEFAULT_ARCHIVE_THRESHOLD,
        help="Keep at most this many Trace-identified Tasks in the main HTML; 0 disables archiving",
    )
    summarize.set_defaults(func=summarize_command)

    test = subparsers.add_parser("self-test", help="Run a standard-library smoke test")
    test.set_defaults(func=lambda _args: self_test())
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(json.dumps({"status": "error", "errors": [str(exc)]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
