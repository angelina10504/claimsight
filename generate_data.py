"""
ClaimSight synthetic data generator.

Produces, under data/:
  policies/POL-XXXX.json     policy schedule (IDV, deductible, dates, add-ons)
  invoices/CLM-XXXX.pdf      garage repair bill
  claims/CLM-XXXX.json       claim record, line items, and ground truth
  manifest.json              index of all claims with split (dev/test)

Run:  uv run python generate_data.py
"""

import json
import random
from datetime import date, timedelta
from pathlib import Path

from faker import Faker
from weasyprint import HTML

fake = Faker("en_IN")
SEED = 42
random.seed(SEED)
Faker.seed(SEED)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
POLICIES = DATA / "policies"
INVOICES = DATA / "invoices"
CLAIMS = DATA / "claims"
LABELS_FILE = DATA / "photo_labels.json"

N_CLAIMS = 60
N_DEV = 20
GST = 0.18

# ---------------------------------------------------------------- catalogue
# material drives the depreciation rule from the ICICI Lombard wording:
#   plastic / rubber  -> 50%
#   glass             -> nil
#   metal (other)     -> age-band table
#   paint             -> 50% of material, material = 25% of charge => 12.5%
#   labour            -> nil
PARTS = {
    "front bumper":   {"material": "plastic", "price": 9000},
    "rear bumper":    {"material": "plastic", "price": 8500},
    "headlight":      {"material": "plastic", "price": 7200},
    "taillight":      {"material": "plastic", "price": 4300},
    "side mirror":    {"material": "plastic", "price": 3100},
    "grille":         {"material": "plastic", "price": 2800},
    "bonnet":         {"material": "metal",   "price": 12500},
    "front door":     {"material": "metal",   "price": 14000},
    "rear door":      {"material": "metal",   "price": 13200},
    "fender":         {"material": "metal",   "price": 6800},
    "windshield":     {"material": "glass",   "price": 11000},
    "door glass":     {"material": "glass",   "price": 4800},
}

VEHICLES = [
    ("Maruti Suzuki", "Swift VXi", 620000),
    ("Hyundai", "i20 Sportz", 780000),
    ("Tata", "Nexon XZ+", 940000),
    ("Honda", "City SV", 1050000),
    ("Maruti Suzuki", "Baleno Zeta", 810000),
    ("Kia", "Seltos HTK", 1150000),
]

AGE_BANDS = [
    (0.5, 0.00), (1, 0.05), (2, 0.10), (3, 0.15),
    (4, 0.25), (5, 0.35), (10, 0.40), (99, 0.50),
]

FAULTS = [
    "clean", "clean", "clean", "clean",
    "inflated_amount",
    "phantom_part",
    "date_outside_policy",
    "excluded_wear_tear",
    "duplicate_claim",
    "clean",
]


def metal_depreciation(age_years: float) -> float:
    for limit, rate in AGE_BANDS:
        if age_years <= limit:
            return rate
    return 0.50


def depreciation_rate(material: str, age_years: float) -> float:
    if material == "plastic":
        return 0.50
    if material == "glass":
        return 0.00
    if material == "paint":
        return 0.125          # 50% on material, material = 25% of charge
    if material == "labour":
        return 0.00
    return metal_depreciation(age_years)


# ---------------------------------------------------------------- generation
def make_policy(idx: int) -> dict:
    make, model, base_idv = random.choice(VEHICLES)
    age_years = random.choice([0.4, 1.2, 2.5, 3.4, 4.8, 6.0, 8.5])
    idv = int(base_idv * (1 - min(0.5, 0.1 * age_years)) / 1000) * 1000
    start = date(2026, 1, 1) + timedelta(days=random.randint(0, 200))
    return {
        "policy_no": f"POL-{3000 + idx}",
        "insured_name": fake.name(),
        "phone": fake.phone_number(),
        "address": fake.address().replace("\n", ", "),
        "make": make,
        "model": model,
        "registration": f"HR{random.randint(10, 99)}{fake.random_uppercase_letter()}"
                        f"{fake.random_uppercase_letter()}{random.randint(1000, 9999)}",
        "vehicle_age_years": age_years,
        "idv": idv,
        "deductible": 1000,
        "policy_start": start.isoformat(),
        "policy_end": (start + timedelta(days=364)).isoformat(),
        "zero_depreciation": random.random() < 0.25,
        "ncb_percent": random.choice([0, 20, 25, 35]),
    }


