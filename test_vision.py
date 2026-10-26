"""
Test the Damage Analyst's photo verdicts and the check that compares them with
the invoice.

Costs no tokens. The vision model is never called: the offline switch returns
before any model is imported, and the one check that exercises the parse-and-
clean path gives inspect_photo a stand-in litellm with a canned answer. Every
provider key is blanked and outbound connections are refused, so a mistake here
fails loudly instead of spending tokens.

What is being tested is the rule that matters for an honest claimant:

    billed part shown undamaged      -> flagged (the photo is evidence against it)
    billed part not visible          -> unchecked, no flag (a further photo would settle it)
    unreadable photograph            -> inconclusive, no flag
    a part outside the 12-part list  -> ignored
    damaged + undamaged + not_visible cover all 12 parts exactly once

Run:  CLAIMSIGHT_OFFLINE=1 uv run python test_vision.py
"""

import os

os.environ["CLAIMSIGHT_OFFLINE"] = "1"
for _key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
             "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ[_key] = ""                # "" survives load_dotenv; a pop does not
os.environ.pop("CLAIMSIGHT_PHOTO_CONSENT", None)
os.environ["LLM_PROVIDER"] = "groq"      # a cloud provider, so clearance applies
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"   # importing crewai fetches it otherwise

import json
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "tools"))

import vision                                                       # noqa: E402
from assessment_tools import check_billed_parts_against_photo       # noqa: E402

DATASET_PHOTO = next(iter(json.loads(vision.LABELS.read_text())))

connections = []
_real_connect = socket.socket.connect


def _refuse(self, address):
    connections.append(address)
    raise ConnectionRefusedError(f"test_vision refuses network access to {address}")


socket.socket.connect = _refuse

passed = total = 0


def check(label, ok, detail=""):
    global passed, total
    total += 1
    passed += bool(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))


def lines(*descriptions):
    return [{"description": d, "amount": 5000.0} for d in descriptions]


def covers_all(result) -> bool:
    lists = [result["damaged"], result["undamaged"], result["not_visible"]]
    flat = [p for group in lists for p in group]
    return sorted(flat) == sorted(vision.PARTS) and len(flat) == len(set(flat))


