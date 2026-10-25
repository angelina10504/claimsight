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

# Groq is reached through the OpenAI-compatible provider rather than the
# "groq/" one: CrewAI's LiteLLM path sends a cache_breakpoint field Groq
# rejects. The doubled "openai/openai/" is deliberate - the first part names
# the provider, the second is Groq's own model id.
#
# The default is the model this project was last run on, so nothing changes
# model without being asked to. gpt-oss-120b judges better and is the one to
# use for the evaluation; each model has its own daily allowance, so the
# smaller one is also the fallback when the larger is spent. Switch with
# CLAIMSIGHT_GROQ_MODEL in .env rather than by editing this file, and note
# that describe() puts the model in every run's log either way.
GROQ_MODEL = os.getenv("CLAIMSIGHT_GROQ_MODEL", "openai/openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("CLAIMSIGHT_GROQ_VISION_MODEL", "openai/qwen/qwen3.8-27b")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

PROFILES = {
    "groq": {
        "text": GROQ_MODEL,
        "vision": GROQ_VISION_MODEL,          # kept for the Phase-3 Damage Analyst
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
    if PROVIDER == "ollama":
        kwargs["base_url"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    elif PROVIDER == "groq":
        kwargs["base_url"] = GROQ_BASE_URL
        kwargs["api_key"] = os.getenv("GROQ_API_KEY")

    # Groq's free tier limits tokens per minute, not just requests, so a busy
    # claim meets a 429 that clears within the minute. The OpenAI client the
    # provider uses already reads the Retry-After header the response carries
    # and backs off with jitter, so the fix is to give it enough attempts to
    # ride out that window rather than to write another backoff loop. Two
    # attempts was not enough; five covers a per-minute window.
    kwargs["max_retries"] = int(os.getenv("CLAIMSIGHT_LLM_RETRIES", "5"))
    kwargs["timeout"] = float(os.getenv("CLAIMSIGHT_LLM_TIMEOUT", "180"))
    try:
        return LLM(**kwargs)
    except TypeError:
        # A provider that does not take these (a local one, say) still works.
        kwargs.pop("max_retries", None)
        kwargs.pop("timeout", None)
        return LLM(**kwargs)


def describe() -> str:
    return (f"provider={PROVIDER} text={PROFILES[PROVIDER]['text']} "
            f"retries={os.getenv('CLAIMSIGHT_LLM_RETRIES', '5')} "
            f"rpm={os.getenv('CLAIMSIGHT_MAX_RPM', '8')} "
            f"knowledge={knowledge_limits()['results_limit']} passages")


def knowledge_limits() -> dict:
    """How much policy text to put in front of the Policy Analyst.

    CrewAI retrieves 5 passages by default. At 1,200 characters each that is
    about 1,500 tokens of policy wording, and an agent's loop re-sends its
    whole prompt on every turn, so it is paid for several times over. Three
    passages is the default here; raise it with CLAIMSIGHT_KNOWLEDGE_RESULTS
    if a policy question starts being missed, and confirm either way with
    test_policy_knowledge.py, which costs no provider tokens."""
    return {"results_limit": int(os.getenv("CLAIMSIGHT_KNOWLEDGE_RESULTS", "3")),
            "score_threshold": float(os.getenv("CLAIMSIGHT_KNOWLEDGE_THRESHOLD", "0.6"))}


def max_rpm() -> int:
    """Requests per minute allowed to the provider.

    The two analysts run in parallel on purpose, which is what makes a claim
    fast, but on a free tier two large requests in the same second can exceed
    the per-minute token allowance on their own. CrewAI's limiter spaces the
    requests out instead of making the work sequential."""
    return int(os.getenv("CLAIMSIGHT_MAX_RPM", "8"))
