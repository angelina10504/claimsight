"""
Damage Analyst vision: what a photograph of the vehicle actually shows.

Why this exists: the paperwork alone cannot catch a phantom part. A garage can
bill a door glass that was never broken, at a fair price, on a valid policy,
and every text tool will pass it. Only a photograph shows the part was intact.

Two rules carried over from the rest of ClaimSight:

  LLMs judge, code calculates.  The model gives one verdict on each of a fixed
  list of parts - damaged, undamaged, or not visible - and nothing else. It
  never sees the invoice, so it cannot be led by it, and it never decides
  anything. Comparing what it saw with what was billed is done in Python
  (tools/assessment_tools.py).

  "Not visible" is not "undamaged", exactly as an "uncertain" coverage status
  is not "excluded". A close-up of a bumper carries no cue for which end of the
  car it is; asked to free-list damaged parts, the model guessed "front", and
  the guess became a fraud flag on an honest claim. Only a part the model can
  see clearly and see intact counts as evidence against a billed line.

  Nothing personal reaches a cloud model.  A photograph of a vehicle carries
  the number plate, and often a phone number stamped across the image by the
  garage. Unlike invoice text, that cannot be cleaned by removing known values.
  So a photo is sent to a cloud model only when its origin says it is safe to
  (see photo_clearance); otherwise the claim stops, exactly as a document with
  surviving PII does.

The image is downscaled before it is sent: a 4000px photo costs many times the
tokens of a 1024px one and tells the model nothing more about which panel is
dented.
"""

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import List, Optional

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "data" / "photos"
LABELS = ROOT / "data" / "photo_labels.json"

# Longest side sent to the model. Images are charged at a high fixed rate -
# even a 64x64 square costs over a thousand tokens - and a large photo can
# exceed the provider's tokens-per-minute cap on its own. 768px is enough to
# tell a bumper from a door; raise it with CLAIMSIGHT_PHOTO_MAX_EDGE if a
# model keeps missing small parts.
MAX_EDGE = int(os.getenv("CLAIMSIGHT_PHOTO_MAX_EDGE", "768"))
JPEG_QUALITY = int(os.getenv("CLAIMSIGHT_PHOTO_QUALITY", "80"))

# A per-minute limit is a wait, not a failure: back off and try again.
VISION_ATTEMPTS = 3
VISION_BACKOFF = (20, 45)          # seconds before the 2nd and 3rd attempts

# The parts ClaimSight prices and settles. The model is held to this list so
# its answer can be compared with invoice lines without fuzzy matching.
PARTS = ["front bumper", "rear bumper", "headlight", "taillight", "side mirror",
         "grille", "bonnet", "front door", "rear door", "fender",
         "windshield", "door glass"]

DAMAGE_TYPES = ["scratch", "dent", "tear", "missing_part", "broken_light",
                "puncture", "broken_glass"]

VERDICTS = ("damaged", "undamaged", "not_visible")

PROMPT = (
    "You are a motor insurance damage assessor looking at one photograph of a "
    "damaged vehicle.\n\n"
    "Give a verdict on EVERY one of these 12 parts, using exactly these names:\n"
    f"{', '.join(PARTS)}\n\n"
    "Each verdict is one of:\n"
    "  damaged      you can see the part, and you can see it is damaged\n"
    "  undamaged    the part is clearly visible in the frame AND clearly intact\n"
    "  not_visible  everything else: out of frame, cropped, blurred, hidden, "
    "or you are not sure which part it is\n\n"
    "Rules:\n"
    "- Say undamaged only when you would stake your name on it. A part you "
    "cannot see properly is not_visible, never undamaged.\n"
    "- If you cannot tell whether a panel is at the front or the rear of the "
    "vehicle (front bumper or rear bumper, front door or rear door, headlight "
    "or taillight), put BOTH candidates in not_visible rather than guessing.\n"
    "- Report damage only on the part that carries it. Do not mark an adjacent "
    "panel damaged because it is near the damage; if you cannot see damage on "
    "it, it is undamaged or not_visible.\n"
    "- A part that is missing entirely counts as damaged.\n"
    "- If the photograph is too close, too dark or too unclear to judge "
    "anything, set readable to false and mark every part not_visible.\n\n"
    "Describe the damage using only these words:\n"
    f"{', '.join(DAMAGE_TYPES)}\n\n"
    "Answer with JSON only, no other text. \"verdicts\" must name all 12 parts:\n"
    '{"verdicts": {"front bumper": "damaged|undamaged|not_visible", ...}, '
    '"damage_types": [], "severity": "minor|moderate|severe", '
    '"readable": true, "notes": "one short line"}'
)


