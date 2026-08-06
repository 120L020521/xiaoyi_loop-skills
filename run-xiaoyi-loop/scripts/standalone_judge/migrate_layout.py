"""Normalize a Judge run directory and backfill incremental-run fingerprints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from standalone_judge.batch import (
    _prepared_input_fingerprint,
    _task_dir_name,
    _task_id_from_dir_name,
    _write_json,
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _rename_to_canonical(path: Path, *, task_id: str) -> Path:
    destination = path.parent / _task_dir_name(task_id)
    if path == destination:
        return path
    if destination.exists():
        raise FileExistsError(
            f"Cannot rename {path.name}: destination exists: {destination}"
        )
    path.rename(destination)
    return destination


def _normalize_agent_output_paths(task_dir: Path) -> bool:
    """Remove workstation-specific source paths from Judge-facing input."""
    agent_path = task_dir / "agent.json"
    if not agent_path.is_file():
        return False
    agent = _read_json(agent_path)
    trace = agent.get("trace")
    outputs = trace.get("outputs") if isinstance(trace, dict) else None
    manifest = outputs.get("outputManifest") if isinstance(outputs, dict) else None
    if not isinstance(manifest, list):
        return False
    changed = False
    for row in manifest:
        if not isinstance(row, dict):
            continue
        prepared_path = row.get("outputPath")
        if isinstance(prepared_path, str) and row.get("sourcePath") != prepared_path:
            row["sourcePath"] = prepared_path
            changed = True
    if changed:
        _write_json(agent_path, agent)
    return changed


def migrate_run_dir(run_dir: Path) -> dict[str, int]:
    root = run_dir.expanduser().resolve()
    prepared_root = root / "prepared"
    results_root = root / "results"
    if not prepared_root.is_dir():
        raise NotADirectoryError(f"Prepared directory not found: {prepared_root}")

    fingerprints: dict[str, dict[str, object]] = {}
    renamed_prepared = 0
    normalized_agents = 0
    updated_manifests = 0
    for original in sorted(path for path in prepared_root.glob("task*") if path.is_dir()):
        manifest_path = original / "case_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        task_id = str(
            manifest.get("taskId")
            or _task_id_from_dir_name(original.name)
            or ""
        )
        canonical = _rename_to_canonical(original, task_id=task_id)
        if canonical != original:
            renamed_prepared += 1
        if _normalize_agent_output_paths(canonical):
            normalized_agents += 1
        fingerprint = _prepared_input_fingerprint(canonical)
        manifest["taskDir"] = str(canonical)
        manifest["inputFingerprint"] = fingerprint
        _write_json(canonical / "case_manifest.json", manifest)
        fingerprints[task_id] = fingerprint
        updated_manifests += 1

    report_path = prepared_root / "prepare_report.json"
    if report_path.is_file():
        report = _read_json(report_path)
        report["preparedDir"] = str(prepared_root)
        rows = report.get("cases")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                task_id = str(row.get("taskId") or "")
                if task_id in fingerprints:
                    row["taskDir"] = str(prepared_root / _task_dir_name(task_id))
                    row["inputFingerprint"] = fingerprints[task_id]
        _write_json(report_path, report)

    renamed_results = 0
    updated_results = 0
    if results_root.is_dir():
        for profile_dir in sorted(path for path in results_root.iterdir() if path.is_dir()):
            if profile_dir.name == "_errors":
                continue
            for original in sorted(path for path in profile_dir.glob("task*") if path.is_dir()):
                result_path = original / "judge_result.json"
                if not result_path.is_file():
                    continue
                result = _read_json(result_path)
                task_id = str(
                    result.get("taskId")
                    or _task_id_from_dir_name(original.name)
                    or ""
                )
                canonical = _rename_to_canonical(original, task_id=task_id)
                if canonical != original:
                    renamed_results += 1
                if task_id in fingerprints:
                    result["inputFingerprint"] = fingerprints[task_id]
                    _write_json(canonical / "judge_result.json", result)
                    updated_results += 1

            summary_path = profile_dir / "batch_summary.json"
            if summary_path.is_file():
                summary = _read_json(summary_path)
                summary["preparedDir"] = str(prepared_root)
                summary["resultsDir"] = str(profile_dir)
                _write_json(summary_path, summary)

    return {
        "renamedPrepared": renamed_prepared,
        "normalizedAgents": normalized_agents,
        "updatedManifests": updated_manifests,
        "renamedResults": renamed_results,
        "updatedResults": updated_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Judge run root")
    args = parser.parse_args()
    report = migrate_run_dir(args.run_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
