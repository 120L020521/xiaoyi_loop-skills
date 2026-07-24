"""Normalize timestamps and JSON content, with optional attribute limits."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import ConversionOptions


def normalized_key(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def cap_attribute(value: Any, max_chars: int) -> Any:
    if max_chars <= 0 or not isinstance(value, str) or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}... [HALO converter truncated: original {len(value)} chars]"


def jsonish(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def attribute_value(value: Any, options: ConversionOptions) -> str:
    return cap_attribute(jsonish(value), options.max_attribute_chars)


def source_attribute_value(value: Any, options: ConversionOptions) -> str:
    """Serialize source data reversibly when truncation is disabled."""
    return cap_attribute(jsonish(value), options.max_attribute_chars)


def source_context_value(
    row: dict[str, Any],
    mapped_payload: dict[str, str],
    options: ConversionOptions,
) -> str:
    """Keep source context and only payload fields not already stored by HALO."""
    payload = row.get("payload")
    context = {key: value for key, value in row.items() if key != "payload"}
    if not isinstance(payload, dict):
        context["payload"] = payload
        return source_attribute_value(context, options)

    if "*" in mapped_payload:
        context["payload_attribute_map"] = {"*": mapped_payload["*"]}
        return source_attribute_value(context, options)

    present_mappings = {
        key: attribute
        for key, attribute in mapped_payload.items()
        if key in payload
    }
    if present_mappings:
        context["payload_attribute_map"] = present_mappings
    remaining_payload = {
        key: value for key, value in payload.items() if key not in mapped_payload
    }
    if remaining_payload:
        context["payload"] = remaining_payload
    return source_attribute_value(context, options)


def halo_time(value: Any) -> str:
    """Normalize a supported timestamp to HALO's UTC string representation."""
    if value in (None, ""):
        raise ValueError("timestamp is missing")
    if isinstance(value, bool):
        raise ValueError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")

    text = str(value).strip()
    if text.isdigit():
        return halo_time(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # HALO commonly uses nanosecond-looking timestamps. Python datetime stores
    # microseconds, so trim excess fractional digits before ISO parsing.
    text = re.sub(
        r"(\.\d{6})\d+(?=(?:[+-]\d{2}:\d{2})?$)",
        r"\1",
        text,
    )
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