class PhotoUnsafeForCloud(RuntimeError):
    """Raised when a photo would be sent to a cloud model without clearance."""


# ---------------------------------------------------------------- privacy
def photo_clearance(name: str) -> dict:
    """May this photo be sent to a model outside this machine?

    A vehicle photograph cannot be sanitised the way invoice text can: the
    number plate is part of the picture. So clearance comes from where the
    photo came from, not from inspecting it.

      research dataset  the photo is one of the labelled dataset images. It is
                        already public, and no policyholder of ours is in it.
      claimant          a real claimant's photo. Not cleared: it needs plate
                        redaction, which is not built yet.

    CLAIMSIGHT_PHOTO_CONSENT=1 overrides this for a photo a person has
    confirmed is safe to send. It is deliberately not the default.
    """
    from_dataset = False
    if LABELS.exists():
        try:
            from_dataset = name in json.loads(LABELS.read_text())
        except (json.JSONDecodeError, OSError):
            from_dataset = False
    consented = os.getenv("CLAIMSIGHT_PHOTO_CONSENT", "").lower() in {"1", "true", "yes"}
    return {
        "photo": name,
        "origin": "research dataset" if from_dataset else "claimant",
        "cleared": from_dataset or consented,
        "reason": ("public research dataset image" if from_dataset else
                   "consent given for this run" if consented else
                   "a claimant photograph carries a number plate and has not "
                   "been redacted; plate redaction is not implemented yet"),
    }


def local_provider() -> bool:
    """A model running on this machine needs no clearance: nothing leaves."""
    return os.getenv("LLM_PROVIDER", "groq").lower() == "ollama"


# ---------------------------------------------------------------- the image
def prepare_image(path: Path) -> str:
    """Downscale and return the photo as a base64 JPEG data URL."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_EDGE:
            scale = MAX_EDGE / max(img.size)
            img = img.resize((int(img.width * scale), int(img.height * scale)),
                             Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


# ---------------------------------------------------------------- the model
def _parse(text: str) -> dict:
    """Read the model's JSON, tolerating a code fence or stray prose."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {"damage_types": [], "severity": "minor",
                "readable": False, "notes": "the model did not return JSON"}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"damage_types": [], "severity": "minor",
                "readable": False, "notes": "the model's JSON could not be read"}


def _clean(values, allowed: List[str]) -> List[str]:
    """Keep only names from the allowed list. A model that invents 'rear panel'
    must not put it into a claim record."""
    seen = []
    for value in values or []:
        name = str(value).lower().strip()
        if name in allowed and name not in seen:
            seen.append(name)
    return seen


def partition(answer: dict, readable: bool = True) -> dict:
    """The model's verdicts as three lists that cover PARTS exactly once.

    Accepts the per-part "verdicts" object the prompt asks for, or three lists
    if the model answers that way instead. Names outside PARTS are dropped
    (_clean). A part the model failed to mention, or put in two lists at once,
    goes to not_visible: an answer that contradicts itself is not evidence
    either way. An unreadable photo shows nothing, so every part is
    not_visible."""
    lists = {v: [] for v in VERDICTS}
    verdicts = answer.get("verdicts") if isinstance(answer, dict) else None
    if isinstance(verdicts, dict):
        for part, verdict in verdicts.items():
            verdict = str(verdict).lower().strip().replace(" ", "_")
            if verdict in lists:
                lists[verdict].append(part)
    elif isinstance(answer, dict):
        for verdict in VERDICTS:
            lists[verdict] = list(answer.get(verdict) or [])
        if not lists["damaged"]:
            lists["damaged"] = list(answer.get("visible_parts") or [])

    cleaned = {v: _clean(lists[v], PARTS) for v in VERDICTS}
    damaged = set(cleaned["damaged"])
    undamaged = set(cleaned["undamaged"])
    contradicted = damaged & undamaged
    if not readable:
        damaged, undamaged = set(), set()
    damaged -= contradicted
    undamaged -= contradicted
    undamaged -= damaged
    return {
        "damaged": [p for p in PARTS if p in damaged],
        "undamaged": [p for p in PARTS if p in undamaged],
        "not_visible": [p for p in PARTS if p not in damaged and p not in undamaged],
    }


def _unread(name: str, notes: str) -> dict:
    """The answer for a photograph that showed nothing: every part not_visible."""
    return {"photo": name, "damaged": [], "visible_parts": [], "undamaged": [],
            "not_visible": list(PARTS), "damage_types": [], "severity": "minor",
            "readable": False, "notes": notes}


