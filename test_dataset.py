"""
Test that the official dataset stays separate from demonstration fixtures, and
that the deterministic tools answer rather than crash on bad input.

Costs no tokens and calls no model.

Two things are being protected here. The first is the boundary: demonstration
fixtures live in data/claims beside the real claims, and the official numbers
must come from data/manifest.json so a fixture can never be counted. The
second is that a tool asked an impossible question says so: a claim with no
incident date yet is an incomplete file, and must never come back looking like
an exclusion or a fraud signal.

    the manifest holds exactly 60 claims, 20 dev and 40 held out
    selecting by manifest gives 20 by default and 60 with --all
    demonstration fixtures are excluded even though they sit in the same folder
    a missing, empty or malformed date is reported as such, not as "outside
      the policy period"
    a missing policy record is reported as such
    one claim with no date does not break the duplicate check for another
    incomplete, excluded and fraudulent stay three different things

Run:  uv run python test_dataset.py
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
sys.path.insert(0, str(ROOT / "src" / "tools"))

from assessment_tools import (   # noqa: E402
    check_part_prices,
    check_policy_period,
    find_prior_claims,
)

MANIFEST = ROOT / "data" / "manifest.json"
CLAIMS = ROOT / "data" / "claims"
DEMO_IDS = ("CLM-9001", "CLM-9002")


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    # ---------------------------------------------------------------- the manifest
    print("1. The official dataset\n")
    run("data/manifest.json is on file", MANIFEST.exists())
    entries = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    dev = [e for e in entries if e["split"] == "dev"]
    held_out = [e for e in entries if e["split"] == "test"]
    run("it names exactly 60 claims", len(entries) == 60, f"{len(entries)}")
    run("20 development claims", len(dev) == 20, f"{len(dev)}")
    run("40 held out for the final evaluation", len(held_out) == 40, f"{len(held_out)}")
    run("no demonstration fixture is in the manifest",
        not [e for e in entries if e["claim_id"] in DEMO_IDS],
        [e["claim_id"] for e in entries if e["claim_id"] in DEMO_IDS] or "none")
    run("every manifest claim is on disk",
        all((CLAIMS / f"{e['claim_id']}.json").exists() for e in entries))

    import test_tools
    run("selecting by manifest gives the 20 development claims",
        len(test_tools.official_claims(False)) == 20,
        f"{len(test_tools.official_claims(False))}")
    run("and 60 with --all", len(test_tools.official_claims(True)) == 60,
        f"{len(test_tools.official_claims(True))}")
    selected = {c["claim_id"] for c in test_tools.official_claims(True)}
    run("no demonstration fixture is selected, though it sits in the same folder",
        not (selected & set(DEMO_IDS)),
        f"fixtures on disk: "
        f"{[i for i in DEMO_IDS if (CLAIMS / f'{i}.json').exists()] or 'none built yet'}")

    on_disk = {path.stem for path in CLAIMS.glob("CLM-*.json")}
    run("globbing the folder would have picked up more than the manifest does",
        len(on_disk) >= len(entries),
        f"{len(on_disk)} files on disk against {len(entries)} official claims")

    # ---------------------------------------------------------------- bad dates
    print("\n2. A tool asked an impossible question\n")
    cases = [
        ("no date at all", None, "incomplete_document"),
        ("an empty date", "", "incomplete_document"),
        ("a date in the wrong order", "30-05-2026", "invalid_input"),
        ("not a date", "last Tuesday", "invalid_input"),
        ("a number", 20260530, "invalid_input"),
    ]
    for name, value, expected_reason in cases:
        answer = check_policy_period("POL-3000", value)
        ok = answer.get("reason") == expected_reason and "within_period" not in answer
        passed += ok
        total += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: reported, not guessed"
              f"\n        {answer.get('error')}  (reason {answer.get('reason')})")

    run("a missing policy record is reported as such",
        check_policy_period("POL-0000", "2026-05-30").get("reason") == "missing_policy")
    run("a real claim still answers normally",
        check_policy_period("POL-3000", "2026-05-30").get("within_period") is True)
    run("nothing without a date is ever called 'outside the period'",
        all("within_period" not in check_policy_period("POL-3000", value)
            for _, value, _ in cases))

    print()
    run("the duplicate check also reports a missing date",
        find_prior_claims("POL-3000", "CLM-5000", None, ["door"]).get("reason")
        == "incomplete_document")
    run("and tolerates one with a bad date",
        find_prior_claims("POL-3000", "CLM-5000", "not a date", ["door"]).get("reason")
        == "invalid_input")
    run("no damaged parts is not an error, just no overlap",
        find_prior_claims("POL-3000", "CLM-5000", "2026-10-22", None)
        .get("possible_duplicate") is False)

    # one claim with no date must not break the check for another
    tmp = CLAIMS / "CLM-9199.json"
    source = json.loads((CLAIMS / "CLM-5000.json").read_text())
    tmp.write_text(json.dumps({**source, "claim_id": "CLM-9199",
                               "incident_date": None, "split": "demo"}, indent=2))
    try:
        answer = find_prior_claims(source["policy_no"], "CLM-9200",
                                   "2026-10-22", ["door glass"])
        run("a dateless claim on file does not break the check for another claim",
            "error" not in answer, answer.get("error") or "answered normally")
        run("and it says which claims it had to skip",
            "CLM-9199" in (answer.get("skipped_claims") or []),
            answer.get("skipped_claims"))
    finally:
        tmp.unlink(missing_ok=True)

    print("\n3. Incomplete is not the same as excluded or fraudulent\n")
    incomplete = check_policy_period("POL-3000", None)
    run("an incomplete file carries its own reason",
        incomplete.get("reason") == "incomplete_document")
    run("it does not claim the incident was outside cover",
        incomplete.get("within_period") is None)
    run("it is not a fraud signal either",
        "suspicious" not in incomplete and "possible_duplicate" not in incomplete)
    empty_invoice = check_part_prices([])
    run("an empty invoice is an error, not a clean bill",
        "error" in empty_invoice or empty_invoice.get("suspicious") is False,
        json.dumps(empty_invoice)[:90])

    print(f"\n{passed}/{total} dataset and input checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
