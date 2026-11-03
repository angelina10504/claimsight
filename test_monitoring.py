"""
Per-request monitoring isolation (src/monitoring.py).

A deployed A2A server assesses several claims at once. Monitoring used to keep
the active claim in a module global, so two claims running together would write
each other's events into the wrong log. The monitor now lives in a context
variable carried per request, and current() resolves it by context, then by the
thread the request runs on, then (for one claim at a time) by the sole running
claim. These tests prove the isolation, driving every write through the SAME
module-level entry points the crews use - monitoring.current(), on_step,
on_task_done - because those are where a wrong resolution would leak; calling a
monitor object directly could never cross claims and would prove nothing.

  1. two concurrent claims     each run as an asyncio Flow (steps are separate
                               tasks on one thread, as CrewAI runs them) on its
                               own thread, exactly like the A2A executor. Writes
                               interleave; each claim's log stays its own.
  2. fallback, one claim        a worker thread that saw neither the context var
                                nor the request thread still finds the only
                                running claim.
  3. fallback off, two claims   the same worker thread resolves to NEITHER
                                claim rather than guess - safe, never wrong.

No provider tokens, no network. Runs under CLAIMSIGHT_OFFLINE=1.

Run:  uv run python test_monitoring.py
"""

import os
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import asyncio
import json
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import monitoring   # noqa: E402


def section(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def read_log(claim_id: str) -> list:
    path = monitoring.LOGS / f"{claim_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# A tool call and an agent answer shaped the way CrewAI's step callback expects:
# an AgentAction has .tool/.tool_input/.result, an AgentFinish has .output.
def fake_action(caller: str, i: int):
    return types.SimpleNamespace(tool="probe_tool", tool_input=f"from:{caller}#{i}", result="ok")


def fake_task_output(caller: str, i: int):
    return types.SimpleNamespace(agent="analyst", description=f"task from:{caller}#{i}",
                                 raw="done", pydantic=None)


def fake_finish(caller: str, i: int):
    return types.SimpleNamespace(output=f"answer from:{caller}#{i}")


# ------------------------------------------------------------ 1
def test_two_concurrent_claims() -> bool:
    section("1. TWO CONCURRENT CLAIMS (asyncio flows on separate threads)")
    claims = ["CLM-CONC1", "CLM-CONC2"]
    rounds = 40
    start = threading.Barrier(len(claims))      # release both flows at once, so they overlap
    wrong_resolution = {}                        # claim -> first foreign claim_id seen in its steps

    def check(claim_id: str) -> None:
        got = monitoring.current()
        if got is None or got.claim_id != claim_id:
            wrong_resolution.setdefault(claim_id, got and got.claim_id)

    async def flow(claim_id: str) -> None:
        # Step 1 (intake): create the monitor. Each step is its own asyncio task,
        # as CrewAI's Flow runs @start/@listen, so a later step's task will not
        # inherit the context var set here - resolution must fall through to the
        # thread the flow runs on.
        async def intake():
            monitoring.ClaimMonitor(claim_id, provider="test", model="none")
            check(claim_id)

        async def assess(i: int):
            check(claim_id)
            monitoring.on_step(fake_action(claim_id, i))         # -> tool_call event
            monitoring.on_task_done(fake_task_output(claim_id, i))  # -> task_done event
            monitoring.on_step(fake_finish(claim_id, i))         # -> agent_answer event

        await asyncio.create_task(intake())
        start.wait()                                             # both monitors now live
        for i in range(rounds):
            await asyncio.create_task(assess(i))                 # fresh task per step
            await asyncio.sleep(0)                               # yield, encourage interleaving
        check(claim_id)
        monitoring.current().finish(status="test")

    def run_flow(claim_id: str) -> None:
        asyncio.run(flow(claim_id))                              # own loop, own thread

    threads = [threading.Thread(target=run_flow, args=(c,)) for c in claims]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = True
    if wrong_resolution:
        print(f"  FAIL  current() returned the wrong monitor mid-flow: {wrong_resolution}")
        ok = False

    for claim_id in claims:
        events = read_log(claim_id)
        other = [c for c in claims if c != claim_id][0]
        foreign_id = [e for e in events if e.get("claim_id") != claim_id]
        leaked_tag = [e for e in events
                      if other in (str(e.get("input", "")) + str(e.get("output", "")) + str(e.get("task", "")))]
        tool_calls = [e for e in events if e["event"] == "tool_call"]
        answers = [e for e in events if e["event"] == "agent_answer"]
        tasks = [e for e in events if e["event"] == "task_done"]
        counts_ok = len(tool_calls) == rounds and len(answers) == rounds and len(tasks) == rounds
        claim_ok = not foreign_id and not leaked_tag and counts_ok
        ok = ok and claim_ok
        print(f"  {'PASS' if claim_ok else 'FAIL'}  {claim_id}: {len(events)} events, "
              f"{len(tool_calls)} tool_calls / {len(answers)} answers / {len(tasks)} tasks, "
              f"foreign_claim_id={len(foreign_id)}, leaked_tag={len(leaked_tag)}")
    return ok


# ------------------------------------------------------------ 2
def test_fallback_single_claim() -> bool:
    section("2. FALLBACK, ONE CLAIM (a worker thread that saw neither finds it)")
    # Mimic CrewAI running a parallel analyst on a thread that inherited nothing:
    # start one claim, then resolve current() from a fresh thread.
    monitor = monitoring.ClaimMonitor("CLM-FALL1", provider="test", model="none")
    resolved = {}

    def worker_thread() -> None:
        resolved["monitor"] = monitoring.current()        # no context var, unknown thread -> fallback

    t = threading.Thread(target=worker_thread)
    t.start()
    t.join()
    monitor.finish(status="test")

    got = resolved["monitor"]
    ok = got is not None and got.claim_id == "CLM-FALL1"
    print(f"  {'PASS' if ok else 'FAIL'}  worker thread resolved to "
          f"{got.claim_id if got else None} (expected CLM-FALL1)")
    return ok


# ------------------------------------------------------------ 3
def test_fallback_disabled_when_two_running() -> bool:
    section("3. FALLBACK OFF, TWO CLAIMS (a worker thread guesses NEITHER)")
    # With two claims running, a worker thread that inherited nothing must NOT be
    # handed either monitor - guessing could mislog. It gets None.
    a = monitoring.ClaimMonitor("CLM-FALL2A", provider="test", model="none")
    b = monitoring.ClaimMonitor("CLM-FALL2B", provider="test", model="none")
    resolved = {}

    def worker_thread() -> None:
        resolved["monitor"] = monitoring.current()        # two running -> no fallback

    t = threading.Thread(target=worker_thread)
    t.start()
    t.join()
    a.finish(status="test")
    b.finish(status="test")

    got = resolved["monitor"]
    ok = got is None
    print(f"  {'PASS' if ok else 'FAIL'}  worker thread resolved to "
          f"{got.claim_id if got else None} (expected None - safe, never a guess)")
    return ok


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claimsight_monitoring_"))
    monitoring.LOGS = tmp / "logs"
    results = [
        test_two_concurrent_claims(),
        test_fallback_single_claim(),
        test_fallback_disabled_when_two_running(),
    ]
    section(f"{sum(results)}/{len(results)} monitoring isolation checks passing")
    print(f"Logs: {monitoring.LOGS}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
