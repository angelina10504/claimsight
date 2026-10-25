"""
Run claims through the full ClaimSight Flow and compare each outcome with the
route a correct adjuster would take.

Run:  uv run python run_flow.py CLM-5000                one claim, asks you at the human gate
      uv run python run_flow.py CLM-5004 CLM-5006       several claims
      uv run python run_flow.py --demo                  the six-claim demo set
      uv run python run_flow.py --plot                  save the Flow diagram as HTML, then exit

Options:
      --fresh      rerun the Intake crew even if a saved intake exists
      --no-input   do not stop at the human gate; queue those claims instead
      --verbose    show the full agent logs
"""

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from claim_flow import FRAUD_THRESHOLD, HIGH_VALUE, ClaimFlow   # noqa: E402

CLAIMS = ROOT / "data" / "claims"
DEMO_SET = ["CLM-5000", "CLM-5004", "CLM-5005", "CLM-5006", "CLM-5007", "CLM-5008"]
PAUSE_SECONDS = 20


def expected_route(claim: dict) -> str:
    """The route the Flow's rules would pick if every agent were right."""
    truth = claim["ground_truth"]
    if not truth["is_covered"]:
        return "reject"
    if truth["is_fraudulent"]:
        return "human_review"
    if truth["expected_payout"] > HIGH_VALUE:
        return "human_review"
    return "auto_approve"


def main() -> None:
    args = sys.argv[1:]

    if "--plot" in args:
        # CrewAI writes the diagram to a temp folder; copy it into docs/flow
        temp_html = Path(ClaimFlow().plot("claimsight_flow.html", show=False))
        out_dir = ROOT / "docs" / "flow"
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in temp_html.parent.iterdir():
            shutil.copy(f, out_dir / f.name)
        print(f"Flow diagram saved: {out_dir / temp_html.name}")
        return

    claim_ids = [a for a in args if a.startswith("CLM-")] or (DEMO_SET if "--demo" in args else [])
    if not claim_ids:
        sys.exit(__doc__)

    rows = []
    for n, claim_id in enumerate(claim_ids):
        path = CLAIMS / f"{claim_id}.json"
        if not path.exists():
            print(f"skip {claim_id}: no such claim")
            continue
        claim = json.loads(path.read_text())
        print("\n" + "=" * 70)
        print(f"FLOW  {claim_id}")
        print("=" * 70)

        flow = ClaimFlow()
        try:
            flow.kickoff(inputs={
                "claim_id": claim_id,
                "use_saved_intake": "--fresh" not in args,
                "interactive": "--no-input" not in args,
                "verbose": "--verbose" in args,
            })
            s = flow.state
            rows.append({"claim_id": claim_id, "pattern": claim["ground_truth"]["fault_type"],
                         "route": s.route, "expected": expected_route(claim),
                         "status": s.status, "payout": s.final_payout,
                         "expected_payout": claim["ground_truth"]["expected_payout"]})
        except Exception as exc:                       # noqa: BLE001
            print(f"\n{claim_id} FAILED: {exc}")
            rows.append({"claim_id": claim_id, "pattern": claim["ground_truth"]["fault_type"],
                         "error": str(exc)})

        if n < len(claim_ids) - 1:
            time.sleep(PAUSE_SECONDS)

    print("\n" + "=" * 70)
    print(f"FLOW SUMMARY   (human review if fraud >= {FRAUD_THRESHOLD} or payout > Rs {HIGH_VALUE:,})")
    print("=" * 70)
    print(f"{'claim':<10} {'pattern':<20} {'route':<13} {'expected':<13} {'status':<21} payout")
    for r in rows:
        if "error" in r:
            print(f"{r['claim_id']:<10} {r['pattern']:<20} FAILED: {r['error'][:40]}")
            continue
        mark = "" if r["route"] == r["expected"] else "  <- differs"
        print(f"{r['claim_id']:<10} {r['pattern']:<20} {r['route']:<13} {r['expected']:<13} "
              f"{r['status']:<21} Rs {r['payout']:,.2f}{mark}")
    done = [r for r in rows if "error" not in r]
    if done:
        right = sum(r["route"] == r["expected"] for r in done)
        print("-" * 70)
        print(f"routes correct      {right}/{len(done)}")
        print("full trail of each claim: data/decisions/<claim>.json")


if __name__ == "__main__":
    main()
