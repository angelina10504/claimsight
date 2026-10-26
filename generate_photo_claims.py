"""
ClaimSight photo claims: paperwork built around REAL damage photos.

The photos and their damage labels are real (car-damage dataset, plus the
damaged parts a person read off each photo, in data/photo_labels.json).
No real policy or garage bill exists for them, so this script generates
those documents to match what each photo actually shows.

Writes, for every labelled photo with at least one part:
  data/policies/POL-6XXX.json
  data/invoices/CLM-6XXX.pdf
  data/claims/CLM-6XXX.json
  data/photo_manifest.json          its own index; data/manifest.json
                                    (the official 60) is never touched

Fraud is either planted in the paperwork (a billed part the photo does not
show, an inflated price, a date before the policy, a wear-and-tear line) or
REAL: two photos of the same car become a genuine duplicate claim.

Run:  uv run python generate_photo_claims.py
"""

import json
import random
from datetime import date, timedelta

import generate_data as gd        # reuse the same catalogue, rules and invoice template

LABELS = gd.DATA / "photo_labels.json"
MANIFEST = gd.DATA / "photo_manifest.json"
PHOTOS = gd.DATA / "photos"

FIRST_ID = 6000
SEED = 6042                       # fixed, so the set rebuilds identically

# Faults for photos that are not a duplicate, applied in turn.
PHOTO_FAULTS = [
    "clean",
    "phantom_part",
    "clean",
    "inflated_amount",
    "clean",
    "date_outside_policy",
    "clean",
    "excluded_wear_tear",
]


def load_photos() -> list:
    """Labelled photos that can become a claim, originals before duplicates,
    because claim ids are issued in filing order."""
    if not LABELS.exists():
        raise SystemExit(f"{LABELS.relative_to(gd.ROOT)} not found. Run make_photo_labels.py first.")
    labels = json.loads(LABELS.read_text())
    usable, skipped = [], []
    for name, label in labels.items():
        bad_parts = [p for p in label.get("parts", []) if p not in gd.PARTS]
        if bad_parts:
            raise SystemExit(f"{name}: unknown part(s) {bad_parts}. Use only: {', '.join(gd.PARTS)}")
        photo = PHOTOS / name
        if not label.get("parts"):
            skipped.append(f"{name} (no parts labelled)")
        elif not photo.exists() or photo.stat().st_size == 0:
            skipped.append(f"{name} (photo missing or empty)")
        else:
            usable.append((name, label))
    usable.sort(key=lambda item: "same_car_as" in item[1])   # duplicates last
    return usable, skipped


def make_photo_policy(number: int, label: dict) -> dict:
    policy = gd.make_policy(0)
    policy["policy_no"] = f"POL-{number}"
    if label.get("vehicle"):                       # what the photo shows, when known
        policy["make"], policy["model"] = label["vehicle"]
    return policy


def main() -> None:
    random.seed(SEED)
    gd.Faker.seed(SEED)
    photos, skipped = load_photos()
    if not photos:
        raise SystemExit("No usable photos: every photo needs at least one part in photo_labels.json.")

    made = {}            # photo name -> (claim, policy, items), so a duplicate can find its original
    manifest = []
    fault_turn = 0
    for offset, (name, label) in enumerate(photos):
        claim_id = f"CLM-{FIRST_ID + offset}"
        damaged = list(label["parts"])
        original = made.get(label.get("same_car_as"))

        if original:
            # A REAL duplicate: a second photo of the same car, claimed again.
            fault = "duplicate_claim"
            first_claim, policy, first_items = original
            duplicate_of = first_claim["claim_id"]
            incident = min(date.fromisoformat(first_claim["incident_date"]) + timedelta(days=12),
                           date.today() - timedelta(days=3))
            items = [dict(item, admitted=False,
                          reason=f"same damage already claimed under {duplicate_of}")
                     for item in first_items]
            narrative = first_claim["narrative"]
            phantom = None
        else:
            if label.get("same_car_as"):
                print(f"  note: {name} says same car as {label['same_car_as']}, "
                      "which has no claim; treated as a normal claim")
            fault = PHOTO_FAULTS[fault_turn % len(PHOTO_FAULTS)]
            fault_turn += 1
            duplicate_of = None
            policy = make_photo_policy(FIRST_ID + offset, label)
            items, phantom = gd.build_line_items(damaged, fault)
            start = date.fromisoformat(policy["policy_start"])
            if fault == "date_outside_policy":
                incident = start - timedelta(days=random.randint(5, 40))
            else:
                incident = min(start + timedelta(days=random.randint(10, 300)),
                               date.today() - timedelta(days=3))
            narrative = random.choice([
                "Vehicle was hit by another car while parked outside a market.",
                "Collided with a divider while taking a turn in the rain.",
                "A two-wheeler struck the vehicle at a traffic signal.",
                "Reversed into a pillar in the basement parking.",
                "Side-swiped by a truck while changing lanes.",
            ])

        invoice_date = incident + timedelta(days=random.randint(1, 6))
        invoice_path = gd.INVOICES / f"{claim_id}.pdf"
        gd.render_invoice(invoice_path, policy, items,
                          f"INV/{random.randint(100, 999)}/26", invoice_date.isoformat())

        covered = fault != "date_outside_policy"
        subtotal = sum(i["amount"] for i in items)
        claim = {
            "claim_id": claim_id,
            "policy_no": policy["policy_no"],
            "incident_date": incident.isoformat(),
            "invoice_date": invoice_date.isoformat(),
            "narrative": narrative,
            "photos": [name],
            "invoice_pdf": str(invoice_path.relative_to(gd.ROOT)),
            "invoice_subtotal": subtotal,
            "invoice_total": round(subtotal * (1 + gd.GST)),
            "line_items": items,
            "ground_truth": {
                "damaged_parts": damaged,                       # read off the real photo
                "photo_damage_types": label["damage_types"],    # from the dataset's annotators
                "fault_type": fault,
                "is_covered": covered,
                "is_fraudulent": fault in {"inflated_amount", "phantom_part", "duplicate_claim"},
                "phantom_part": phantom,
                "duplicate_of": duplicate_of,
                "expected_payout": gd.expected_payout(items, policy, covered),
                "fraud_source": "real photo" if duplicate_of else
                                ("planted in paperwork" if fault != "clean" else None),
            },
            "split": "photo",
        }
        made[name] = (claim, policy, items)

        if duplicate_of is None:            # a duplicate reuses the original's policy
            (gd.POLICIES / f"{policy['policy_no']}.json").write_text(json.dumps(policy, indent=2))
        (gd.CLAIMS / f"{claim_id}.json").write_text(json.dumps(claim, indent=2))
        manifest.append({"claim_id": claim_id, "policy_no": policy["policy_no"],
                         "photo": name, "fault_type": fault, "split": "photo"})
        print(f"  {claim_id}  {fault:20s}  {', '.join(damaged):40s}  {name}")

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"\nGenerated {len(manifest)} photo claims -> {MANIFEST.relative_to(gd.ROOT)}")
    if skipped:
        print("Skipped: " + "; ".join(skipped))


if __name__ == "__main__":
    main()
