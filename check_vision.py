"""
Find out which vision model to use, and what Groq actually said.

run_damage_analyst reports a failed vision call as one word so a bad photo
never stops a claim. That is right in the pipeline and useless when you are
trying to fix it, so this asks the provider directly:

  1. which models the account can see
  2. what the real error message is for one small vision call

It sends one tiny image (a 64x64 square), so it costs almost nothing.

  uv run python check_vision.py
"""

import base64
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import config                                     # noqa: E402

KEY = os.getenv("GROQ_API_KEY", "")


def list_models() -> None:
    import urllib.error
    import urllib.request

    print(f"1. Models this account can see  ({config.GROQ_BASE_URL})\n")
    if config.PROVIDER != "groq":
        print(f"   LLM_PROVIDER is '{config.PROVIDER}', not groq - skipping.\n")
        return
    if not KEY:
        print("   GROQ_API_KEY is not set in .env\n")
        return
    request = urllib.request.Request(
        f"{config.GROQ_BASE_URL.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            import json
            names = sorted(m["id"] for m in json.load(response).get("data", []))
    except urllib.error.HTTPError as exc:
        print(f"   HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}\n")
        return
    except Exception as exc:                        # noqa: BLE001
        print(f"   could not reach the model list: {exc}\n")
        return

    hints = ("vision", "vl", "scout", "maverick", "qwen", "llava", "gemma", "multimodal")
    likely = [n for n in names if any(h in n.lower() for h in hints)]
    print(f"   {len(names)} model(s). Ones that may accept images:")
    for name in likely or ["   (none matched the usual vision names)"]:
        print(f"     {name}")
    print("\n   All models:")
    for name in names:
        print(f"     {name}")
    configured = config.PROFILES[config.PROVIDER].get("vision", "")
    bare = configured.split("openai/", 1)[-1] if configured.startswith("openai/") else configured
    print(f"\n   Configured vision model : {configured}")
    print(f"   Groq would see it as    : {bare}")
    print(f"   On the account?         : {'yes' if bare in names else 'NO - this is the problem'}\n")


def try_one_call() -> None:
    print("2. One small vision call, with the provider's own message\n")
    try:
        import litellm
        from PIL import Image
    except ImportError as exc:
        print(f"   {exc}\n")
        return

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, format="JPEG")
    url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    kwargs = {"model": config.PROFILES[config.PROVIDER].get("vision"),
              "timeout": 60, "temperature": 0.0, "max_tokens": 20}
    if config.PROVIDER == "groq":
        kwargs["base_url"] = config.GROQ_BASE_URL
        kwargs["api_key"] = KEY
    elif config.PROVIDER == "ollama":
        kwargs["base_url"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    messages = [{"role": "user", "content": [
        {"type": "text", "text": "What colour is this image? One word."},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]
    try:
        response = litellm.completion(messages=messages, **kwargs)
        print(f"   WORKED. The model replied: {response.choices[0].message.content!r}")
        usage = getattr(response, "usage", None)
        if usage:
            print(f"   tokens: prompt {usage.prompt_tokens}, "
                  f"completion {usage.completion_tokens}")
    except Exception as exc:                        # noqa: BLE001
        print(f"   {type(exc).__name__}")
        message = str(exc)
        print(f"   {message[:900]}")
        lowered = message.lower()
        print()
        if "per day" in lowered or "rpd" in lowered or "tpd" in lowered:
            print("   -> the DAILY allowance for this model is spent. It resets at "
                  "midnight UTC (5:30am IST). Try a different model, or wait.")
        elif "per minute" in lowered or "tpm" in lowered or "rpm" in lowered:
            print("   -> a PER MINUTE limit. Wait a minute and run again; the image "
                  "may also be too large for this model's tokens-per-minute cap.")
        elif "does not exist" in lowered or "not found" in lowered or "decommission" in lowered:
            print("   -> the model id is wrong or retired. Pick one from the list "
                  "above and set CLAIMSIGHT_GROQ_VISION_MODEL in .env.")
    print()


if __name__ == "__main__":
    list_models()
    try_one_call()
    print("Set the one that works in .env, e.g.:")
    print("   CLAIMSIGHT_GROQ_VISION_MODEL=openai/<the groq model id>")
