"""
Print the evidence report for a claim that has been assessed.

The report says why the claim was decided the way it was, and traces each
finding to the document, policy clause or tool result behind it. It is built
from the claim's saved state and monitoring log, so it costs no tokens and
can be produced long after the run.

Run:  uv run python show_report.py CLM-5004
      uv run python show_report.py CLM-5004 --json        save data/reports/<id>.json
      uv run python show_report.py CLM-5004 --markdown    save data/reports/<id>.md
      uv run python show_report.py --all                   every claim decided so far
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import reporting   # noqa: E402


def claim_ids() -> list:
    """Every claim with a saved decision or sitting in the review queue."""
    found = set()
    for folder in (reporting.DECISIONS, reporting.REVIEW_QUEUE):
        if folder.exists():
            found.update(path.stem for path in folder.glob("CLM-*.json"))
    return sorted(found)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    ids = claim_ids() if "--all" in flags else args
    if not ids:
        print("Which claim? e.g. uv run python show_report.py CLM-5004")
        print("Claims assessed so far: " + (", ".join(claim_ids()) or "none yet"))
        sys.exit(1)

    missing = []
    for claim_id in ids:
        report = reporting.for_claim(claim_id)
        if report is None:
            missing.append(claim_id)
            continue
        print(reporting.render(report))
        if "--json" in flags:
            print(f"saved {reporting.save(report)}")
        if "--markdown" in flags:
            print(f"saved {reporting.save(report, markdown=True)}")
        print()

    if missing:
        print(f"No saved decision for: {', '.join(missing)}")
        print("Run the claim first: uv run python run_flow.py " + missing[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
