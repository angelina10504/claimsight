"""
Test that no personal data reaches a model.

Costs no tokens. The important part is not the regex checks at the end but the
first section: it runs the real Intake crew with a stand-in model that records
every message it is handed, and then looks for the policyholder's name, phone
and address in what was actually sent. A regex test can pass while the payload
still leaks, so the payload is what gets checked.

    the claimant's name never appears in any message sent to the model
    nor their phone number, nor their address
    the registration, line items and amounts DO survive the cleaning
    the policy handed to the agents has no name, phone or address in it
    labelled fields on the document lose their values
    a line that is a postal address goes; a money line does not
    if anything personal survives, the claim stops instead of being sent

Run:  uv run python test_privacy.py
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import guardrails   # noqa: E402

POLICIES = ROOT / "data" / "policies"
CLAIMS = ROOT / "data" / "claims"
CLAIM_ID = "CLM-5000"

INVOICE_TEMPLATE = """{garage} Auto Works
{garage_addr}
GSTIN: 06AB1234C1Z5 | Ph: 09812345678
Invoice No. INV/833/26 Date {invoice_date}
Customer {customer} Vehicle {make} {model}
Reg. No. {registration} Job Card JC/1234
{rows}
Subtotal {subtotal:,}
GST @ 18% {gst:,}
Total Payable {total:,}
Payment due on delivery. This is a computer-generated invoice."""


def build_invoice(claim: dict, policy: dict) -> str:
    """An invoice in the same shape generate_data.py renders, carrying the real
    policyholder's name. Used when the PDF cannot be read, so the test works
    the same either way."""
    rows = "\n".join(f"{n} {item['description']} {item['amount']:,}"
                     for n, item in enumerate(claim["line_items"], 1))
    subtotal = sum(item["amount"] for item in claim["line_items"])
    gst = round(subtotal * 0.18)
    surname = policy["insured_name"].split()[-1]
    return INVOICE_TEMPLATE.format(
        garage=surname, garage_addr="88 Industrial Area Road, Phase 2, Gurgaon, 122016",
        invoice_date=claim.get("incident_date") or "2026-10-22",
        customer=policy["insured_name"], make=policy["make"], model=policy["model"],
        registration=policy["registration"], rows=rows,
        subtotal=subtotal, gst=gst, total=subtotal + gst)


def recording_llm(seen: list):
    """A stand-in model that records every payload and returns usable answers,
    so the real crew runs end to end without a single token spent."""
    from crewai.llms.base_llm import BaseLLM

    invoice = json.dumps({"invoice_no": "INV/833/26", "invoice_date": "2026-10-22",
                          "garage_name": "Auto Works", "registration": "HR45GY5012",
                          "line_items": [], "subtotal": 0, "gst": 0, "total": 0})
    intake = json.dumps({"claim_id": CLAIM_ID, "policy_no": "POL-3000",
                         "invoice": json.loads(invoice), "is_complete": True,
                         "missing_fields": [], "damaged_parts": []})

    class Recording(BaseLLM):
        turn: int = 0

        def supports_function_calling(self):
            return False

        def call(self, messages, tools=None, callbacks=None, available_functions=None, **kw):
            seen.append(json.dumps(messages, default=str))
            self.turn += 1
            return f"Thought: done.\nFinal Answer: {intake if self.turn > 1 else invoice}"

    return Recording(model="recording")


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    claim = json.loads((CLAIMS / f"{CLAIM_ID}.json").read_text())
    policy = json.loads((POLICIES / f"{claim['policy_no']}.json").read_text())
    name = policy["insured_name"]
    surname = name.split()[-1]
    phone = policy["phone"]
    address = policy["address"]

    # ---------------------------------------------------------------- the payload
    print("1. What the model is actually sent, with the real Intake crew\n")
    print(f"   policyholder on file: {name} | {phone} | {address[:40]}...\n")

    import crews.intake_crew as intake_crew
    from tools import claim_tools

    real_pdf = claim_tools.ReadInvoicePDF()._run(CLAIM_ID)
    using_pdf = not real_pdf.startswith("ERROR") and surname.lower() in real_pdf.lower()
    invoice_text = real_pdf if using_pdf else build_invoice(claim, policy)
    print(f"   invoice source: {'the real PDF' if using_pdf else 'a copy of the same template'}\n")
    claim_tools.ReadInvoicePDF._run = lambda self, _claim_id: invoice_text

    run("the invoice really does carry the policyholder's name before cleaning",
        surname.lower() in invoice_text.lower())

    seen: list = []
    intake_crew.get_llm = lambda *a, **k: recording_llm(seen)
    try:
        intake_crew.run_intake(CLAIM_ID, verbose=False)
    except Exception as exc:                       # noqa: BLE001
        print(f"   (the crew ended with: {type(exc).__name__}; the payloads still count)\n")

    payload = "\n".join(seen).lower()
    run("the crew was given something to read", bool(seen),
        f"{len(seen)} message payload(s) captured")
    run("the policyholder's full name is not in any payload", name.lower() not in payload,
        f"looked for {name!r}")
    run("nor their surname on its own", surname.lower() not in payload,
        f"looked for {surname!r}")
    digits = "".join(ch for ch in phone if ch.isdigit())
    run("nor their phone number, in any spacing",
        phone.lower() not in payload and (len(digits) < 8 or digits not in
                                          "".join(c for c in payload if c.isdigit())),
        f"looked for {phone!r}")
    worst = max(address.split(","), key=len).strip().lower()
    run("nor the longest part of their address", worst not in payload,
        f"looked for {worst!r}")
    run("no postal code from the documents survives",
        "122016" not in payload and "122001" not in payload)

    print()
    run("the registration still reached the agents",
        policy["registration"].lower() in payload, policy["registration"])
    amounts = [f"{item['amount']:,}" for item in claim["line_items"]]
    run("every billed amount still reached the agents",
        all(a in payload for a in amounts), ", ".join(amounts))
    run("the line descriptions still reached the agents",
        all(item["description"].split()[0].lower() in payload
            for item in claim["line_items"]))
    run("the invoice number still reached the agents", "inv/833/26" in payload)

    # ---------------------------------------------------------------- the inputs
    print("\n2. What fetch_inputs hands over\n")
    inputs = intake_crew.fetch_inputs(CLAIM_ID)
    run("the policy passed on has no name, phone or address",
        not any(field in inputs["policy"] for field in intake_crew.PII_FIELDS),
        inputs["policy"][:60].replace("\n", " ") + " ...")
    run("the invoice text passed on has no trace of the claimant",
        not guardrails.contains_known_pii(inputs["invoice_text"], policy),
        str(guardrails.contains_known_pii(inputs["invoice_text"], policy)))
    run("the submission passed on has none either",
        not guardrails.contains_known_pii(inputs["submission"], policy))

    import claim_flow
    saved = claim_flow.INTAKE / f"{CLAIM_ID}.json"
    if saved.exists():
        for_assessment = json.dumps(claim_flow.assessment_inputs(
            json.loads(saved.read_text())))
        run("what the Assessment crew is given has none either",
            not guardrails.contains_known_pii(for_assessment, policy),
            str(guardrails.contains_known_pii(for_assessment, policy)))

    # ---------------------------------------------------------------- the rules
    print("\n3. The cleaning rules themselves\n")
    made_up = {"insured_name": "Ravi Kumar", "phone": "+91 98765 43210",
               "address": "12 Ashoka Road, Sector 14, Gurgaon, 122001"}
    document = ("Kumar Auto Works\n"
                "88 Industrial Area Road, Phase 2, Gurgaon, 122016\n"
                "GSTIN: 06AB1234C1Z5 | Ph: 09812345678\n"
                "Customer Ravi Kumar Vehicle Maruti Swift\n"
                "Reg. No. HR45GY5012 Job Card JC/1234\n"
                "1 Door Glass - replacement 4,880\n"
                "Total Payable 123456\n")
    clean, report = guardrails.sanitise_document(document, made_up)
    run("the customer's name goes", "ravi kumar" not in clean.lower(), report)
    run("a labelled GSTIN and phone go",
        "06AB1234C1Z5" not in clean and "09812345678" not in clean)
    run("a postal address line goes", "122016" not in clean)
    run("a six-digit amount is NOT mistaken for a postal code",
        "123456" in clean, [line for line in clean.splitlines() if "123" in line])
    run("the vehicle and registration stay",
        "HR45GY5012" in clean and "Maruti Swift" in clean)
    run("a redaction marker is not then read as a field label",
        "[withheld-postal]" in clean and "withheld-postal]" not in clean.split("[withheld-postal]")[0])

    nothing_known = guardrails.sanitise_document("Customer Ravi Kumar Ph 09812345678", None)[0]
    run("it still removes labelled fields with no policy to compare against",
        "Ravi Kumar" not in nothing_known, nothing_known)
    found = guardrails.find_pii("call 9876543210 or +91 9876543211")
    run("a reported phone number is the number, not a fragment of the pattern",
        found.get("phone") == ["9876543210", "+91 9876543211"], found)

    print("\n4. The last line of defence\n")
    original = guardrails.sanitise_document
    guardrails.sanitise_document = lambda text, policy=None: (text, {})
    try:
        guardrails.safe_for_cloud(document, made_up, what="a test document")
        run("a document that still carries a name is refused", False,
            "it was passed through")
    except guardrails.PrivacyError as exc:
        run("a document that still carries a name is refused", True, str(exc)[:110])
    finally:
        guardrails.sanitise_document = original

    print(f"\n{passed}/{total} privacy checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
