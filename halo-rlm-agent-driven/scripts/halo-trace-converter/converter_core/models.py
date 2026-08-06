"""Shared converter types and options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


Json = dict[str, Any]


@dataclass(frozen=True)
class ConversionOptions:
    """Optional output-size control for string attributes."""

    max_attribute_chars: int = 0