def main():
    # ------------------------------------------------------------ the comparison
    print("\n1. Comparing the photograph with the invoice\n")

    # CLM-6001's failure: the front door is the phantom. Under the old rule it
    # slipped through because the model listed it as damaged; under the new one,
    # a front door seen intact is flagged.
    r = check_billed_parts_against_photo(
        lines("Fender - replacement", "Front door - replacement"),
        damaged=["fender"], undamaged=["front door", "bonnet"],
        not_visible=[p for p in vision.PARTS if p not in ("fender", "front door", "bonnet")])
    check("a billed part shown undamaged is flagged",
          r["unsupported_parts"] == ["front door"] and len(r["flags"]) == 1 and r["suspicious"],
          f"flags={r['flags']}")
    check("the damaged billed part is not flagged", "fender" not in r["unsupported_parts"])

    # CLM-6002's failure: a rear bumper close-up the model could not place.
    r = check_billed_parts_against_photo(
        lines("Rear bumper - replacement", "Painting charges"),
        damaged=[], undamaged=["grille"],
        not_visible=[p for p in vision.PARTS if p != "grille"])
    check("a billed part in not_visible is NOT flagged",
          not r["flags"] and not r["suspicious"] and r["unsupported_parts"] == [],
          f"flags={r['flags']}")
    check("... and appears in unchecked_parts", r["unchecked_parts"] == ["rear bumper"],
          f"unchecked_parts={r['unchecked_parts']}")
    check("... and the note says a further photograph would settle it",
          "further photograph" in r["note"] and not r["conclusive"], r["note"])

    r = check_billed_parts_against_photo(
        lines("Front bumper - replacement", "Headlight - replacement"),
        damaged=["front bumper", "headlight"], undamaged=[], not_visible=[],
        photo_readable=False)
    check("an unreadable photo gives conclusive=False and no flags",
          r["conclusive"] is False and r["flags"] == [] and r["unsupported_parts"] == [],
          r["note"][:70])

    r = check_billed_parts_against_photo([], damaged=["fender"], undamaged=[], not_visible=[])
    check("an empty invoice is reported as an incomplete document",
          r.get("reason") == "incomplete_document" and "flags" not in r)

    r = check_billed_parts_against_photo(
        lines("Front bumper - replacement"), damaged=["front bumper"],
        undamaged=["front bumper"], not_visible=[])
    check("a part the photo calls both damaged and undamaged is not flagged",
          not r["flags"], f"flags={r['flags']}")

    r = check_billed_parts_against_photo(
        lines("Front bumper - replacement"), damaged=["front bumper"],
        undamaged=[p for p in vision.PARTS if p != "front bumper"], not_visible=[])
    check("every billed part seen damaged is conclusive with no flag",
          r["conclusive"] and not r["flags"] and not r["unchecked_parts"], r["note"])

    # ------------------------------------------------------------ the three lists
    print("\n2. The model's answer becomes three lists over the 12 parts\n")

    p = vision.partition({"verdicts": {"rear bumper": "damaged", "rear panel": "damaged",
                                       "fender": "undamaged", "wheel": "undamaged",
                                       "grille": "not visible"}})
    check("a part outside the 12 is ignored",
          "rear panel" not in sum(p.values(), []) and "wheel" not in sum(p.values(), []),
          f"damaged={p['damaged']} undamaged={p['undamaged']}")
    check("the three lists cover all 12 parts with no overlap", covers_all(p))
    check("a part the model did not mention goes to not_visible",
          "bonnet" in p["not_visible"] and "front door" in p["not_visible"])

    p = vision.partition({"damaged": ["Fender", "fender"], "undamaged": ["fender", "bonnet"],
                          "not_visible": []})
    check("the list-shaped answer is accepted, and a contradiction goes to not_visible",
          p["damaged"] == [] and p["undamaged"] == ["bonnet"]
          and "fender" in p["not_visible"] and covers_all(p), str(p["undamaged"]))

    p = vision.partition({"verdicts": {"front bumper": "damaged", "bonnet": "undamaged"}},
                         readable=False)
    check("an unreadable photo puts every part in not_visible",
          p["not_visible"] == vision.PARTS and not p["damaged"] and not p["undamaged"])

    # The parse-and-clean path end to end, with a stand-in model.
    canned = json.dumps({"verdicts": {"rear bumper": "damaged", "rear door": "damaged",
                                      "rear windscreen": "damaged", "fender": "undamaged"},
                         "damage_types": ["dent", "smudge"], "severity": "moderate",
                         "readable": True, "notes": "rear quarter"})
    fake = types.ModuleType("litellm")
    fake.calls = []

    def completion(**kwargs):
        fake.calls.append(kwargs)
        message = types.SimpleNamespace(content="```json\n" + canned + "\n```")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)],
                                     usage=None)

    fake.completion = completion
    real_litellm = sys.modules.get("litellm")
    sys.modules["litellm"] = fake
    os.environ["CLAIMSIGHT_OFFLINE"] = ""
    try:
        seen = vision.inspect_photo(DATASET_PHOTO)
    finally:
        os.environ["CLAIMSIGHT_OFFLINE"] = "1"
        if real_litellm is None:
            sys.modules.pop("litellm", None)
        else:
            sys.modules["litellm"] = real_litellm
    check("inspect_photo returns the new shape, through a stand-in model",
          {"photo", "damaged", "undamaged", "not_visible", "damage_types", "severity",
           "readable", "notes"} <= set(seen) and len(fake.calls) == 1, str(sorted(seen)))
    check("... drops the invented part and damage type",
          seen["damaged"] == ["rear bumper", "rear door"] and seen["damage_types"] == ["dent"],
          f"damaged={seen['damaged']} types={seen['damage_types']}")
    check("... covers all 12 parts exactly once", covers_all(seen))
    check("... keeps visible_parts as an alias of damaged",
          seen["visible_parts"] == seen["damaged"])

    # ------------------------------------------------------------ privacy
    print("\n3. Which photographs may leave this machine\n")

    c = vision.photo_clearance(DATASET_PHOTO)
    check("a research dataset photo is cleared", c["cleared"] and c["origin"] == "research dataset",
          DATASET_PHOTO)
    c = vision.photo_clearance("claimant_upload_0001.jpg")
    check("a claimant photo is not cleared", not c["cleared"] and c["origin"] == "claimant",
          c["reason"][:70])

    folder = Path(tempfile.mkdtemp(prefix="claimsight_vision_"))
    real_photos = vision.PHOTOS
    try:
        shutil.copy(real_photos / DATASET_PHOTO, folder / "claimant_upload_0001.jpg")
        vision.PHOTOS = folder
        try:
            vision.inspect_photo("claimant_upload_0001.jpg")
            blocked = False
        except vision.PhotoUnsafeForCloud:
            blocked = True
        check("inspect_photo refuses to send a claimant photo", blocked)
    finally:
        vision.PHOTOS = real_photos
        shutil.rmtree(folder, ignore_errors=True)

    # ------------------------------------------------------------ offline
    print("\n4. CLAIMSIGHT_OFFLINE=1\n")

    before = "litellm" in sys.modules
    seen = vision.inspect_photo(DATASET_PHOTO)
    check("makes no network call", connections == [], f"connections={connections}")
    check("does not even import the model client",
          ("litellm" in sys.modules) == before)
    check("reports the photo as unread, every part not_visible",
          not seen["readable"] and seen["not_visible"] == vision.PARTS and covers_all(seen),
          seen["notes"])

    # ------------------------------------------------------------ the crew wiring
    print("\n5. The intake crew\n")

    from crews import intake_crew
    from models import ClaimIntake, PhotoFindings

    photo_claims = json.loads((ROOT / "data" / "photo_manifest.json").read_text())
    with_photo = photo_claims[0]["claim_id"]
    inputs = intake_crew.fetch_inputs(with_photo)
    findings = json.loads(inputs.get("photo_findings", "[]"))
    check("a claim with photos gets photo_findings in its inputs",
          len(findings) == 1 and covers_all(findings[0]), with_photo)
    check("the findings carry nothing from the invoice",
          "invoice" not in inputs["photo_findings"].lower()
          and "visible_parts" not in inputs["photo_findings"])
    check("a claim with no photos is unchanged: no photo_findings",
          "photo_findings" not in intake_crew.fetch_inputs("CLM-5000"))
    check("ClaimIntake.photo_damage defaults to empty",
          "photo_damage" in ClaimIntake.model_fields
          and ClaimIntake.model_fields["photo_damage"].default_factory() == [])

    intake_crew.get_llm = lambda *a, **k: "openai/stand-in"
    plain = intake_crew.build_intake_crew(verbose=False)
    crew = intake_crew.build_intake_crew(verbose=False, with_photos=True)
    analyst_task = crew.tasks[-1]
    check("without photos the crew has its two agents, with photos a third",
          len(plain.agents) == 2 and len(crew.agents) == 3
          and crew.agents[-1].role == "Damage Analyst")
    check("the Damage Analyst's task sees no invoice and no earlier task",
          "{invoice_text}" not in analyst_task.description and analyst_task.context == []
          and analyst_task.output_pydantic is PhotoFindings)
    check("the intake task is still second, so run_intake reads the right output",
          crew.tasks[1].output_pydantic is ClaimIntake)

    inspected = [vision._unread("a.jpg", "x") | {"readable": True,
                                                 "damaged": ["fender"],
                                                 "not_visible": [p for p in vision.PARTS
                                                                 if p != "fender"]}]
    altered = PhotoFindings(photos=[{"photo": "a.jpg", "damaged": ["fender"],
                                     "undamaged": ["front door"],
                                     "not_visible": [p for p in vision.PARTS
                                                     if p not in ("fender", "front door")],
                                     "readable": True}])
    kept = intake_crew.photo_damage(altered, inspected)
    check("an agent that moves a part is overruled by the inspection",
          kept[0].undamaged == [] and "front door" in kept[0].not_visible)
    check("an agent that drops a photo is filled in from the inspection",
          intake_crew.photo_damage(PhotoFindings(), inspected)[0].photo == "a.jpg")

    check("still no network call after the crew wiring", connections == [],
          f"connections={connections}")

    print(f"\n{passed}/{total} vision checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
