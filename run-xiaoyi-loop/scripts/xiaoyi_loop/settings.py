"""Load machine-local settings for the XiaoYi pipeline.

The committed source tree contains no workstation paths or credentials. Skill
launchers keep optional local values outside the installed Skill, while command-line
arguments remain the highest-priority override.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping


_SCRIPT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """Raised when the local configuration is missing or malformed."""


@dataclass(frozen=True)
class LocalSettings:
    """Fully resolved workstation and runtime settings."""

    project_root: Path
    config_path: Path | None
    tasks_root: Path | None
    logs_dir: Path
    run_dir: Path
    state_file: Path
    hdc: str
    target: str | None
    user_id: str | None
    bundle_name: str
    ability_name: str
    remote_workspace_base: str
    poll_seconds: float
    timeout_seconds: int
    settle_seconds: float
    restart_delay_seconds: float
    tail_lines: int
    force_stop: bool
    stop_on_error: bool
    judge_profile: str | None
    judge_trace_mode: str
    judge_skip_existing: bool
    profiles_file: Path
    environment: Mapping[str, str]


_ROOT_KEYS = {"version", "paths", "device", "runner", "judge", "environment"}
_SECTION_KEYS = {
    "paths": {"tasks_root", "logs_dir", "run_dir", "state_file"},
    "device": {
        "hdc",
        "target",
        "user_id",
        "bundle_name",
        "ability_name",
        "remote_workspace_base",
    },
    "runner": {
        "poll_seconds",
        "timeout_seconds",
        "settle_seconds",
        "restart_delay_seconds",
        "tail_lines",
        "force_stop",
        "stop_on_error",
    },
    "judge": {"profile", "trace_mode", "skip_existing", "profiles_file"},
}


def default_config_path(project_root: Path) -> Path:
    """Return the conventional untracked workstation config path."""
    configured = os.environ.get("XIAOYI_LOOP_CONFIG", "").strip()
    if configured:
        return _resolve_path(configured, project_root=project_root)
    return (project_root / "config" / "local.toml").resolve()


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        if sys.version_info >= (3, 11):
            import tomllib  # type: ignore[import-not-found]
        else:
            try:
                import tomli as tomllib  # type: ignore[import-not-found]
            except ModuleNotFoundError as exc:
                raise ConfigError(
                    "Python 3.10 读取 TOML 需要 tomli；请先执行 "
                    "python -m pip install -e ."
                ) from exc
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
    except Exception as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"配置文件不是有效 TOML：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"配置文件顶层必须是 TOML table：{path}")
    return value


def _resolve_path(value: object, *, project_root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"路径必须是非空字符串，实际为 {value!r}")
    expanded = os.path.expandvars(value.strip())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _optional_path(value: object, *, project_root: Path) -> Path | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _resolve_path(value, project_root=project_root)


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} 必须是字符串")
    return value.strip()


def _text(value: object, *, name: str) -> str:
    result = _optional_text(value, name=name)
    if result is None:
        raise ConfigError(f"{name} 不能为空")
    return result


def _number(value: object, *, name: str, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 必须是数字")
    converted: int | float = int(value) if integer else float(value)
    if converted <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return converted


def _non_negative_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 必须是数字")
    converted = float(value)
    if converted < 0:
        raise ConfigError(f"{name} 不能小于 0")
    return converted


def _boolean(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} 必须是 true 或 false")
    return value


def _legacy_environment_boolean(name: str, *, default: bool) -> bool:
    value = os.environ.get(name, "").strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(
        f"旧版环境变量 {name} 必须是 true/false、1/0、yes/no 或 on/off"
    )


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是 TOML table")
    unknown = set(value) - _SECTION_KEYS[name]
    if unknown:
        raise ConfigError(f"[{name}] 包含未知配置项：{', '.join(sorted(unknown))}")
    return value


def _defaults(project_root: Path, *, use_legacy_environment: bool) -> LocalSettings:
    legacy_run_dir = (
        os.environ.get("JUDGE_RUN_DIR", "").strip()
        if use_legacy_environment
        else ""
    )
    run_dir = (
        _resolve_path(legacy_run_dir, project_root=project_root)
        if legacy_run_dir
        else (project_root / "xiaoyi_judge").resolve()
    )
    return LocalSettings(
        project_root=project_root,
        config_path=None,
        tasks_root=None,
        logs_dir=(project_root / "xiaoyi_logs").resolve(),
        run_dir=run_dir,
        state_file=(project_root / "pipeline_state.json").resolve(),
        hdc="hdc.exe" if os.name == "nt" else "hdc",
        target=None,
        user_id=None,
        bundle_name="com.huawei.hmos.vassistant",
        ability_name="PCAgentTaskAbility",
        remote_workspace_base=(
            "/data/app/el2/100/base/com.huawei.hmos.vassistant/files/taichu_data"
        ),
        poll_seconds=3.0,
        timeout_seconds=1800,
        settle_seconds=1.5,
        restart_delay_seconds=5.0,
        tail_lines=300,
        force_stop=True,
        stop_on_error=False,
        judge_profile=(
            os.environ.get("JUDGE_PROFILE", "").strip() or None
            if use_legacy_environment
            else None
        ),
        judge_trace_mode=(
            (
                os.environ.get("JUDGE_TRACE_MODE", "compact")
                if use_legacy_environment
                else "compact"
            ).strip().casefold()
            or "compact"
        ),
        judge_skip_existing=(
            _legacy_environment_boolean("JUDGE_SKIP_EXISTING", default=False)
            if use_legacy_environment
            else False
        ),
        profiles_file=(_SCRIPT_ROOT / "standalone_judge" / "judge_profiles.toml").resolve(),
        environment={},
    )


def load_local_settings(
    *,
    project_root: Path,
    config_path: Path | None = None,
    require_config: bool = False,
    discover_default_config: bool = True,
) -> LocalSettings:
    """Load and validate ``local.toml`` with project-relative path handling."""
    root = project_root.expanduser().resolve()
    selected = (
        config_path.expanduser().resolve()
        if config_path is not None
        else default_config_path(root) if discover_default_config else None
    )
    if selected is None:
        if require_config:
            raise ConfigError("未指定本机配置文件。")
        return _defaults(root, use_legacy_environment=False)

    settings = _defaults(root, use_legacy_environment=not selected.is_file())
    if not selected.is_file():
        if require_config or config_path is not None:
            raise ConfigError(
                f"本机配置不存在：{selected}。请从 Skill 的 "
                "config/local.example.toml 创建工作目录配置后填写。"
            )
        return settings

    data = _load_toml(selected)
    unknown_root = set(data) - _ROOT_KEYS
    if unknown_root:
        raise ConfigError(f"配置文件包含未知顶层项：{', '.join(sorted(unknown_root))}")
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"不支持的配置版本：{version!r}，当前仅支持 version = 1")

    paths = _section(data, "paths")
    device = _section(data, "device")
    runner = _section(data, "runner")
    judge = _section(data, "judge")
    environment_value = data.get("environment", {})
    if not isinstance(environment_value, dict):
        raise ConfigError("[environment] 必须是 TOML table")
    environment: dict[str, str] = {}
    for raw_key, raw_value in environment_value.items():
        key = str(raw_key).strip()
        if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
            raise ConfigError(f"无效的环境变量名：{raw_key!r}")
        if not isinstance(raw_value, (str, int, float, bool)):
            raise ConfigError(f"environment.{key} 必须是字符串、数字或布尔值")
        value = str(raw_value).strip()
        if value:
            environment[key] = value

    result = replace(settings, config_path=selected, environment=environment)
    if "tasks_root" in paths:
        result = replace(
            result,
            tasks_root=_optional_path(paths["tasks_root"], project_root=root),
        )
    for key in ("logs_dir", "run_dir", "state_file"):
        if key in paths:
            result = replace(result, **{key: _resolve_path(paths[key], project_root=root)})

    if "hdc" in device:
        hdc_value = _text(device["hdc"], name="device.hdc")
        if any(separator in hdc_value for separator in ("/", "\\")):
            hdc_value = str(_resolve_path(hdc_value, project_root=root))
        result = replace(result, hdc=hdc_value)
    for key in ("target", "user_id"):
        if key in device:
            result = replace(
                result,
                **{key: _optional_text(device[key], name=f"device.{key}")},
            )
    for key in ("bundle_name", "ability_name", "remote_workspace_base"):
        if key in device:
            result = replace(
                result,
                **{key: _text(device[key], name=f"device.{key}")},
            )

    runner_number_fields = {
        "poll_seconds": False,
        "timeout_seconds": True,
        "tail_lines": True,
    }
    for key, integer in runner_number_fields.items():
        if key in runner:
            result = replace(
                result,
                **{key: _number(runner[key], name=f"runner.{key}", integer=integer)},
            )
    for key in ("settle_seconds", "restart_delay_seconds"):
        if key in runner:
            result = replace(
                result,
                **{key: _non_negative_number(runner[key], name=f"runner.{key}")},
            )
    for key in ("force_stop", "stop_on_error"):
        if key in runner:
            result = replace(
                result,
                **{key: _boolean(runner[key], name=f"runner.{key}")},
            )

    if "profile" in judge:
        result = replace(
            result,
            judge_profile=_optional_text(judge["profile"], name="judge.profile"),
        )
    if "trace_mode" in judge:
        result = replace(
            result,
            judge_trace_mode=_text(judge["trace_mode"], name="judge.trace_mode").casefold(),
        )
    if "skip_existing" in judge:
        result = replace(
            result,
            judge_skip_existing=_boolean(
                judge["skip_existing"],
                name="judge.skip_existing",
            ),
        )
    if "profiles_file" in judge:
        result = replace(
            result,
            profiles_file=_resolve_path(judge["profiles_file"], project_root=root),
        )

    if result.judge_trace_mode not in {"compact", "full"}:
        raise ConfigError("judge.trace_mode 必须是 compact 或 full")
    return result


def apply_local_environment(settings: LocalSettings) -> None:
    """Apply non-empty local environment values without logging secrets."""
    for key, value in settings.environment.items():
        os.environ[key] = value
    os.environ["XIAOYI_HDC"] = settings.hdc
    os.environ["XIAOYI_BUNDLE_NAME"] = settings.bundle_name
    os.environ["XIAOYI_ABILITY_NAME"] = settings.ability_name
    os.environ["XIAOYI_REMOTE_WORKSPACE_BASE"] = settings.remote_workspace_base
