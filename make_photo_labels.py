"""
Draft data/photo_labels.json for the photos in data/photos/.

The car-damage dataset's annotations (VIA format, Vietnamese class names) say
WHAT KIND of damage is in each photo and where (a polygon), but not WHICH PART
of the car it is on. So this script fills in the damage types from the
dataset and leaves "parts" empty for a person to fill in by looking at the
photo. A hint is given where the damage type narrows the part down.

Run:  uv run python make_photo_labels.py data/annotations/0Train_via_annos.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PHOTOS = ROOT / "data" / "photos"
OUTPUT = ROOT / "data" / "photo_labels.json"

# Dataset class (Vietnamese) -> damage type used by ClaimSight
DAMAGE_TYPES = {
    "tray_son": "scratch",
    "mop_lom": "dent",
    "rach": "tear",
    "mat_bo_phan": "missing_part",
    "be_den": "broken_light",
    "thung": "puncture",
    "vo_kinh": "broken_glass",
}

# Damage types that already say which parts are possible
PART_HINTS = {
    "broken_light": "headlight or taillight",
    "broken_glass": "windshield or door glass",
}

PARTS = ["front bumper", "rear bumper", "headlight", "taillight", "side mirror",
         "grille", "bonnet", "front door", "rear door", "fender",
         "windshield", "door glass"]

IMAGE_TYPES = {".jpg", ".jpeg", ".png"}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python make_photo_labels.py <path to the *_via_annos.json file>")
    annotations = json.loads(Path(sys.argv[1]).read_text())

    photos = sorted(p.name for p in PHOTOS.iterdir() if p.suffix.lower() in IMAGE_TYPES)
    if not photos:
        sys.exit(f"no images found in {PHOTOS}")

    existing = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
    labels, missing = {}, []
    for name in photos:
        entry = annotations.get(name)
        if entry is None:
            missing.append(name)
            continue
        found = [DAMAGE_TYPES.get(r.get("class"), f"unknown:{r.get('class')}")
                 for r in entry.get("regions", [])]
        types = sorted(set(found))
        hints = [PART_HINTS[t] for t in types if t in PART_HINTS]
        labels[name] = {
            "damage_types": types,                       # from the dataset: real ground truth
            "regions": len(found),
            "parts": existing.get(name, {}).get("parts", []),   # filled in by a person; kept on re-run
            "hint": "; ".join(hints) or "look at the photo",
        }

    OUTPUT.write_text(json.dumps(labels, indent=2))
    print(f"Wrote {OUTPUT.relative_to(ROOT)} for {len(labels)} photos.")
    if missing:
        print(f"Not in the annotation file (skipped): {', '.join(missing)}")
    empty = [n for n, v in labels.items() if not v["parts"]]
    if empty:
        print(f"\nFill in \"parts\" for {len(empty)} photos, using only these names:")
        print("  " + ", ".join(PARTS))


if __name__ == "__main__":
    main()
