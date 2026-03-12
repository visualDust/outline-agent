from __future__ import annotations

from typing import Any


def as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
