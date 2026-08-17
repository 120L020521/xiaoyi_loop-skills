"""LangChain bridge for the native workspace-bench judge.

The native ``agent_eval`` calls a urllib-based ``_chat_completions`` to talk to
the judge model. This module swaps that out for a LangChain ChatOpenAI call
(via :func:`judge_agent.judge_model.judge_chat_completion`), which gives the
judge robust timeouts/retries and Kimi image handling.

Use :func:`enable_langchain_judge` / :func:`disable_langchain_judge` to install
and restore the patch around a judge run.
"""

import logging
from typing import Any

from standalone_judge.judge_core.wb_eval import agent_eval

logger = logging.getLogger(__name__)
Json = Any

# judge_chat_completion is only needed when the native judge is available.
judge_chat_completion = None
if agent_eval is not None:
    try:
        from standalone_judge.judge_core.judge_model import (
            judge_chat_completion as _judge_chat_completion,
        )

        judge_chat_completion = _judge_chat_completion
    except Exception as exc:  # pragma: no cover - import-time environment issue
        logger.warning("judge_chat_completion not available: %s", exc)

_original_chat_completions = getattr(agent_eval, "_chat_completions", None) if agent_eval else None


def _chat_completions_langchain(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Json]],
    timeout_s: int = 120,
    max_retries: int = 10,
    total_timeout_s: float = 200.0,
) -> tuple[dict[str, Json] | None, dict[str, Json] | None, str]:
    """Drop-in replacement for agent_eval._chat_completions using LangChain."""
    logger.debug("[Judge LangChain] model=%s, msgs=%d", model, len(messages))
    if judge_chat_completion is None:
        raise RuntimeError("judge_chat_completion not available")
    return judge_chat_completion(
        messages=messages,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def enable_langchain_judge() -> None:
    """Switch agent_eval to use LangChain-based judge LLM calls."""
    if agent_eval and _original_chat_completions is not None:
        agent_eval._chat_completions = _chat_completions_langchain
        logger.info("LangChain judge enabled")


def disable_langchain_judge() -> None:
    """Restore original urllib-based judge LLM calls."""
    if agent_eval and _original_chat_completions is not None:
        agent_eval._chat_completions = _original_chat_completions
        logger.info("Original urllib judge restored")
