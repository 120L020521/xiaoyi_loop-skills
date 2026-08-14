"""Lazy access to workspace-bench's native ``agent_eval`` module.

The native judge lives in the workspace-bench repo (``evaluation/src``), which
is not always on ``PYTHONPATH``. This module performs the path insertion once
and exposes ``agent_eval`` (or ``None`` when unavailable), so the rest of
judge_agent can import it from a single place.
"""

import logging

logger = logging.getLogger(__name__)

agent_eval = None
try:
    from standalone_judge.vendor import agent_eval as _agent_eval

    agent_eval = _agent_eval
except Exception as exc:  # pragma: no cover - depends on runtime environment
    logger.warning("Native workspace-bench judge not available: %s", exc)


def require_agent_eval():
    """Return the native agent_eval module or raise if unavailable."""
    if agent_eval is None:
        raise RuntimeError(
            "Native workspace-bench judge not available. "
            "Install the dependencies listed in requirements.txt."
        )
    return agent_eval
