"""Configuration loading for the standalone Judge pipeline."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - dependency validation
        tomllib = None  # type: ignore[assignment]


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "judge-model"


@dataclass(frozen=True)
class JudgeProfile:
    """Resolved model configuration for one Judge run."""

    name: str
    api_key: str
    base_url: str
    model: str
    temperature: float
    extra_body: dict[str, Any] | None
    request_timeout_s: float = 60.0
    max_retries: int = 5
    max_tokens: int | None = None
    inter_task_delay_s: float = 0.0


def load_env_file(path: Path | None, *, override: bool = False) -> None:
    """Load simple dotenv assignments without adding a runtime dependency.

    Args:
        path: `.env` file, or `None` to skip loading.
        override: Replace variables already present in the process.
    """
    if path is None or not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value


def _load_profiles(path: Path) -> dict[str, dict[str, Any]]:
    """Read named Judge profiles from TOML.

    Args:
        path: Profile TOML path.

    Returns:
        Mapping of profile name to configuration.

    Raises:
        FileNotFoundError: If the profile file does not exist.
        ValueError: If the TOML profile table is malformed.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Judge profile file not found: {path}")
    if tomllib is None:
        raise ModuleNotFoundError(
            "Python 3.10 requires the 'tomli' package to read Judge profiles. "
            "Install requirements.txt."
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError(f"Missing [profiles.*] sections in {path}")
    return {
        str(name): value
        for name, value in profiles.items()
        if isinstance(value, dict)
    }


def list_profiles(*, profiles_path: Path) -> list[dict[str, object]]:
    """Return a safe, key-free summary of all configured Judge profiles."""
    profiles = _load_profiles(profiles_path)
    rows: list[dict[str, object]] = []
    for name in sorted(profiles):
        value = profiles[name]
        api_key_env = str(
            value.get("api_key_env") or "JUDGE_AGENT_API_KEY"
        )
        rows.append(
            {
                "name": name,
                "model": str(value.get("model") or DEFAULT_MODEL),
                "baseUrl": str(value.get("base_url") or DEFAULT_BASE_URL),
                "apiKeyEnv": api_key_env,
                "configured": bool(os.environ.get(api_key_env)),
            }
        )
    return rows


def resolve_profile(
    *,
    name: str,
    profiles_path: Path,
) -> JudgeProfile:
    """Resolve a named model profile and its API key.

    Args:
        name: Profile name.
        profiles_path: TOML profile file.

    Returns:
        Fully resolved Judge configuration.

    Raises:
        KeyError: If the profile or its API-key environment variable is absent.
        ValueError: If configuration values are invalid.
    """
    profiles = _load_profiles(profiles_path)
    if name not in profiles:
        choices = ", ".join(sorted(profiles))
        raise KeyError(f"Unknown Judge profile {name!r}; available: {choices}")
    value = profiles[name]
    api_key_env = str(value.get("api_key_env") or "JUDGE_AGENT_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise KeyError(
            f"Environment variable {api_key_env!r} required by profile {name!r} "
            "is not set"
        )
    base_url = str(value.get("base_url") or DEFAULT_BASE_URL)
    model = str(value.get("model") or DEFAULT_MODEL)
    try:
        temperature = float(value.get("temperature", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Profile {name!r} has an invalid temperature"
        ) from exc
    extra_body_value = value.get("extra_body_json")
    extra_body: dict[str, Any] | None = None
    if isinstance(extra_body_value, str) and extra_body_value.strip():
        parsed = json.loads(extra_body_value)
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Profile {name!r} extra_body_json must decode to an object"
            )
        extra_body = parsed
    try:
        request_timeout_s = float(value.get("request_timeout_s", 60.0))
        max_retries = int(value.get("max_retries", 5))
        inter_task_delay_s = float(value.get("inter_task_delay_s", 0.0))
        raw_max_tokens = value.get("max_tokens")
        max_tokens = (
            int(raw_max_tokens)
            if raw_max_tokens is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Profile {name!r} has invalid request-control values"
        ) from exc
    if request_timeout_s <= 0:
        raise ValueError(
            f"Profile {name!r} request_timeout_s must be greater than zero"
        )
    if max_retries < 0:
        raise ValueError(
            f"Profile {name!r} max_retries cannot be negative"
        )
    if inter_task_delay_s < 0:
        raise ValueError(
            f"Profile {name!r} inter_task_delay_s cannot be negative"
        )
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError(
            f"Profile {name!r} max_tokens must be greater than zero"
        )
    return JudgeProfile(
        name=name,
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        extra_body=extra_body,
        request_timeout_s=request_timeout_s,
        max_retries=max_retries,
        max_tokens=max_tokens,
        inter_task_delay_s=inter_task_delay_s,
    )


def apply_profile(profile: JudgeProfile) -> None:
    """Expose a resolved profile to the copied Judge model helpers.

    Args:
        profile: Resolved Judge profile.
    """
    os.environ["JUDGE_AGENT_API_KEY"] = profile.api_key
    os.environ["JUDGE_AGENT_API_BASE"] = profile.base_url
    os.environ["JUDGE_AGENT_MODEL"] = profile.model
    os.environ["STANDALONE_JUDGE_TEMPERATURE"] = str(profile.temperature)
    os.environ["STANDALONE_JUDGE_REQUEST_TIMEOUT_S"] = str(
        profile.request_timeout_s
    )
    os.environ["STANDALONE_JUDGE_MAX_RETRIES"] = str(profile.max_retries)
    if profile.max_tokens is None:
        os.environ.pop("STANDALONE_JUDGE_MAX_TOKENS", None)
    else:
        os.environ["STANDALONE_JUDGE_MAX_TOKENS"] = str(profile.max_tokens)
    if profile.extra_body is None:
        os.environ.pop("STANDALONE_JUDGE_EXTRA_BODY", None)
    else:
        os.environ["STANDALONE_JUDGE_EXTRA_BODY"] = json.dumps(
            profile.extra_body,
            ensure_ascii=False,
        )


def get_judge_key() -> str:
    """Return the configured Judge API key."""
    key = os.environ.get("JUDGE_AGENT_API_KEY") or os.environ.get("JUDGE_API_KEY")
    if not key:
        raise ValueError("JUDGE_AGENT_API_KEY is not configured")
    return key


def get_judge_base_url() -> str:
    """Return the configured Judge API base URL."""
    return (
        os.environ.get("JUDGE_AGENT_API_BASE")
        or os.environ.get("JUDGE_BASE_URL")
        or DEFAULT_BASE_URL
    )


def get_judge_model() -> str:
    """Return the configured Judge model."""
    return (
        os.environ.get("JUDGE_AGENT_MODEL")
        or os.environ.get("JUDGE_MODEL")
        or DEFAULT_MODEL
    )


def get_judge_temperature() -> float:
    """Return the provider-specific Judge temperature."""
    try:
        return float(os.environ.get("STANDALONE_JUDGE_TEMPERATURE", "0"))
    except ValueError:
        return 0.0


def get_judge_request_timeout_s() -> float:
    """Return the maximum time to wait for one model response."""
    try:
        return float(
            os.environ.get("STANDALONE_JUDGE_REQUEST_TIMEOUT_S", "60")
        )
    except ValueError:
        return 60.0


def get_judge_max_retries() -> int:
    """Return the HTTP client's automatic retry count."""
    try:
        return max(
            0,
            int(os.environ.get("STANDALONE_JUDGE_MAX_RETRIES", "5")),
        )
    except ValueError:
        return 5


def get_judge_max_tokens() -> int | None:
    """Return the configured maximum Judge response length."""
    raw = os.environ.get("STANDALONE_JUDGE_MAX_TOKENS")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def get_judge_extra_body() -> dict[str, Any] | None:
    """Return provider-specific request fields."""
    raw = os.environ.get("STANDALONE_JUDGE_EXTRA_BODY")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("STANDALONE_JUDGE_EXTRA_BODY is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("STANDALONE_JUDGE_EXTRA_BODY must be a JSON object")
    return value
