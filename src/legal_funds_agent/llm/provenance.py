"""模型调用溯源：让「这次结论是谁产出的」变成可查证的事实。

为什么需要这个
--------------
``deepseek-chat`` 之类的模型名是**浮动别名**，指向服务商当前服务的版本。
实测发现：提示词逐字节未变、同一请求连跑两次 token 计数稳定，但输入 token 数
与 2026-08-28 记录的冻结基线仍相差 3/案，而输出 token 完全一致——只可能是
服务端模型或分词器发生了漂移。

对一个以「可复现、可举证」为立身之本的证据审查工具，这意味着：
**基线里的结论未必是现在这个模型产出的。** 所以每次真实调用都要留下溯源信息，
而不是只记一个模型名。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

from legal_funds_agent.llm.schemas import SCHEMAS, get_schema

# 浮动别名：不保证跨日期可复现。若服务商提供带日期的快照模型名，应优先使用。
FLOATING_MODEL_ALIASES = {
    "deepseek-chat",
    "deepseek-reasoner",
    "gpt-4o",
}

REPRODUCIBILITY_NOTE = (
    "模型名为浮动别名时，服务端版本可能随时变更；"
    "本记录仅对该次调用时点有效，不能用于证明历史结论可复现。"
)


def prompt_fingerprint(schema_name: str) -> str:
    """SHA-256 of the exact system prompt sent for a contract."""
    return hashlib.sha256(get_schema(schema_name).system_prompt.encode("utf-8")).hexdigest()


def all_prompt_fingerprints() -> dict[str, str]:
    return {name: prompt_fingerprint(name) for name in SCHEMAS}


def _endpoint(provider: Any) -> dict[str, Any]:
    model = getattr(provider, "model", None) or getattr(provider, "name", None)
    base_url = getattr(provider, "base_url", None)
    return {
        "provider": getattr(provider, "name", None),
        "model": model,
        "base_url": base_url,
        "floating_alias": model in FLOATING_MODEL_ALIASES,
        "declared_schemas": list(getattr(provider, "supported_schemas", ()) or ()),
        "offline": base_url is None,
    }


def build_provenance(providers: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Describe every provider involved in a run, keyed by the role it played."""
    endpoints: dict[str, Any] = {}
    for role, provider in providers:
        if provider is None:
            continue
        endpoints[role] = _endpoint(provider)
    has_remote = any(not item["offline"] for item in endpoints.values())
    return {
        # Offline providers are deterministic, so no timestamp is stamped: that keeps
        # repeat-run reports byte-identical, which the Mock baseline test relies on.
        "captured_at": datetime.now(timezone.utc).isoformat() if has_remote else None,
        "roles": endpoints,
        "prompt_fingerprints": all_prompt_fingerprints(),
        "remote_calls": has_remote,
        "note": REPRODUCIBILITY_NOTE if has_remote else "全部为离线 provider，无外部可复现性风险。",
    }
