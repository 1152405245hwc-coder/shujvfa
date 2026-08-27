from __future__ import annotations

from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str

    def generate_structured(self, *, text: str, schema_name: str) -> list[dict[str, Any]]:
        """Return schema-constrained data without performing legal conclusions."""

