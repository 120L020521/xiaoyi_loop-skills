"""Strict output collection for the native workspace-bench judge.

By default ``agent_eval._collect_outputs`` sweeps the whole work directory,
which can leak intermediate/source files into the judge's view. This module
provides a stricter collector that only surfaces:

  - everything under ``<task_dir>/output/``
  - files whose basename matches an ``output_files`` entry in metadata

Use :func:`enable_strict_outputs` / :func:`disable_strict_outputs` to install
and restore the patch around a judge run.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

from standalone_judge.judge_core.wb_eval import agent_eval

logger = logging.getLogger(__name__)
Json = Any

# _normalize_filename_key is part of the native judge; only available with it.
_normalize_filename_key = None
if agent_eval is not None:
    _normalize_filename_key = getattr(agent_eval, "_normalize_filename_key", None)

_original_collect_outputs = getattr(agent_eval, "_collect_outputs", None) if agent_eval else None


def _strict_collect_outputs(
    task_dir: str,
    meta: Dict[str, Json],
) -> Dict[str, Json]:
    """Strictly collect only runner output files for judging."""
    if agent_eval is None or _normalize_filename_key is None:
        raise RuntimeError("agent_eval not available for strict output collection")

    task_dir_path = Path(task_dir)
    output_dir = task_dir_path / "output"
    work_dir = task_dir_path

    expected_outputs: list[str] = []
    if isinstance(meta, dict):
        expected_outputs = [
            str(x)
            for x in meta.get("output_files", [])
            if isinstance(x, str) and x.strip()
        ]

    expected_keys = {
        _normalize_filename_key(name)
        for name in expected_outputs
        if name.strip()
    }

    files: list[Json] = []
    seen: set[str] = set()

    def _add_file(p: Path) -> None:
        resolved = str(p.resolve())
        if resolved in seen:
            return
        seen.add(resolved)
        try:
            excerpt, image_data_url, note = agent_eval._read_rich_excerpt(resolved)
            st = p.stat()
            rel = None
            try:
                rel = str(p.relative_to(work_dir))
            except ValueError:
                pass
            files.append(
                {
                    "path": resolved,
                    "relToWorkDir": rel,
                    "sizeBytes": int(st.st_size),
                    "excerpt": excerpt,
                    "mime": agent_eval._guess_mime(resolved),
                    "hasImage": bool(image_data_url),
                    "_imageDataUrl": image_data_url,
                    "note": note,
                }
            )
        except Exception:
            return

    if output_dir.exists() and output_dir.is_dir():
        for p in output_dir.rglob("*"):
            if p.is_file():
                _add_file(p)

    judge_artifacts_dir = work_dir / "judge_artifacts"
    if expected_keys and work_dir.exists():
        for p in work_dir.rglob("*"):
            if not p.is_file():
                continue
            if output_dir in p.parents or judge_artifacts_dir in p.parents:
                continue
            key = _normalize_filename_key(p.name)
            if key not in expected_keys:
                continue
            _add_file(p)

    def _sort_key(file_entry: Json) -> tuple[int, str]:
        path = file_entry.get("path") or ""
        name = os.path.basename(path)
        is_expected = _normalize_filename_key(name) in expected_keys
        return (0 if is_expected else 1, name.lower())

    files.sort(key=_sort_key)

    return {
        "workDir": str(work_dir),
        "expectedOutputs": expected_outputs,
        "files": files,
    }


def enable_strict_outputs() -> None:
    """Restrict output collection to runner outputs only."""
    if agent_eval and _original_collect_outputs is not None:
        agent_eval._collect_outputs = _strict_collect_outputs
        logger.info("Strict output collection enabled")


def disable_strict_outputs() -> None:
    """Restore original output collection."""
    if agent_eval and _original_collect_outputs is not None:
        agent_eval._collect_outputs = _original_collect_outputs
        logger.info("Original output collection restored")
