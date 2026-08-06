"""Judge LLM instance — LangChain ChatOpenAI for the evaluator.

Provides robust connection config (timeout, retries) for the judge,
similar to the inner agent's ChatOpenAI setup, plus a drop-in
``judge_chat_completion`` that mirrors a raw OpenAI chat call.
"""

import logging
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from standalone_judge.config import (
    get_judge_base_url,
    get_judge_extra_body,
    get_judge_key,
    get_judge_max_retries,
    get_judge_max_tokens,
    get_judge_model,
    get_judge_request_timeout_s,
    get_judge_temperature,
)

logger = logging.getLogger(__name__)
Json = Any


def build_judge_llm(
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
) -> ChatOpenAI:
    """Build and return a ChatOpenAI instance for judge (evaluator)."""
    model = model or get_judge_model()
    api_key = api_key or get_judge_key()
    base_url = base_url or get_judge_base_url()
    temperature = (
        get_judge_temperature()
        if temperature is None
        else temperature
    )

    logger.info("Building judge LLM: model=%s, base_url=%s", model, base_url)

    _http_client = httpx.Client(http2=False)

    kwargs: dict[str, Json] = {}
    extra_body = get_judge_extra_body()
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    max_tokens = get_judge_max_tokens()
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    request_timeout_s = get_judge_request_timeout_s()

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(
            connect=10.0,
            read=request_timeout_s,
            write=max(60.0, min(request_timeout_s, 120.0)),
            pool=10.0,
        ),
        max_retries=get_judge_max_retries(),
        streaming=False,
        temperature=temperature,
        http_client=_http_client,
        http_socket_options=(),
        **kwargs,
    )


def judge_chat_completion(
    messages: list[dict[str, Json]],
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[dict[str, Json] | None, dict[str, Json] | None, str]:
    """Drop-in replacement for raw OpenAI chat completions.

    Returns:
        (full_response_dict, usage_dict, assistant_content_str)
        On error: (None, None, error_message_str)
    """
    model = model or get_judge_model()
    api_key = api_key or get_judge_key()
    base_url = base_url or get_judge_base_url()

    llm = build_judge_llm(model=model, api_key=api_key, base_url=base_url)

    lc_messages = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "system":
            lc_messages.append(SystemMessage(content=content))
        else:
            lc_messages.append(HumanMessage(content=content))

    try:
        response = llm.invoke(lc_messages)
        content = str(response.content) if response.content else ""

        full_response = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ]
        }

        usage = {}
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            usage = {
                "prompt_tokens": um.get("input_tokens", 0),
                "completion_tokens": um.get("output_tokens", 0),
                "total_tokens": um.get("total_tokens", 0),
            }

        return full_response, usage, content

    except Exception as exc:
        err_msg = f"Judge LLM error: {type(exc).__name__}: {exc}"
        logger.error(err_msg)
        return None, None, err_msg
