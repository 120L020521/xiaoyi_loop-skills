"""Minimal OpenAI-compatible chat.completions client (stdlib only).

- HTTP via urllib; no third-party deps.
- Supports ``tools`` (function tools), parallel ``tool_calls`` responses,
  ``temperature`` and ``max_tokens``.
- Exponential backoff retry on HTTP 429 and 5xx (and transient network errors).
- Mock mode: pass ``mock_script=[{content, tool_calls}, ...]``; each chat()
  call pops the next scripted response (thread-safe). Used for key-less demos
  and unit tests.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .models import ChatResult, ToolCall
from .prompts import COMPACTION_SYSTEM_PROMPT

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class LLMError(RuntimeError):
    """Raised when the chat completion call fails permanently."""


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        mock_script: Optional[list[dict[str, Any]]] = None,
        max_retries: int = 5,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        # Mock mode -----------------------------------------------------------
        self._mock_enabled = mock_script is not None
        self._mock_script: list[dict[str, Any]] = list(mock_script) if mock_script else []
        self._mock_lock = threading.Lock()
        self._mock_calls: list[dict[str, Any]] = []  # recorded requests (tests)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return self._mock_enabled

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        """One chat.completions round. Returns a normalized ChatResult."""
        if self.is_mock:
            return self._mock_chat(messages, model, tools)

        if not self.api_key:
            raise LLMError(
                "No API key configured. Set OPENAI_API_KEY or pass api_key, "
                "or use mock mode."
            )

        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        payload = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}/chat/completions"

        last_err: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return self._parse_response(data)
            except urllib.error.HTTPError as e:
                last_err = e
                status = e.code
                # Read body for diagnostics (bounded).
                try:
                    err_body = e.read().decode("utf-8", "replace")[:2000]
                except Exception:
                    err_body = ""
                if status in (429,) or 500 <= status < 600:
                    self._sleep_backoff(attempt, e)
                    continue
                raise LLMError(f"HTTP {status} from chat completions API: {err_body}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                self._sleep_backoff(attempt, None)
                continue

        raise LLMError(f"chat completion failed after {self.max_retries + 1} attempts: {last_err}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _sleep_backoff(attempt: int, http_err: Optional[urllib.error.HTTPError]) -> None:
        # Honor Retry-After when present, else exponential backoff w/ jitter.
        delay: Optional[float] = None
        if http_err is not None and http_err.headers:
            ra = http_err.headers.get("Retry-After")
            if ra:
                try:
                    delay = float(ra)
                except ValueError:
                    delay = None
        if delay is None:
            delay = min(2.0 ** attempt, 30.0) * (0.5 + random.random())
        time.sleep(delay)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"chat completion returned no choices: {str(data)[:500]}")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{len(tool_calls)}",
                    name=fn.get("name") or "",
                    arguments_json=fn.get("arguments") or "{}",
                )
            )
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
        )

    # ------------------------------------------------------------------
    # Mock mode
    # ------------------------------------------------------------------

    def _mock_chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: Optional[list[dict[str, Any]]],
    ) -> ChatResult:
        with self._mock_lock:
            self._mock_calls.append(
                {"messages": messages, "model": model, "tools": tools}
            )
            # Compaction calls are a background maintenance detail: answer them
            # with a canned summary WITHOUT consuming the scripted queue, so a
            # mock script only has to script agent-visible turns.
            if self._is_compaction_request(messages):
                return ChatResult(
                    content="[mock summary: compacted conversation item]",
                    finish_reason="stop",
                    usage={"mock": True},
                )
            if not self._mock_script:
                # Mock mode with an exhausted/empty script: answer plainly so
                # loops can still terminate.
                return ChatResult(content="<final/>")
            step = self._mock_script.pop(0)

        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(step.get("tool_calls") or []):
            args = tc.get("arguments", {})
            if not isinstance(args, str):
                args = json.dumps(args)
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"mock_call_{len(self._mock_calls)}_{i}",
                    name=tc.get("name") or "",
                    arguments_json=args,
                )
            )
        return ChatResult(
            content=step.get("content") or "",
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage={"mock": True},
        )

    @staticmethod
    def _is_compaction_request(messages: list[dict[str, Any]]) -> bool:
        if not messages:
            return False
        first = messages[0]
        return (
            isinstance(first, dict)
            and first.get("role") == "system"
            and first.get("content") == COMPACTION_SYSTEM_PROMPT
        )

    # Test/introspection helper ------------------------------------------
    @property
    def mock_calls(self) -> list[dict[str, Any]]:
        with self._mock_lock:
            return list(self._mock_calls)
