"""Locate Workspace-Bench task directories without machine-specific paths."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from batch_runner import TaskSpec, load_task_spec, parse_tasks


class TaskLocationError(ValueError):
    """Raised when task metadata is missing or ambiguous."""


@dataclass(frozen=True)
class LocatedTasks:
    """Resolved Task specs and the source used to locate them."""

    specs: tuple[TaskSpec, ...]
    source: str


def _metadata_paths_at(location: Path) -> list[Path]:
    path = location.expanduser().resolve()
    if path.is_file():
        return [path] if path.name.casefold() == "metadata.json" else []
    if not path.is_dir():
        return []
    direct = path / "metadata.json"
    if direct.is_file():
        return [direct.resolve()]
    return sorted(
        (candidate.resolve() for candidate in path.glob("*/metadata.json")),
        key=lambda candidate: candidate.as_posix().casefold(),
    )


def discover_workspace_metadata(workspace: Path) -> list[Path]:
    """Discover conventional Task layouts directly below an Agent workspace."""
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise TaskLocationError(f"Agent 工作目录不存在：{root}")
    candidates: list[Path] = []
    direct = root / "metadata.json"
    if direct.is_file():
        candidates.append(direct.resolve())
    for location in (root / "task", root / "tasks"):
        candidates.extend(_metadata_paths_at(location))
    candidates.extend(
        candidate.resolve()
        for candidate in root.glob("*/metadata.json")
        if candidate.parent.name.isdigit()
    )
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(str(candidate).casefold(), candidate)
    return sorted(unique.values(), key=lambda path: path.as_posix().casefold())


def _load_specs(
    paths: Iterable[Path],
    *,
    min_task: int,
    max_task: int,
) -> list[TaskSpec]:
    specs: list[TaskSpec] = []
    seen: dict[int, Path] = {}
    for path in paths:
        spec = load_task_spec(path, min_task=min_task, max_task=max_task)
        previous = seen.get(spec.task_id)
        if previous is not None and previous != spec.metadata_path:
            raise TaskLocationError(
                f"Task {spec.task_id} 对应多个 metadata.json：{previous}；"
                f"{spec.metadata_path}"
            )
        if previous is None:
            seen[spec.task_id] = spec.metadata_path
            specs.append(spec)
    return specs


def _candidate_specs(
    locations: Iterable[Path],
    *,
    min_task: int,
    max_task: int,
) -> list[TaskSpec]:
    paths: list[Path] = []
    for location in locations:
        paths.extend(_metadata_paths_at(location))
    return _load_specs(paths, min_task=min_task, max_task=max_task)


def _select_ids(
    ids: Sequence[int],
    sources: Sequence[tuple[str, Sequence[TaskSpec]]],
    *,
    configured_root: Path | None,
    min_task: int,
    max_task: int,
) -> LocatedTasks:
    selected: list[TaskSpec] = []
    used_sources: list[str] = []
    for task_id in ids:
        matches: list[tuple[str, TaskSpec]] = []
        for source_name, specs in sources:
            matches.extend(
                (source_name, spec) for spec in specs if spec.task_id == task_id
            )
            if matches:
                break
        if not matches and configured_root is not None:
            metadata = configured_root / str(task_id) / "metadata.json"
            if metadata.is_file():
                matches.append(
                    (
                        "config",
                        load_task_spec(
                            metadata,
                            min_task=min_task,
                            max_task=max_task,
                        ),
                    )
                )
        if not matches:
            raise TaskLocationError(
                f"找不到 Task {task_id} 的 metadata.json。请告诉我 Task 目录，"
                "或把它放在当前工作目录的 task/ 下。"
            )
        if len(matches) > 1:
            paths = "；".join(str(spec.metadata_path) for _, spec in matches)
            raise TaskLocationError(f"Task {task_id} 路径不唯一：{paths}")
        source_name, spec = matches[0]
        selected.append(spec)
        used_sources.append(source_name)
    source = used_sources[0] if len(set(used_sources)) == 1 else "mixed"
    return LocatedTasks(specs=tuple(selected), source=source)


def resolve_task_specs(
    selectors: Sequence[str],
    *,
    workspace: Path,
    explicit_locations: Sequence[Path] = (),
    configured_root: Path | None = None,
    min_task: int = 1,
    max_task: int = 388,
    allow_many_without_selectors: bool = False,
) -> LocatedTasks:
    """Resolve paths by explicit input, workspace-relative discovery, then config."""
    root = workspace.expanduser().resolve()
    direct_paths: list[Path] = []
    numeric_selectors: list[str] = []
    for selector in selectors:
        candidate = Path(selector).expanduser()
        workspace_candidate = candidate if candidate.is_absolute() else root / candidate
        if workspace_candidate.exists() or selector.lower().endswith("metadata.json"):
            direct_paths.append(workspace_candidate)
        else:
            numeric_selectors.append(selector)

    direct_specs = _candidate_specs(
        direct_paths,
        min_task=min_task,
        max_task=max_task,
    )
    explicit_specs = _candidate_specs(
        explicit_locations,
        min_task=min_task,
        max_task=max_task,
    )
    excluded_metadata = {
        spec.metadata_path for spec in (*direct_specs, *explicit_specs)
    }
    workspace_specs = _load_specs(
        (
            path
            for path in discover_workspace_metadata(root)
            if path not in excluded_metadata
        ),
        min_task=min_task,
        max_task=max_task,
    )

    requested_ids = (
        parse_tasks(
            numeric_selectors,
            min_task=min_task,
            max_task=max_task,
        )
        if numeric_selectors
        else []
    )
    if requested_ids:
        located = _select_ids(
            requested_ids,
            (
                ("explicit", explicit_specs),
                ("workspace", workspace_specs),
            ),
            configured_root=configured_root,
            min_task=min_task,
            max_task=max_task,
        )
        combined = [*direct_specs, *located.specs]
        return LocatedTasks(
            specs=tuple(
                _load_specs(
                    (spec.metadata_path for spec in combined),
                    min_task=min_task,
                    max_task=max_task,
                )
            ),
            source=located.source if not direct_specs else "mixed",
        )

    if direct_specs:
        return LocatedTasks(specs=tuple(direct_specs), source="explicit")

    candidates = explicit_specs if explicit_specs else workspace_specs
    source = "explicit" if explicit_specs else "workspace"
    if not candidates and configured_root is not None:
        candidates = _candidate_specs(
            (configured_root,),
            min_task=min_task,
            max_task=max_task,
        )
        source = "config"
    if not candidates:
        raise TaskLocationError(
            "没有找到 Task 目录。请告诉我 Task 目录，或在当前工作目录下提供 "
            "task/metadata.json（批量时为 task/<ID>/metadata.json）。"
        )
    if len(candidates) > 1 and not allow_many_without_selectors:
        paths = "\n  - " + "\n  - ".join(
            str(spec.metadata_path) for spec in candidates
        )
        raise TaskLocationError(
            "当前工作目录中发现多个 Task，请指定 Task ID 或明确目录：" + paths
        )
    return LocatedTasks(specs=tuple(candidates), source=source)
