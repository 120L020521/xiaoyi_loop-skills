"""Resolve XiaoYi runtime artifacts relative to the Agent workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xiaoyi_loop.settings import LocalSettings


@dataclass(frozen=True)
class RuntimePaths:
    logs_dir: Path
    run_dir: Path
    state_file: Path


def _explicit_path(value: str | Path, *, workspace: Path) -> Path:
    candidate = Path(value).expanduser()
    return (
        candidate.resolve()
        if candidate.is_absolute()
        else (workspace / candidate).resolve()
    )


def _workspace_default(
    configured: Path,
    *,
    settings_root: Path,
    workspace: Path,
) -> Path:
    """Rebase Skill-relative defaults while preserving explicit absolute paths."""
    resolved = configured.expanduser().resolve()
    try:
        relative = resolved.relative_to(settings_root.expanduser().resolve())
    except ValueError:
        return resolved
    return (workspace / relative).resolve()


def resolve_runtime_paths(
    settings: LocalSettings,
    *,
    workspace: Path,
    logs_dir: str | Path | None = None,
    run_dir: str | Path | None = None,
    state_file: str | Path | None = None,
) -> RuntimePaths:
    """Return workspace-local defaults with explicit CLI paths taking priority."""
    root = workspace.expanduser().resolve()
    return RuntimePaths(
        logs_dir=(
            _explicit_path(logs_dir, workspace=root)
            if logs_dir is not None
            else _workspace_default(
                settings.logs_dir,
                settings_root=settings.project_root,
                workspace=root,
            )
        ),
        run_dir=(
            _explicit_path(run_dir, workspace=root)
            if run_dir is not None
            else _workspace_default(
                settings.run_dir,
                settings_root=settings.project_root,
                workspace=root,
            )
        ),
        state_file=(
            _explicit_path(state_file, workspace=root)
            if state_file is not None
            else _workspace_default(
                settings.state_file,
                settings_root=settings.project_root,
                workspace=root,
            )
        ),
    )