def build_line_items(damaged_parts, fault):
    """Return (line_items, phantom_part_or_None).

    Each item carries `admitted`: whether a correct adjuster would pay for it.
    Deciding this at build time gives the ground truth the Fraud and Policy
    agents are graded against.
    """
    items = []
    for part in damaged_parts:
        spec = PARTS[part]
        price = int(spec["price"] * random.uniform(0.9, 1.1) / 10) * 10
        items.append({"description": f"{part.title()} - replacement",
                      "material": spec["material"], "amount": price,
                      "admitted": True, "reason": ""})

    phantom = None
    if fault == "phantom_part":
        phantom = random.choice([p for p in PARTS if p not in damaged_parts])
        items.insert(1, {
            "description": f"{phantom.title()} - replacement",
            "material": PARTS[phantom]["material"],
            "amount": PARTS[phantom]["price"],
            "admitted": False,
            "reason": "billed part not visible in damage evidence"})

    if fault == "excluded_wear_tear":
        items.insert(1, {
            "description": "Clutch plate wear - replacement",
            "material": "metal", "amount": 6500, "admitted": False,
            "reason": "wear and tear excluded under Section I"})

    if fault == "inflated_amount":
        items[0]["amount"] = int(items[0]["amount"] * random.uniform(2.2, 3.0))
        items[0]["admitted"] = False
        items[0]["reason"] = "line item priced far above market rate"

    parts_total = sum(i["amount"] for i in items)
    paint = int(parts_total * random.uniform(0.25, 0.4) / 10) * 10
    items.append({"description": "Painting and finishing", "material": "paint",
                  "amount": paint, "admitted": True, "reason": ""})
    labour = int((parts_total + paint) * random.uniform(0.10, 0.18) / 10) * 10
    items.append({"description": "Labour charges", "material": "labour",
                  "amount": labour, "admitted": True, "reason": ""})
    return items, phantom


def expected_payout(items, policy, covered):
    """Ground-truth settlement a correct adjuster would reach."""
    if not covered:
        return 0.0
    age = policy["vehicle_age_years"]
    payable = 0.0
    for item in items:
        if not item["admitted"]:
            continue
        rate = 0.0 if policy["zero_depreciation"] else \
            depreciation_rate(item["material"], age)
        payable += item["amount"] * (1 - rate)
    payable *= (1 + GST)                       # settled on the billed amount
    payable -= policy["deductible"]
    return round(max(0.0, min(payable, policy["idv"])), 2)


INVOICE_HTML = """
<html><head><style>
 body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11pt; margin: 30px; }}
 h1 {{ font-size: 16pt; margin-bottom: 0; }}
 .sub {{ color: #444; font-size: 9pt; margin-bottom: 18px; }}
 table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
 th, td {{ border: 1px solid #999; padding: 6px 8px; text-align: left; }}
 th {{ background: #eee; }}
 td.amt {{ text-align: right; }}
 .tot {{ font-weight: bold; }}
 .foot {{ margin-top: 24px; font-size: 9pt; color: #555; }}
</style></head><body>
 <h1>{garage}</h1>
 <div class="sub">{garage_addr}<br/>GSTIN: {gstin} &nbsp;|&nbsp; Ph: {garage_ph}</div>
 <table>
  <tr><td><b>Invoice No.</b></td><td>{invoice_no}</td>
      <td><b>Date</b></td><td>{invoice_date}</td></tr>
  <tr><td><b>Customer</b></td><td>{customer}</td>
      <td><b>Vehicle</b></td><td>{make} {model}</td></tr>
  <tr><td><b>Reg. No.</b></td><td>{registration}</td>
      <td><b>Job Card</b></td><td>JC/{jobcard}</td></tr>
 </table>
 <table>
  <tr><th>#</th><th>Description</th><th>Amount (Rs.)</th></tr>
  {rows}
  <tr class="tot"><td colspan="2">Subtotal</td><td class="amt">{subtotal:,}</td></tr>
  <tr class="tot"><td colspan="2">GST @ 18%</td><td class="amt">{gst:,}</td></tr>
  <tr class="tot"><td colspan="2">Total Payable</td><td class="amt">{total:,}</td></tr>
 </table>
 <div class="foot">Payment due on delivery. Warranty on replaced parts as per
 manufacturer terms. This is a computer-generated invoice.</div>
</body></html>
"""


