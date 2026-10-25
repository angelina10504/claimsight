"""
Print a claim's monitoring log as a readable timeline.

Run:  uv run python show_log.py CLM-5004
      uv run python show_log.py CLM-5004 --full     (do not shorten outputs)
"""

import json
import sys
from pathlib import Path

LOGS = Path(__file__).parent / "data" / "logs"


def line(entry: dict, full: bool) -> str:
    def cut(text, n=110):
        text = str(text).replace("\n", " ")
        return text if full or len(text) <= n else text[:n] + " ..."

    e = entry["event"]
    if e == "flow":
        return f"FLOW    {entry['message']}"
    if e == "tool_call":
        mark = "  !! tool returned an error" if entry.get("tool_error") else ""
        return f"TOOL    {entry['tool']}  in: {cut(entry['input'], 70)}\n" \
               f"{'':16}out: {cut(entry['result'])}{mark}"
    if e == "agent_answer":
        return f"ANSWER  {cut(entry['output'])}"
    if e == "task_done":
        typed = f"  -> {entry['structured']}" if entry.get("structured") else ""
        return f"TASK    done by {entry['agent']} in {entry['seconds_since_previous_task']}s{typed}\n" \
               f"{'':16}out: {cut(entry['output'])}"
    if e == "crew_done":
        tokens = f", {entry['total_tokens']:,} tokens" if entry.get("total_tokens") else ""
        return f"CREW    {entry['crew']} crew finished in {entry['seconds']}s{tokens}"
    if e == "llm_call_failed":
        return f"LLMFAIL {entry['kind']}: {cut(entry['error'], 80)}"
    if e in ("tool_error", "error"):
        where = entry.get("where") or entry.get("tool")
        return f"ERROR   {where}: {cut(entry['error'])}"
    if e == "run_started":
        return "START"
    if e == "run_finished":
        return (f"END     status={entry.get('status')}  route={entry.get('route')}  "
                f"payout=Rs {entry.get('final_payout', 0):,.2f}\n"
                f"{'':8}{entry['total_seconds']}s total, {entry['tasks']} tasks, "
                f"{entry['tool_calls']} tool calls, {entry['retried_llm_failures']} failed LLM calls, "
                f"{entry['errors']} errors")
    return f"{e.upper():<8}{cut(entry)}"


def main() -> None:
    args = sys.argv[1:]
    ids = [a for a in args if a.startswith("CLM-")]
    if not ids:
        sys.exit(__doc__)
    path = LOGS / f"{ids[0]}.jsonl"
    if not path.exists():
        sys.exit(f"No log for {ids[0]} yet. Run: uv run python run_flow.py {ids[0]}")

    print(f"Monitoring log  {ids[0]}   ({path.relative_to(Path(__file__).parent)})\n")
    for raw in path.read_text().splitlines():
        entry = json.loads(raw)
        print(f"{entry['elapsed_s']:>7.1f}s  {line(entry, '--full' in args)}")


if __name__ == "__main__":
    main()
