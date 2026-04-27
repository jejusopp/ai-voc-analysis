from __future__ import annotations

from functools import lru_cache

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings


@lru_cache
def get_chat_model() -> BaseChatModel:
    s = get_settings()
    return build_chat_model(s)


def build_chat_model(settings: Settings) -> BaseChatModel:
    provider = settings.llm_provider.lower().strip()
    if provider == "claude":
        if not settings.anthropic_api_key:
            raise ValueError("LLM_PROVIDER=claude 인데 ANTHROPIC_API_KEY 가 비어 있습니다.")
        return ChatAnthropic(
            model=settings.claude_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
        )
    return ChatOpenAI(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        api_key="ollama",
        temperature=0,
    )
