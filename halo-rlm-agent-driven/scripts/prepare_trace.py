#!/usr/bin/env python3
"""Detect a trace JSONL format and prepare a HALO-readable span JSONL."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

def _first_record(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            return value
    raise ValueError(f"{path}: empty JSONL")


def detect_format(path: Path) -> str:
    record = _first_record(path)
    if (
        isinstance(record.get("trace_id"), str)
        and isinstance(record.get("span_id"), str)
        and isinstance(record.get("attributes"), dict)
    ):
        return "halo-span-jsonl"
    if isinstance(record.get("event"), str) and isinstance(record.get("payload"), dict):
        return "event-payload-jsonl"
    return "unknown-jsonl"


def _find_converter() -> Path:
    skill_root = Path(__file__).resolve().parents[1]
    candidate = (
        skill_root / "scripts" / "halo-trace-converter" / "convertToHaloTrace.py"
    )
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"bundled HALO trace converter was not found: {candidate}")


def _converter_mtime_ns(converter: Path) -> int:
    return max(path.stat().st_mtime_ns for path in converter.parent.rglob("*.py"))


def prepare_trace(source: Path, output: Path | None, force: bool) -> tuple[str, Path]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"trace file not found: {source}")

    detected = detect_format(source)
    if detected not in {"halo-span-jsonl", "event-payload-jsonl"}:
        raise ValueError(
            "unsupported JSONL shape: expected HALO span objects or event + payload objects"
        )
    converter = _find_converter() if detected == "event-payload-jsonl" else None

    destination = (
        output.resolve()
        if output is not None
        else (
            source
            if detected == "halo-span-jsonl"
            else source.with_name(f"{source.stem}.halo.jsonl")
        )
    )
    if destination == source:
        if detected == "halo-span-jsonl":
            return detected, source
        raise ValueError("refusing to overwrite the source trace")
    if destination.exists() and not force:
        valid_existing = detect_format(destination) == "halo-span-jsonl"
        required_mtime = source.stat().st_mtime_ns
        if converter is not None:
            required_mtime = max(required_mtime, _converter_mtime_ns(converter))
        current = destination.stat().st_mtime_ns >= required_mtime
        if valid_existing and current:
            return detected, destination
        force = True
    destination.parent.mkdir(parents=True, exist_ok=True)

    if detected == "halo-span-jsonl":
        shutil.copy2(source, destination)
        return detected, destination

    assert converter is not None
    command = [sys.executable, str(converter), str(source), str(destination)]
    if force:
        destination.unlink(missing_ok=True)
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"converter failed with exit code {completed.returncode}"
            + (f": {detail}" if detail else "")
        )

    if detect_format(destination) != "halo-span-jsonl":
        raise RuntimeError(f"converter produced a non-HALO file: {destination}")
    return detected, destination


def _logical_name(source: Path, detected: str) -> str:
    stem = source.stem
    if detected == "halo-span-jsonl" and stem.casefold().endswith(".halo"):
        return stem[:-5]
    return stem


def _default_output_root(source_root: Path) -> Path:
    return source_root.with_name(f"{source_root.name}_halo")


def _artifact_paths(
    source: Path,
    detected: str,
    source_root: Path,
    output_root: Path,
) -> tuple[Path, Path, Path, Path]:
    logical_name = _logical_name(source, detected)
    relative_parent = source.parent.relative_to(source_root)
    if not relative_parent.parts:
        artifact_dir = output_root / f"{logical_name}_halo"
    else:
        artifact_dir = (
            output_root
            / relative_parent.parent
            / f"{relative_parent.name}_halo"
        )
    return (
        artifact_dir / f"{logical_name}.halo.jsonl",
        artifact_dir / "halo_prompt.txt",
        artifact_dir / "halo_report.json",
        artifact_dir / "halo-prepared-manifest.json",
    )


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _is_generated_artifact_path(path: Path, input_root: Path) -> bool:
    """Return whether a path is inside a generated ``*_halo`` directory."""
    try:
        relative = path.relative_to(input_root)
    except ValueError:
        return False
    return any(part.casefold().endswith("_halo") for part in relative.parts[:-1])


def prepare_directory(
    source_dir: Path,
    force: bool,
    output_root: Path | None = None,
) -> dict[str, Any]:
    """Prepare one immutable directory snapshot and write per-trace manifests."""
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise NotADirectoryError(f"trace directory not found: {source_dir}")
    source_root = source_dir
    output_root = (
        output_root.resolve()
        if output_root is not None
        else _default_output_root(source_root).resolve()
    )

    # Snapshot once. Files created during conversion can never re-enter this run.
    legacy_output_dir = source_dir / "_halo"
    snapshot = sorted(
        path.resolve()
        for path in source_dir.rglob("*.jsonl")
        if path.is_file()
        and not _is_under(path.resolve(), legacy_output_dir)
        and not _is_under(path.resolve(), output_root)
        and not _is_generated_artifact_path(path.resolve(), source_dir)
    )
    classified: list[tuple[Path, str]] = []
    errors: list[dict[str, str]] = []
    for path in snapshot:
        try:
            classified.append((path, detect_format(path)))
        except (OSError, ValueError) as exc:
            classified.append((path, "unknown-jsonl"))
            errors.append({"path": str(path), "error": str(exc)})

    formats = {path: detected for path, detected in classified}
    prepared: list[dict[str, Any]] = []
    selected: set[str] = set()
    artifact_sources: dict[str, Path] = {}
    paired_companions: set[Path] = set()

    def prepare_one(source: Path, candidate: Path) -> None:
        destination, prompt_path, report_path, manifest_path = _artifact_paths(
            source, formats[source], source_root, output_root
        )
        artifact_key = str(destination.parent.resolve()).casefold()
        previous_source = artifact_sources.get(artifact_key)
        if previous_source is not None and previous_source != source:
            errors.append(
                {
                    "path": str(source),
                    "error": (
                        "multiple logical traces map to the same task artifact "
                        f"directory: {destination.parent} (already used by "
                        f"{previous_source})"
                    ),
                }
            )
            return
        artifact_sources[artifact_key] = source
        key = str(destination.resolve()).casefold()
        if key in selected:
            return
        selected.add(key)
        try:
            previous_mtime = (
                destination.stat().st_mtime_ns if destination.is_file() else None
            )
            prepare_trace(candidate, destination, force=force)
            current = (
                previous_mtime is not None
                and destination.stat().st_mtime_ns == previous_mtime
            )
            prepared.append(
                {
                    "source": str(source),
                    "selected": str(destination.resolve()),
                    "prompt_path": str(prompt_path.resolve()),
                    "report_path": str(report_path.resolve()),
                    "manifest_path": str(manifest_path.resolve()),
                    "action": "reused" if current else "prepared",
                }
            )
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"path": str(source), "error": str(exc)})

    for source, detected in classified:
        if detected != "event-payload-jsonl":
            continue
        companion = source.with_name(f"{source.stem}.halo.jsonl").resolve()
        if companion in formats:
            paired_companions.add(companion)
        # Raw events are authoritative. Never reuse a sibling conversion made
        # by an older converter with weaker status semantics.
        prepare_one(source, source)

    for source, detected in classified:
        if detected == "halo-span-jsonl" and source not in paired_companions:
            prepare_one(source, source)

    for path, detected in classified:
        if path in paired_companions:
            continue
        if detected == "unknown-jsonl" and not any(
            item["path"] == str(path) for item in errors
        ):
            errors.append(
                {
                    "path": str(path),
                    "error": (
                        "unsupported JSONL shape: expected HALO span objects "
                        "or event + payload objects"
                    ),
                }
            )

    manifest = {
        "schema_version": 3,
        "input_directory": str(source_dir),
        "output_directory": str(output_root),
        "snapshot_jsonl_count": len(snapshot),
        "prepared_traces": prepared,
        "errors": errors,
        "manifest_paths": [entry["manifest_path"] for entry in prepared],
    }
    for entry in prepared:
        trace_manifest = {
            "schema_version": 3,
            "input_directory": str(source_dir),
            "output_directory": str(output_root),
            "snapshot_jsonl_count": len(snapshot),
            "prepared_traces": [entry],
            "errors": errors,
        }
        Path(entry["manifest_path"]).write_text(
            json.dumps(trace_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect and convert trace JSONL into HALO-readable span JSONL.",
        allow_abbrev=False,
    )
    parser.add_argument("trace", type=Path, help="A JSONL file or directory")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--output-root",
        type=Path,
        help=(
            "Explicit artifact root. Directory names have no special meaning; "
            "if omitted, defaults to a sibling <input-name>_halo directory."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only print the detected format; do not convert.",
    )
    args = parser.parse_args()

    try:
        source = args.trace.resolve()
        if source.is_dir():
            if args.check:
                snapshot = sorted(
                    path.resolve()
                    for path in source.rglob("*.jsonl")
                    if path.is_file()
                    and not _is_under(path.resolve(), source / "halo")
                    and not _is_under(path.resolve(), source / "_halo")
                    and not _is_generated_artifact_path(path.resolve(), source)
                )
                checked = []
                has_unknown = False
                for path in snapshot:
                    try:
                        detected = detect_format(path)
                    except (OSError, ValueError):
                        detected = "unknown-jsonl"
                    has_unknown = has_unknown or detected == "unknown-jsonl"
                    checked.append({"path": str(path), "format": detected})
                print(
                    json.dumps(
                        {"directory": str(source), "files": checked},
                        ensure_ascii=False,
                    )
                )
                return 2 if has_unknown else 0
            manifest = prepare_directory(source, args.force, args.output_root)
            print(json.dumps(manifest, ensure_ascii=False))
            return 2 if manifest["errors"] else 0

        detected = detect_format(source)
        if args.check:
            print(json.dumps({"format": detected, "path": str(source)}))
            return 0 if detected != "unknown-jsonl" else 2
        source_root = source.parent
        output_root = (
            args.output_root.resolve()
            if args.output_root is not None
            else _default_output_root(source_root).resolve()
        )
        destination, prompt_path, report_path, manifest_path = _artifact_paths(
            source, detected, source_root, output_root
        )
        artifact_dir = destination.parent
        source_format, prepared = prepare_trace(source, destination, args.force)
        entry = {
            "source": str(source),
            "selected": str(prepared),
            "prompt_path": str(prompt_path),
            "report_path": str(report_path),
            "manifest_path": str(manifest_path),
            "action": "prepared",
        }
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "input_directory": str(source.parent),
                    "output_directory": str(output_root),
                    "snapshot_jsonl_count": 1,
                    "prepared_traces": [entry],
                    "errors": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "source_format": source_format,
                    "converted": prepared.resolve() != source,
                    "artifact_dir": str(artifact_dir.resolve()),
                    "trace_path": str(prepared),
                    "prompt_path": str(prompt_path),
                    "report_path": str(report_path),
                    "manifest_path": str(manifest_path),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
