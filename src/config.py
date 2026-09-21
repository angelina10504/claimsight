"""
Central LLM configuration.

The provider is a setting, not a hardcoded choice, so the same pipeline can be
run against a cloud model during development and a locally hosted model for
the privacy-preserving demonstration. Set LLM_PROVIDER in .env to one of:
groq | ollama | anthropic
"""

import os
from pathlib import Path

from crewai import LLM
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()

PROFILES = {
    "groq": {
        "text": "openai/openai/gpt-oss-120b",
        "vision": "openai/qwen/qwen3.8-27b",
    },
    "ollama": {
        "text": "ollama/llama3.2",
        "vision": "ollama/moondream",
    },
    "anthropic": {
        "text": "anthropic/claude-haiku-4-5-20251001",
        "vision": "anthropic/claude-haiku-4-5-20251001",
    },
}


def get_llm(kind: str = "text", temperature: float = 0.1) -> LLM:
    """Return the configured LLM. kind is 'text' or 'vision'."""
    if PROVIDER not in PROFILES:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{PROVIDER}'. "
            f"Choose one of: {', '.join(PROFILES)}"
        )
    model = PROFILES[PROVIDER].get(kind)
    if model is None:
        raise ValueError(f"Provider '{PROVIDER}' has no '{kind}' model configured.")

    kwargs = {"model": model, "temperature": temperature}
    if PROVIDER == "groq":
        kwargs["base_url"] = "https://api.groq.com/openai/v1"
        kwargs["api_key"] = os.getenv("GROQ_API_KEY")
    return LLM(**kwargs)


def describe() -> str:
    return f"provider={PROVIDER} text={PROFILES[PROVIDER]['text']}"
