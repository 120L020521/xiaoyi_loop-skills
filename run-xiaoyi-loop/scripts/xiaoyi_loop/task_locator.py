"""Locate XiaoYi Task datasets without machine-specific paths."""

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


def _is_exact_task_location(location: Path) -> bool:
    """Return whether a user supplied one concrete Task rather than a dataset root."""
    path = location.expanduser().resolve()
    if path.is_file():
        return path.name.casefold() == "metadata.json"
    return path.is_dir() and (path / "metadata.json").is_file()


def discover_workspace_metadata(workspace: Path) -> list[Path]:
    """Discover Task metadata in generic task-named workspace datasets."""
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise TaskLocationError(f"Agent 工作目录不存在：{root}")
    candidates: list[Path] = []
    direct = root / "metadata.json"
    if direct.is_file():
        candidates.append(direct.resolve())
    try:
        task_roots = sorted(
            (
                location
                for location in root.iterdir()
                if location.is_dir() and "task" in location.name.casefold()
            ),
            key=lambda location: location.name.casefold(),
        )
    except OSError as exc:
        raise TaskLocationError(f"无法扫描 Agent 工作目录：{root}：{exc}") from exc
    for location in task_roots:
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
    allow_duplicate_ids: bool = False,
) -> list[TaskSpec]:
    specs: list[TaskSpec] = []
    seen: dict[int, Path] = {}
    for path in paths:
        spec = load_task_spec(path)
        previous = seen.get(spec.task_id)
        if previous is not None and previous != spec.metadata_path:
            if not allow_duplicate_ids:
                raise TaskLocationError(
                    f"Task {spec.task_id} 对应多个 metadata.json：{previous}；"
                    f"{spec.metadata_path}。请明确指定数据集目录。"
                )
            specs.append(spec)
            continue
        if previous is None:
            seen[spec.task_id] = spec.metadata_path
            specs.append(spec)
    return specs


def _candidate_specs(
    locations: Iterable[Path],
    *,
    requested_ids: Sequence[int] = (),
) -> list[TaskSpec]:
    paths: list[Path] = []
    for location in locations:
        resolved = location.expanduser().resolve()
        direct = resolved / "metadata.json" if resolved.is_dir() else None
        if requested_ids and resolved.is_dir() and not (
            direct is not None and direct.is_file()
        ):
            paths.extend(
                metadata.resolve()
                for task_id in requested_ids
                if (metadata := resolved / str(task_id) / "metadata.json").is_file()
            )
        else:
            paths.extend(_metadata_paths_at(resolved))
    return _load_specs(paths)


def _select_ids(
    ids: Sequence[int],
    sources: Sequence[tuple[str, Sequence[TaskSpec]]],
    *,
    configured_root: Path | None,
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
                        load_task_spec(metadata),
                    )
                )
        if not matches:
            raise TaskLocationError(
                f"找不到 Task {task_id} 的 metadata.json。请告诉我 Task 目录，"
                "或把它放在当前工作目录下名称包含 task 的数据集目录中。"
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

    requested_ids = parse_tasks(numeric_selectors) if numeric_selectors else []
    direct_specs = _candidate_specs(
        direct_paths,
        requested_ids=requested_ids,
    )
    explicit_specs = _candidate_specs(
        explicit_locations,
        requested_ids=requested_ids,
    )

    # A concrete metadata path or Task directory is authoritative. Avoid scanning
    # unrelated datasets, which may legitimately reuse the same integer IDs.
    if direct_specs and not requested_ids:
        return LocatedTasks(specs=tuple(direct_specs), source="explicit")

    # Repeated --task-dir values that each point to one concrete Task are fully
    # specified and need no redundant numeric selectors.
    if (
        explicit_specs
        and not requested_ids
        and all(_is_exact_task_location(path) for path in explicit_locations)
    ):
        return LocatedTasks(specs=tuple(explicit_specs), source="explicit")

    workspace_specs: list[TaskSpec] = []
    if not explicit_locations:
        excluded_metadata = {
            spec.metadata_path for spec in (*direct_specs, *explicit_specs)
        }
        workspace_paths = (
            path
            for path in discover_workspace_metadata(root)
            if path not in excluded_metadata
            and (
                not requested_ids
                or not path.parent.name.isdigit()
                or int(path.parent.name) in requested_ids
            )
        )
        workspace_specs = _load_specs(
            workspace_paths,
            allow_duplicate_ids=True,
        )

    if requested_ids:
        selection_sources: list[tuple[str, Sequence[TaskSpec]]] = [
            ("explicit", direct_specs)
        ]
        selection_sources.extend(
            (("explicit", explicit_specs),)
            if explicit_locations
            else (("workspace", workspace_specs),)
        )
        located = _select_ids(
            requested_ids,
            selection_sources,
            configured_root=None if explicit_locations else configured_root,
        )
        combined = [*direct_specs, *located.specs]
        return LocatedTasks(
            specs=tuple(
                _load_specs(
                    (spec.metadata_path for spec in combined),
                )
            ),
            source=located.source if not direct_specs else "mixed",
        )

    candidates = explicit_specs if explicit_specs else workspace_specs
    source = "explicit" if explicit_specs else "workspace"
    if not candidates and configured_root is not None and not explicit_locations:
        candidates = _candidate_specs((configured_root,))
        source = "config"
    if not candidates:
        if explicit_locations:
            supplied = "；".join(str(path) for path in explicit_locations)
            raise TaskLocationError(
                "显式 Task 路径没有解析到 metadata.json："
                f"{supplied}。若提供的是数据集根目录，请同时传入 Task ID；"
                "若用户已指定目录和 ID，请传入精确的 <数据集>/<ID> 目录。"
            )
        raise TaskLocationError(
            "没有找到 Task 目录。请告诉我 Task 目录，或在当前工作目录下提供 "
            "名称包含 task 的数据集目录，例如 task/<ID>/metadata.json 或 "
            "filestask/<ID>/metadata.json。"
        )
    if len(candidates) > 1 and not allow_many_without_selectors:
        paths = "\n  - " + "\n  - ".join(
            str(spec.metadata_path) for spec in candidates
        )
        if explicit_locations:
            raise TaskLocationError(
                "--task-dir 指向的数据集包含多个 Task。请把用户给出的 ID 作为 "
                "selector，或把每个“目录 + ID”组合成精确的 <数据集>/<ID> "
                "Task 目录，并在一次 Runner 调用中全部传入：" + paths
            )
        raise TaskLocationError(
            "当前工作目录中发现多个 Task，请指定 Task ID 或明确目录：" + paths
        )
    return LocatedTasks(
        specs=tuple(
            _load_specs(spec.metadata_path for spec in candidates)
        ),
        source=source,
    )