def inspect_photo(name: str, monitor=None, timeout: int = 90) -> dict:
    """Ask the vision model for a verdict on each part in one photograph.

    Returns a plain dict - photo, damaged, undamaged, not_visible,
    damage_types, severity, readable, notes - in which the three part lists
    cover PARTS exactly once. "visible_parts" is kept as an alias of
    "damaged" for older readers. The Damage Analyst task turns it into a
    PhotoDamage.
    Never raises on a model failure: a photo that cannot be read is reported as
    unreadable, so the claim carries on with the paperwork alone rather than
    stopping.
    """
    path = PHOTOS / name
    if not path.exists() or path.stat().st_size == 0:
        return _unread(name, "photograph missing or empty")

    clearance = photo_clearance(name)
    if not (local_provider() or clearance["cleared"]):
        raise PhotoUnsafeForCloud(
            f"{name} was not sent to the model: {clearance['reason']}.")

    if os.getenv("CLAIMSIGHT_OFFLINE", "").lower() in {"1", "true", "yes"}:
        return _unread(name, "offline: no vision model was called")

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config                                            # noqa: E402
    import litellm                                           # noqa: E402

    model = config.PROFILES[config.PROVIDER].get("vision")
    kwargs = {"model": model, "timeout": timeout, "temperature": 0.1}
    if config.PROVIDER == "groq":
        kwargs["base_url"] = config.GROQ_BASE_URL
        kwargs["api_key"] = os.getenv("GROQ_API_KEY")
    elif config.PROVIDER == "ollama":
        kwargs["base_url"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": prepare_image(path)}},
    ]}]

    import time                                               # noqa: E402

    last_error = None
    answer = usage = None
    for attempt in range(VISION_ATTEMPTS):
        try:
            response = litellm.completion(messages=messages, **kwargs)
            answer = _parse(response.choices[0].message.content)
            usage = getattr(response, "usage", None)
            last_error = None
            break
        except Exception as exc:                              # noqa: BLE001
            last_error = exc
            # Only a per-minute limit is worth waiting out. A spent daily
            # allowance or a wrong model id will fail again just as fast.
            text = str(exc).lower()
            per_minute = "per minute" in text or "tpm" in text or "rpm" in text
            if not per_minute or attempt == VISION_ATTEMPTS - 1:
                break
            pause = VISION_BACKOFF[min(attempt, len(VISION_BACKOFF) - 1)]
            if monitor is not None:
                monitor.record("vision_rate_limited", photo=name, waiting_s=pause)
            print(f"    rate limited, waiting {pause}s before retry "
                  f"{attempt + 2}/{VISION_ATTEMPTS}...", flush=True)
            time.sleep(pause)

    if last_error is not None:
        exc = last_error
        # Keep the provider's own words. A bare exception name says a call
        # failed but not whether the model id is wrong, the daily allowance is
        # spent, or the image was too large, and those need different fixes.
        detail = " ".join(str(exc).split())[:300]
        if monitor is not None:
            monitor.record("vision_failed", photo=name, model=model,
                           error_type=type(exc).__name__, error=detail)
        return _unread(name, f"the vision model could not be reached "
                             f"({type(exc).__name__}): {detail}")

    readable = bool(answer.get("readable", True))
    parts = partition(answer, readable=readable)
    result = {
        "photo": name,
        **parts,
        "visible_parts": parts["damaged"],          # alias, while readers migrate
        "damage_types": _clean(answer.get("damage_types"), DAMAGE_TYPES),
        "severity": (answer.get("severity") if answer.get("severity") in
                     {"minor", "moderate", "severe"} else "moderate"),
        "readable": readable,
        "notes": str(answer.get("notes", ""))[:200],
    }
    if result["readable"] and not result["damaged"]:
        result["notes"] = (result["notes"] + " (no listed part identified)").strip()

    if monitor is not None:
        monitor.record("photo_inspected", photo=name, model=model,
                       origin=clearance["origin"], cleared=clearance["cleared"],
                       damaged=result["damaged"], undamaged=result["undamaged"],
                       not_visible=result["not_visible"],
                       damage_types=result["damage_types"],
                       readable=result["readable"],
                       prompt_tokens=getattr(usage, "prompt_tokens", None),
                       completion_tokens=getattr(usage, "completion_tokens", None))
    return result


def inspect_claim_photos(photos: List[str], monitor=None) -> List[dict]:
    """Every photo on a claim. One model call each; claims have one today."""
    return [inspect_photo(name, monitor=monitor) for name in photos or []]
