"""
Sharing a finished claim log with the other services, without a token.

In the cloud the dashboard and the assistant cannot see the A2A container's
disk, so every claim assessed there read "No monitoring log is saved" and all
six agents "Not run yet". ClaimMonitor now copies the completed log ONCE, at
finish(), into CLAIMSIGHT_LOG_ARCHIVE (a folder mounted from the shared bucket).

    with no folder configured (every local run), nothing is copied
    the copy is the whole log, run_finished included, and leaves no .tmp behind
    a re-run replaces the earlier copy
    every terminal path copies it: approved, rejected, review, evidence, failed
    the copy failing does not fail the claim: no exception, decision still written,
      local log intact, the failure noted in the local log, no stray .tmp
    a copy that hangs is abandoned after the timeout, and the decision is written
    the event-by-event writes still go to the local disk only (hot path unchanged)

Run:  CLAIMSIGHT_OFFLINE=1 uv run python tests/test_log_archive.py
"""

import os
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")
os.environ.pop("CLAIMSIGHT_LOG_ARCHIVE", None)

import inspect
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import monitoring                                   # noqa: E402
import claim_flow                                   # noqa: E402

CLAIM = "CLM-TEST-ARCHIVE"


def events(path: Path) -> list:
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))

    work = Path(tempfile.mkdtemp(prefix="claimsight-archive-"))
    local, shared = work / "local_logs", work / "bucket_logs"
    shared.mkdir()
    monitoring.LOGS = local
    claim_flow.DECISIONS = work / "decisions"
    claim_flow.REVIEW_QUEUE = work / "review_queue"

    def stray_tmp() -> list:
        return [p.name for p in shared.iterdir() if p.name.endswith(".tmp")]

    print("1. No folder configured: a local run copies nothing\n")
    m = monitoring.ClaimMonitor(CLAIM)
    m.finish(status="approved")
    run("nothing appears in the shared folder", not any(shared.iterdir()))
    run("the local log is complete", events(local / f"{CLAIM}.jsonl")[-1] == "run_finished")

    os.environ["CLAIMSIGHT_LOG_ARCHIVE"] = str(shared)

    print("\n2. The copy\n")
    m = monitoring.ClaimMonitor(CLAIM)
    m.record("task_done", agent="Policy Analyst")
    run("during the run nothing is in the shared folder (hot path stays local)",
        not (shared / f"{CLAIM}.jsonl").exists())
    m.finish(status="approved", route="auto_approve")
    copy = shared / f"{CLAIM}.jsonl"
    run("the finished log lands in the shared folder", copy.exists())
    run("it is byte-for-byte the local log",
        copy.exists() and copy.read_bytes() == (local / f"{CLAIM}.jsonl").read_bytes())
    run("run_finished is in it (copied after the run ends)",
        copy.exists() and events(copy)[-1] == "run_finished")
    run("no .tmp is left behind", not stray_tmp(), str(stray_tmp()))
    m = monitoring.ClaimMonitor(CLAIM)
    m.finish(status="rejected")
    run("a re-run replaces the earlier copy",
        json.loads(copy.read_text().splitlines()[-1])["status"] == "rejected")

    print("\n3. Every terminal path goes through ClaimMonitor.finish\n")
    for step in ("approve_claim", "reject_claim", "send_to_adjuster", "request_evidence"):
        source = inspect.getsource(getattr(claim_flow.ClaimFlow, step))
        run(f"{step} ends with self.finish()", "self.finish()" in source)
    run("ClaimFlow.finish calls the monitor's finish before writing the decision",
        "monitor.finish(" in inspect.getsource(claim_flow.ClaimFlow.finish))
    run("a failed step (guarded) calls the monitor's finish too",
        'monitor.finish(status="failed"' in inspect.getsource(claim_flow.ClaimFlow.guarded))

    def flow_finish(status: str) -> claim_flow.ClaimFlow:
        flow = claim_flow.ClaimFlow()
        flow.state.claim_id = CLAIM
        monitoring.ClaimMonitor(CLAIM)
        flow.state.status = status
        flow.finish()
        return flow

    for status in ("approved", "rejected", "awaiting_review", "awaiting_evidence"):
        copy.unlink(missing_ok=True)
        flow_finish(status)
        last = json.loads(copy.read_text().splitlines()[-1]) if copy.exists() else {}
        run(f"a claim that ends '{status}' shares its log", last.get("status") == status)

    copy.unlink(missing_ok=True)
    flow = claim_flow.ClaimFlow()
    flow.state.claim_id = CLAIM
    monitoring.ClaimMonitor(CLAIM)

    def boom():
        raise RuntimeError("crew fell over")
    try:
        flow.guarded("assess", boom)
    except RuntimeError:
        pass
    last = json.loads(copy.read_text().splitlines()[-1]) if copy.exists() else {}
    run("a claim whose step fails shares its log too",
        last.get("status") == "failed" and last.get("failed_at") == "assess")

    print("\n4. The copy failing never fails the claim\n")
    decision = claim_flow.DECISIONS / f"{CLAIM}.json"

    def failing_run(label, make_broken):
        decision.unlink(missing_ok=True)
        undo = make_broken()
        try:
            flow = claim_flow.ClaimFlow()
            flow.state.claim_id = CLAIM
            monitoring.ClaimMonitor(CLAIM)
            flow.state.status = "approved"
            started = time.time()
            error = None
            try:
                flow.finish()
            except BaseException as exc:            # noqa: BLE001
                error = exc
            took = time.time() - started
        finally:
            undo()
        local_events = events(local / f"{CLAIM}.jsonl")
        run(f"{label}: no exception reaches the claim", error is None, repr(error))
        run(f"{label}: the decision is still written",
            decision.exists() and json.loads(decision.read_text())["status"] == "approved")
        run(f"{label}: the local log is intact and notes the failure",
            "run_finished" in local_events and local_events[-1] == "log_archive_failed",
            str(local_events[-2:]))
        run(f"{label}: no .tmp is left in the shared folder", not stray_tmp(), str(stray_tmp()))
        return took

    def missing_folder():
        os.environ["CLAIMSIGHT_LOG_ARCHIVE"] = str(work / "not_mounted")
        return lambda: os.environ.__setitem__("CLAIMSIGHT_LOG_ARCHIVE", str(shared))
    failing_run("mount missing", missing_folder)

    def rename_fails():
        real = monitoring.os.replace
        def broken(source, target, *a, **k):
            # Only the upload breaks; the decision's own write must still work.
            if str(shared) in str(target):
                raise OSError(5, "Input/output error")
            return real(source, target, *a, **k)
        monitoring.os.replace = broken
        return lambda: setattr(monitoring.os, "replace", real)
    failing_run("upload errors mid-copy", rename_fails)

    def archive_raises():
        real = monitoring.archive_log
        def broken(*a, **k):
            raise MemoryError("unexpected")
        monitoring.archive_log = broken
        return lambda: setattr(monitoring, "archive_log", real)
    failing_run("archive function itself raises", archive_raises)

    release = threading.Event()

    def mount_hangs():
        real_copy, real_timeout = monitoring._copy_atomically, monitoring.LOG_UPLOAD_TIMEOUT_S
        monitoring._copy_atomically = lambda *a, **k: release.wait(30)
        monitoring.LOG_UPLOAD_TIMEOUT_S = 0.3
        def undo():
            monitoring._copy_atomically = real_copy
            monitoring.LOG_UPLOAD_TIMEOUT_S = real_timeout
            release.set()
        return undo
    took = failing_run("mount hangs", mount_hangs)
    run("a hung mount delays the claim by the timeout only", took < 3, f"{took:.2f}s")

    print(f"\n{passed}/{total} log archive checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
