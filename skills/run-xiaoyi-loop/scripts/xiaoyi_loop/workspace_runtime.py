"""Resolve writable XiaoYi runtime files outside the installed Skill."""

from __future__ import annotations

import os
from pathlib import Path


RUNTIME_DIR_NAME = ".xiaoyi-loop"


def workspace_runtime_dir(workspace: Path) -> Path:
    """Return the writable, workspace-local XiaoYi support directory."""
    return (workspace.expanduser().resolve() / RUNTIME_DIR_NAME).resolve()


def resolve_workspace_config(
    workspace: Path,
    *,
    explicit: Path | None = None,
) -> Path | None:
    """Resolve explicit/env config or discover ``.xiaoyi-loop/local.toml``."""
    root = workspace.expanduser().resolve()
    if explicit is not None:
        candidate = explicit.expanduser()
        return (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )

    configured = os.environ.get("XIAOYI_LOOP_CONFIG", "").strip()
    if configured:
        candidate = Path(os.path.expandvars(configured)).expanduser()
        return (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )

    candidate = workspace_runtime_dir(root) / "local.toml"
    return candidate if candidate.is_file() else None
