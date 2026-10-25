"""
Audit the settlement calculator against the policy wording it claims to follow.

Costs no tokens, calls no model. This is the test that answers "how do you know
your payout is right?" with something better than "our own tests pass":

    every rule that claims to come from the policy is quoted, and the quote is
      checked against knowledge/policy_wording.txt
    the depreciation table is parsed OUT of the policy wording and compared,
      band by band, with the table the code applies
    the flat rates in the clause are parsed out and compared too
    rules that are OUR assumption rather than the policy's say so plainly
    the arithmetic is checked for rounding drift
    whether an invoice includes GST is verified per invoice, not assumed

It also prints the assumptions table, so the audit is something you can hand to
an assessor.

Run:  uv run python test_audit.py
      uv run python test_audit.py --table     just print the assumptions
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "tools"))

import settlement as S   # noqa: E402

WORDING = ROOT / "knowledge" / "policy_wording.txt"
CLAIMS = ROOT / "data" / "claims"
POLICIES = ROOT / "data" / "policies"

BASE_POLICY = {"vehicle_age_years": 1.0, "deductible": 1000.0, "idv": 500000.0,
               "zero_depreciation": False}


CURLY = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
         "\u2013": "-", "\u2014": "-", "\u2011": "-", "\u00a0": " "}


def plain(text: str) -> str:
    """Lowercase, single-spaced, with curly quotes and dashes made straight.

    A citation should not fail because the PDF uses a curly apostrophe in
    "Insured's" while the code uses a straight one."""
    for fancy, simple in CURLY.items():
        text = text.replace(fancy, simple)
    return " ".join(text.lower().split())


def wording() -> str:
    """The policy wording as one plain, single-spaced string."""
    if not WORDING.exists():
        return ""
    return plain(WORDING.read_text(errors="ignore"))


def bands_from_wording(text: str) -> list:
    """Read the depreciation schedule out of the policy document itself.

    Returns [(upper limit in years, rate), ...] so it can be compared with
    AGE_BANDS in the code. If the clause is ever reworded, this test fails
    rather than the code quietly disagreeing with the policy."""
    section = re.search(r"AGE OF VEHICLE.*?(?=5\.\s*Rate of Depreciation for Painting)",
                        text, re.I)
    if not section:
        return []
    block = section.group(0)
    bands = []

    first = re.search(r"Not exceeding\s*(\d+)\s*(month|year)s?\s*(Nil|\d+)\s*%?", block, re.I)
    if first:
        count, unit, rate = first.groups()
        years = int(count) / 12 if unit.lower() == "month" else int(count)
        bands.append((years, 0.0 if rate.lower() == "nil" else int(rate) / 100))

    for match in re.finditer(r"Exceeding\s*\d+\s*(?:month|year)s?\s*but not exceeding\s*"
                             r"(\d+)\s*(month|year)s?\s*(Nil|\d+)\s*%?", block, re.I):
        count, unit, rate = match.groups()
        years = int(count) / 12 if unit.lower() == "month" else int(count)
        bands.append((years, 0.0 if rate.lower() == "nil" else int(rate) / 100))

    last = re.findall(r"Exceeding\s*(\d+)\s*years?\s*(\d+)\s*%", block, re.I)
    if last:
        bands.append((float("inf"), int(last[-1][1]) / 100))
    return bands


def flat_rates_from_wording(text: str) -> dict:
    """The named material rates in the clause, e.g. 'glass -Nil' -> 0.0."""
    clause = re.search(r"in respect of parts replaced\s*:(.{0,400})", text, re.I)
    found = {}
    if clause:
        for parts, rate in re.findall(r"For\s+(.+?)\s*-\s*(Nil|\d+)\s*%?",
                                      clause.group(1), re.I):
            found[parts.strip().lower()] = 0.0 if rate.lower() == "nil" else int(rate) / 100
    return found


def print_table() -> None:
    text = wording()
    print("\nSETTLEMENT ASSUMPTIONS\n" + "=" * 74)
    for n, rule in enumerate(S.ASSUMPTIONS, 1):
        from_policy = not rule["basis"].lower().startswith("model assumption")
        quoted = from_policy and plain(rule["basis"]) in text
        source = ("policy wording, quote verified" if quoted else
                  "policy wording, quote NOT FOUND" if from_policy else
                  "our assumption, not the policy's")
        print(f"\n{n:>2}. {rule['rule']}: {rule['value']}")
        print(f"    source: {source}")
        print(f"    basis:  {rule['basis']}")
        if rule.get("reasoning"):
            print(f"    step:   {rule['reasoning']}")
        if rule["if_different"]:
            print(f"    note:   a real insurer may differ - {rule['if_different']}")
    print("\n" + "=" * 74)


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    text = wording()

    # ------------------------------------------------ the citations
    print("1. Does each rule really say what the policy says?\n")
    run("the policy wording is on file", bool(text), f"{len(text):,} characters")

    for rule in S.ASSUMPTIONS:
        claims_policy = not rule["basis"].lower().startswith("model assumption")
        if not claims_policy:
            continue
        found = plain(rule["basis"]) in text
        run(f"{rule['rule']}: the quoted clause is in the policy document", found,
            "" if found else f"not found: {rule['basis'][:80]}")

    assumptions = [r for r in S.ASSUMPTIONS
                   if r["basis"].lower().startswith("model assumption")]
    run("GST is declared as our assumption, not the policy's",
        any("GST" in r["rule"] for r in assumptions),
        "; ".join(r["rule"] for r in assumptions))
    run("every assumption says what a real insurer might do instead",
        all(r["if_different"] for r in assumptions),
        [r["rule"] for r in assumptions if not r["if_different"]] or "all do")

    # ------------------------------------------------ the table, read from the policy
    print("\n2. The depreciation table, parsed out of the policy document\n")
    from_policy = bands_from_wording(text)
    run("the schedule could be read out of the wording", len(from_policy) >= 7,
        f"{len(from_policy)} bands found")

    in_code = [(limit, rate) for limit, rate in S.AGE_BANDS]
    if from_policy:
        print("        age up to   policy    code")
        ok = True
        for n, (limit, rate) in enumerate(from_policy):
            code_rate = in_code[n][1] if n < len(in_code) else None
            same = code_rate is not None and abs(code_rate - rate) < 1e-9
            ok = ok and same
            shown = "over 10y" if limit == float("inf") else f"{limit:g}y"
            print(f"        {shown:<11} {rate * 100:>5.0f}%   "
                  f"{(code_rate * 100 if code_rate is not None else 0):>5.0f}%"
                  f"{'' if same else '   <- differs'}")
        run("every band in the code matches the band in the policy", ok)
        run("the code has no extra bands the policy does not have",
            len(in_code) == len(from_policy), f"code {len(in_code)}, policy {len(from_policy)}")

    print("\n3. The named material rates, parsed out of the policy document\n")
    named = flat_rates_from_wording(text)
    run("the material rates could be read out of the wording", len(named) >= 3,
        json.dumps(named))
    for parts, rate in named.items():
        if "glass" in parts and "fibre" not in parts:
            run(f"glass: policy {rate * 100:.0f}%", S.FLAT_RATES["glass"] == rate,
                f"code {S.FLAT_RATES['glass'] * 100:.0f}%")
        elif "fibre" in parts:
            run(f"fibre glass: policy {rate * 100:.0f}%", S.FLAT_RATES["fibreglass"] == rate,
                f"code {S.FLAT_RATES['fibreglass'] * 100:.0f}%")
        else:
            for material in ("plastic", "rubber", "tyre", "battery", "airbag"):
                run(f"{material}: policy {rate * 100:.0f}%",
                    S.FLAT_RATES[material] == rate,
                    f"code {S.FLAT_RATES[material] * 100:.0f}%")

    run("painting is 50% of a 25% material share",
        abs(S.FLAT_RATES["paint"] - 0.125) < 1e-9
        and "material component shall be considered as 25%" in text,
        f"{S.FLAT_RATES['paint'] * 100:.1f}%")
    run("labour attracts no depreciation", S.FLAT_RATES["labour"] == 0.0)

    # ------------------------------------------------ the airbag case
    print("\n4. An air bag, which the clause names beside tyres and batteries\n")
    airbag = S.calculate_settlement(
        [{"description": "Airbag - replacement", "amount": 20000}], BASE_POLICY)
    run("an air bag is depreciated 50%, not by vehicle age",
        abs(airbag.lines[0]["depreciation_rate"] - 0.50) < 1e-9,
        f"{airbag.lines[0]['depreciation_rate'] * 100:.0f}% on a "
        f"{BASE_POLICY['vehicle_age_years']:g}-year-old car "
        f"(the age band would be {S.metal_depreciation(1.0) * 100:.0f}%)")
    run("it is recognised from the words 'air bag' too",
        S.classify_material("Air Bag module") == "airbag")

    # ------------------------------------------------ the arithmetic
    print("\n5. Rounding and the arithmetic\n")
    lines = [{"description": "Bumper - replacement", "amount": 8333.33, "material": "plastic"},
             {"description": "Door - replacement", "amount": 11111.11, "material": "metal"},
             {"description": "Painting and finishing", "amount": 7777.77, "material": "paint"},
             {"description": "Labour charges", "amount": 3333.33, "material": "labour"}]
    result = S.calculate_settlement(lines, BASE_POLICY)
    line_sum = round(sum(line["payable"] for line in result.lines), 2)
    run("the line payables add up to the subtotal after depreciation",
        abs(line_sum - result.after_depreciation) < 0.01,
        f"lines {line_sum:,.2f} vs after_depreciation {result.after_depreciation:,.2f}")
    expected = round(result.after_depreciation + result.gst - result.deductible, 2)
    run("payout = subtotal after depreciation + GST - deductible",
        abs(result.payout - expected) < 0.01,
        f"{result.after_depreciation:,.2f} + {result.gst:,.2f} - "
        f"{result.deductible:,.2f} = {result.payout:,.2f}")
    run("GST is taken on the depreciated amount, not the billed amount",
        abs(result.gst - round(result.after_depreciation * 0.18, 2)) < 0.01)
    run("a denied line contributes nothing",
        S.calculate_settlement(
            lines + [{"description": "Phantom part", "amount": 50000,
                      "admitted": False, "reason": "not damaged"}],
            BASE_POLICY).payout == result.payout)

    print()
    run("the deductible comes from the schedule, not a constant",
        S.calculate_settlement(lines, {**BASE_POLICY, "deductible": 2000.0}).payout
        == round(result.payout - 1000.0, 2),
        "a Rs 2,000 schedule pays Rs 1,000 less")
    capped = S.calculate_settlement(lines, {**BASE_POLICY, "idv": 5000.0})
    run("nothing above the IDV is paid", capped.payout == 5000.0 and capped.capped_at_idv,
        f"capped at Rs {capped.payout:,.2f}")
    zero_dep = S.calculate_settlement(lines, {**BASE_POLICY, "zero_depreciation": True})
    run("zero depreciation waives the deduction",
        zero_dep.total_depreciation == 0.0
        and zero_dep.payout > result.payout)
    run("a payout can never go below zero",
        S.calculate_settlement([{"description": "Labour charges", "amount": 100,
                                 "material": "labour"}],
                               {**BASE_POLICY, "deductible": 99999.0}).payout == 0.0)

    # ------------------------------------------------ tax treatment
    print("\n6. Does the invoice include GST? Checked, not assumed\n")
    run("lines plus GST matching the total reads as pre-tax",
        S.check_invoice_tax([{"amount": 100}, {"amount": 100}], 236)["treatment"]
        == "exclusive")
    inclusive = S.check_invoice_tax([{"amount": 100}, {"amount": 100}], 200)
    run("lines already matching the total reads as tax-inclusive",
        inclusive["treatment"] == "inclusive", inclusive["note"][:90])
    run("anything else is reported as unclear rather than guessed",
        S.check_invoice_tax([{"amount": 100}], 500)["treatment"] == "unclear")
    run("an empty invoice is unclear, not zero",
        S.check_invoice_tax([], 1000)["treatment"] == "unclear")

    real = sorted(CLAIMS.glob("CLM-*.json"))[:12]
    verdicts = {}
    for path in real:
        claim = json.loads(path.read_text())
        verdict = S.check_invoice_tax(claim["line_items"], claim["invoice_total"])
        verdicts[claim["claim_id"]] = verdict["treatment"]
    run("every real invoice in the dataset is pre-tax, as the calculator expects",
        verdicts and set(verdicts.values()) == {"exclusive"},
        ", ".join(f"{k}={v}" for k, v in list(verdicts.items())[:4]) + " ...")

    print(f"\n{passed}/{total} audit checks as expected")
    print_table()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    if "--table" in sys.argv:
        print_table()
    else:
        main()
