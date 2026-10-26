"""
Run the Damage Analyst over the photo claims and score it.

For each claim it looks at the real photograph, asks the vision model for a
verdict on each of the 12 parts - damaged, undamaged or not visible - then
compares that, in code, with the parts billed on the invoice. The phantom part
is the case to watch: the paperwork is clean, so only the photograph can catch
it.

A billed part is flagged only when the photograph shows it undamaged. A billed
part the photograph does not show is "unchecked": not a flag, and counted
separately, so a false flag and a missing view are never confused.

  uv run python run_damage_analyst.py                 all photo claims
  uv run python run_damage_analyst.py CLM-6001        one claim
  uv run python run_damage_analyst.py --offline       no model call, wiring only

Costs vision tokens: roughly one image per claim.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "tools"))

import vision                                                        # noqa: E402
from assessment_tools import check_billed_parts_against_photo        # noqa: E402

CLAIMS = ROOT / "data" / "claims"
PHOTO_MANIFEST = ROOT / "data" / "photo_manifest.json"


def claim_ids(args) -> list:
    wanted = [a for a in args if a.startswith("CLM-")]
    if wanted:
        return wanted
    if not PHOTO_MANIFEST.exists():
        sys.exit("data/photo_manifest.json not found. Run generate_photo_claims.py first.")
    return [e["claim_id"] for e in json.loads(PHOTO_MANIFEST.read_text())]


def main() -> None:
    args = sys.argv[1:]
    if "--offline" in args:
        import os
        os.environ["CLAIMSIGHT_OFFLINE"] = "1"

    rows = []
    for claim_id in claim_ids(args):
        path = CLAIMS / f"{claim_id}.json"
        if not path.exists():
            print(f"{claim_id}: no claim on file")
            continue
        claim = json.loads(path.read_text())
        truth = claim.get("ground_truth", {})
        photos = claim.get("photos") or []
        if not photos:
            print(f"{claim_id}: no photograph on this claim")
            continue

        print(f"\n{'=' * 72}\n{claim_id}  ({truth.get('fault_type', '?')})  {photos[0]}")
        try:
            seen = vision.inspect_photo(photos[0])
        except vision.PhotoUnsafeForCloud as exc:
            print(f"  STOPPED: {exc}")
            continue

        print(f"  damaged       : {', '.join(seen['damaged']) or '(none)'}")
        print(f"  undamaged     : {', '.join(seen['undamaged']) or '(none)'}")
        print(f"  not visible   : {', '.join(seen['not_visible']) or '(none)'}")
        print(f"  damage types  : {', '.join(seen['damage_types']) or '-'}"
              f"   severity {seen['severity']}   readable {seen['readable']}")
        if seen["notes"]:
            print(f"  note          : {seen['notes']}")
        print(f"  truth (person): {', '.join(truth.get('damaged_parts', []))}")
        print(f"  truth (datset): {', '.join(truth.get('photo_damage_types', []))}")

        lines = [{"description": i["description"], "amount": i["amount"]}
                 for i in claim.get("line_items", [])]
        check = check_billed_parts_against_photo(lines, seen["damaged"],
                                                 seen["undamaged"], seen["not_visible"],
                                                 photo_readable=seen["readable"])
        print(f"  billed parts  : {', '.join(check.get('billed_parts', []))}")
        for flag in check.get("flags", []):
            print(f"  FLAG          : {flag}")
        for part in check.get("unchecked_parts", []):
            print(f"  UNCHECKED     : {part} (not visible; a further photograph would settle it)")
        if not check.get("flags"):
            print(f"  no flag       : {check.get('note')}")

        expected_phantom = truth.get("phantom_part")
        caught = expected_phantom in (check.get("unsupported_parts") or [])
        parts_right = (set(seen["damaged"]) == set(truth.get("damaged_parts", [])))
        rows.append({
            "claim_id": claim_id,
            "fault": truth.get("fault_type"),
            "readable": seen["readable"],
            "parts_exact": parts_right,
            "phantom_expected": expected_phantom,
            "phantom_caught": caught if expected_phantom else None,
            "false_flags": [p for p in (check.get("unsupported_parts") or [])
                            if p != expected_phantom],
            "unchecked": check.get("unchecked_parts") or [],
            "phantom_unchecked": bool(expected_phantom)
                                 and expected_phantom in (check.get("unchecked_parts") or []),
        })

    if not rows:
        return
    print(f"\n{'=' * 72}\nSUMMARY\n")
    readable = sum(r["readable"] for r in rows)
    exact = sum(r["parts_exact"] for r in rows)
    phantoms = [r for r in rows if r["phantom_expected"]]
    caught = sum(bool(r["phantom_caught"]) for r in phantoms)
    false_flagged = [r for r in rows if r["false_flags"]]
    unchecked = [r for r in rows if r["unchecked"]]
    phantom_unchecked = sum(r["phantom_unchecked"] for r in phantoms)
    print(f"  photographs read            : {readable}/{len(rows)}")
    print(f"  parts matched the person    : {exact}/{len(rows)}")
    print(f"  phantom parts caught        : {caught}/{len(phantoms)}"
          if phantoms else "  phantom parts caught        : no phantom claim in this set")
    if phantoms:
        print(f"  phantom parts left unchecked: {phantom_unchecked}/{len(phantoms)}")
    print(f"  claims with a false flag    : {len(false_flagged)}/{len(rows)}"
          + (f"   {[r['claim_id'] for r in false_flagged]}" if false_flagged else ""))
    print(f"  claims with unchecked parts : {len(unchecked)}/{len(rows)}"
          + (f"   {[r['claim_id'] for r in unchecked]}" if unchecked else ""))
    print("\n  A false flag means the photograph was read as showing a genuinely"
          "\n  damaged billed part intact. That sends an honest claim to a person"
          "\n  as suspicious - the number to drive to zero."
          "\n  An unchecked part is not a flag: the photograph did not show it,"
          "\n  and a further photograph would settle it.")


if __name__ == "__main__":
    main()
