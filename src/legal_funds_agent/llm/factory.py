from __future__ import annotations

import os

from legal_funds_agent.llm.deepseek_provider import DeepSeekProvider
from legal_funds_agent.llm.mock_provider import MockProvider
from legal_funds_agent.llm.openai_provider import OpenAIProvider


def provider_from_environment(provider_name: str | None = None):
    name = (provider_name or os.getenv("LLM_PROVIDER", "mock")).lower()
    if name == "mock":
        return MockProvider()
    if name == "openai":
        return OpenAIProvider(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        )
    if name == "deepseek":
        return DeepSeekProvider(
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )
    raise ValueError(f"unsupported LLM_PROVIDER: {name}")
