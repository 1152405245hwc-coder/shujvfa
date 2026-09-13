"""跨层共享的小工具。"""

from __future__ import annotations


def mask_account(value: str | None) -> str | None:
    if not value:
        return value
    return "*" * max(len(value) - 4, 0) + value[-4:]