def render_invoice(path, policy, items, invoice_no, invoice_date):
    rows = "".join(
        f"<tr><td>{n}</td><td>{i['description']}</td>"
        f"<td class='amt'>{i['amount']:,}</td></tr>"
        for n, i in enumerate(items, 1)
    )
    subtotal = sum(i["amount"] for i in items)
    gst = round(subtotal * GST)
    HTML(string=INVOICE_HTML.format(
        garage=f"{fake.last_name()} Auto Works",
        garage_addr=fake.address().replace("\n", ", "),
        gstin=f"06{fake.random_uppercase_letter()}{fake.random_uppercase_letter()}"
              f"{random.randint(1000, 9999)}{fake.random_uppercase_letter()}1Z{random.randint(1, 9)}",
        garage_ph=fake.phone_number(),
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        customer=policy["insured_name"],
        make=policy["make"], model=policy["model"],
        registration=policy["registration"],
        jobcard=random.randint(1000, 9999),
        rows=rows, subtotal=subtotal, gst=gst, total=subtotal + gst,
    )).write_pdf(path)


def main():
    for d in (POLICIES, INVOICES, CLAIMS):
        d.mkdir(parents=True, exist_ok=True)

    labels = {}
    if LABELS_FILE.exists():
        labels = json.loads(LABELS_FILE.read_text())
        print(f"Using {len(labels)} labelled photos from {LABELS_FILE.name}")
    else:
        print("No photo_labels.json found - generating claims without photos.")

    manifest = []
    for i in range(N_CLAIMS):
        claim_id = f"CLM-{5000 + i}"
        policy = make_policy(i)
        fault = FAULTS[i % len(FAULTS)]

        if labels:
            photo, parts = random.choice(list(labels.items()))
            photos, damaged = [photo], parts
        else:
            photos = []
            damaged = random.sample(list(PARTS), random.randint(1, 3))

        items, phantom = build_line_items(damaged, fault)

        start = date.fromisoformat(policy["policy_start"])
        if fault == "date_outside_policy":
            incident = start - timedelta(days=random.randint(5, 40))
        else:
            incident = start + timedelta(days=random.randint(10, 300))
            incident = min(incident, date.today() - timedelta(days=3))
        invoice_date = incident + timedelta(days=random.randint(1, 6))

        invoice_path = INVOICES / f"{claim_id}.pdf"
        render_invoice(invoice_path, policy, items,
                       f"INV/{random.randint(100, 999)}/26", invoice_date.isoformat())

        covered = fault != "date_outside_policy"
        subtotal = sum(i["amount"] for i in items)

        claim = {
            "claim_id": claim_id,
            "policy_no": policy["policy_no"],
            "incident_date": incident.isoformat(),
            "invoice_date": invoice_date.isoformat(),
            "narrative": random.choice([
                "Vehicle was hit by another car while parked outside a market.",
                "Collided with a divider while taking a turn in the rain.",
                "A two-wheeler struck the vehicle at a traffic signal.",
                "Reversed into a pillar in the basement parking.",
                "Stone impact on the highway damaged the front of the vehicle.",
            ]),
            "photos": photos,
            "invoice_pdf": str(invoice_path.relative_to(ROOT)),
            "invoice_subtotal": subtotal,
            "invoice_total": round(subtotal * (1 + GST)),
            "line_items": items,
            "ground_truth": {
                "damaged_parts": damaged,
                "fault_type": fault,
                "is_covered": covered,
                "is_fraudulent": fault in {"inflated_amount", "phantom_part",
                                           "duplicate_claim"},
                "phantom_part": phantom,
                "expected_payout": expected_payout(items, policy, covered),
            },
            "split": "dev" if i < N_DEV else "test",
        }

        (POLICIES / f"{policy['policy_no']}.json").write_text(
            json.dumps(policy, indent=2))
        (CLAIMS / f"{claim_id}.json").write_text(json.dumps(claim, indent=2))
        manifest.append({"claim_id": claim_id, "policy_no": policy["policy_no"],
                         "fault_type": fault, "split": claim["split"]})

    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))
    dev = sum(1 for m in manifest if m["split"] == "dev")
    print(f"Generated {len(manifest)} claims ({dev} dev / {len(manifest)-dev} test)")


if __name__ == "__main__":
    main()
