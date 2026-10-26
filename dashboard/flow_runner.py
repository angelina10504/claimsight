"""
Live assessment from the dashboard: one real ClaimFlow run, started on purpose.

The Flow is not imported into the dashboard. It runs as its own process, with
a fixed command and no shell:

    <this python> run_flow.py <claim id> --no-input [--fresh]

which is exactly what the terminal demo runs, so the Flow, the guardrails, the
MCP tools, the settlement calculator and the routing are the real ones, and a
claim that needs a person is queued rather than waiting on input().

Three things keep it safe inside Streamlit, which reruns the page on every
click:

    one run at a time   a lock file created with O_EXCL; a rerun, a second
                        tab or a double click finds it and is refused
    nothing is lost     the claim's saved decision and log are copied aside
                        first, and can be put back if the run fails
    no false success    a run counts as done only if the claim's log has a
                        run_finished entry from THIS run and the decision file
                        was written after it started. Otherwise the page says
                        it failed, and the older result is labelled as older.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "dashboard" / ".runtime"
TIMEOUT_SECONDS = 20 * 60
FINAL = {"approved", "rejected", "awaiting_evidence", "awaiting_review",
         "approved_by_adjuster", "rejected_by_adjuster"}

# Popen is looked up through this module so a test can replace it.
Popen = subprocess.Popen


def _data() -> dict:
    data = ROOT / "data"
    return {"decisions": data / "decisions", "review_queue": data / "review_queue",
            "logs": data / "logs", "manifest": data / "manifest.json"}


def _lock() -> Path:
    return RUNTIME / "live_run.json"


def eligible(claim_id: str) -> bool:
    """Development claims only. The 40 held-out claims are for the final
    evaluation and are not run from a demo."""
    import re
    if not isinstance(claim_id, str) or not re.match(r"^CLM-\d{4}$", claim_id):
        return False
    try:
        entries = json.loads(_data()["manifest"].read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any(e.get("claim_id") == claim_id and e.get("split") == "dev" for e in entries)


def _files(claim_id: str) -> dict:
    d = _data()
    return {"decision": d["decisions"] / f"{claim_id}.json",
            "queued": d["review_queue"] / f"{claim_id}.json",
            "log": d["logs"] / f"{claim_id}.jsonl"}


def command(claim_id: str, reuse_intake: bool) -> list:
    return [sys.executable, str(ROOT / "run_flow.py"), claim_id, "--no-input"] + \
        ([] if reuse_intake else ["--fresh"])


def _alive(pid: int) -> bool:
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)       # reap it if it is our child
        if done == pid:
            return False
    except ChildProcessError:
        pass                                        # not our child: ask the OS
    except OSError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def current() -> Optional[dict]:
    path = _lock()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"broken": True}


def start(claim_id: str, reuse_intake: bool = True) -> tuple:
    """Begin one run. Returns (started, message). Never raises for a refusal."""
    if not eligible(claim_id):
        return False, "Only development claims can be run live."
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(_lock(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        run = current() or {}
        return False, (f"A live run of {run.get('claim_id', 'a claim')} is already "
                       f"in progress or awaiting acknowledgement.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = RUNTIME / "backups" / f"{claim_id}-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    existed = {}
    for name, path in _files(claim_id).items():
        existed[name] = path.exists()
        if path.exists():
            shutil.copy2(path, backup / path.name)

    output = RUNTIME / f"live-{claim_id}-{stamp}.txt"
    started_at = time.time()
    try:
        with output.open("w") as out:
            process = Popen(command(claim_id, reuse_intake), cwd=str(ROOT), stdout=out,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    except OSError as exc:
        os.close(fd)
        _lock().unlink(missing_ok=True)
        return False, f"The run could not be started ({type(exc).__name__})."
    with os.fdopen(fd, "w") as f:
        json.dump({"claim_id": claim_id, "pid": process.pid, "started_at": started_at,
                   "reuse_intake": reuse_intake, "backup": backup.name,
                   "existed": existed, "output": output.name}, f)
    return True, f"Live run of {claim_id} started."


def _log_entries(claim_id: str) -> list:
    path = _files(claim_id)["log"]
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _entry_epoch(entry: dict) -> float:
    try:
        return datetime.fromisoformat(entry["time"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return 0.0


def progress(run: dict) -> dict:
    """Where the run has got to, from the claim's own log and the process."""
    claim_id, started_at = run["claim_id"], run["started_at"]
    entries = [e for e in _log_entries(claim_id) if _entry_epoch(e) >= started_at - 2]
    running = _alive(run["pid"])
    finished = next((e for e in reversed(entries) if e.get("event") == "run_finished"), None)
    decision = [p for p in (_files(claim_id)["decision"], _files(claim_id)["queued"])
                if p.exists() and p.stat().st_mtime >= started_at - 1]
    elapsed = time.time() - started_at

    if running and elapsed > TIMEOUT_SECONDS:
        phase = "timed_out"
    elif running:
        phase = "running"
    elif finished and finished.get("status") in FINAL and decision:
        phase = "succeeded"
    else:
        phase = "failed"
    reason = None
    if phase == "failed":
        errors = [e for e in entries if e.get("event") in ("error", "llm_call_failed")]
        if errors:
            last = errors[-1]
            reason = str(last.get("kind") or last.get("error_type") or "error").replace("_", " ")
            if last.get("where"):
                reason += f" during {last['where']}"
        elif not entries:
            reason = "the run stopped before it wrote anything to the claim's log"
        else:
            reason = "the run ended without a decision"
    return {"phase": phase, "elapsed": round(elapsed, 1), "entries": entries,
            "status": (finished or {}).get("status"), "reason": reason,
            "rate_limited": any(e.get("event") == "llm_call_failed" for e in entries)}


def output_tail(run: dict, lines: int = 25) -> str:
    """The end of the run's console output, with anything key-like masked."""
    import re
    path = RUNTIME / run.get("output", "")
    if not run.get("output") or not path.exists():
        return ""
    text = "\n".join(path.read_text(errors="ignore").splitlines()[-lines:])
    text = re.sub(r"\b(gsk_|sk-lf-|pk-lf-|sk-|AIza)[A-Za-z0-9_\-]{8,}", "[key hidden]", text)
    return text.replace(str(ROOT), ".")


def cancel(run: dict) -> None:
    try:
        os.killpg(run["pid"], signal.SIGTERM)
    except (OSError, KeyError):
        pass


def acknowledge() -> None:
    """Clear a finished run so another can start. Refused while it runs."""
    run = current()
    if run and not run.get("broken") and _alive(run["pid"]):
        return
    _lock().unlink(missing_ok=True)


def restore(run: dict) -> bool:
    """Put back the decision and log the claim had before the run."""
    backup = RUNTIME / "backups" / run.get("backup", "")
    if not run.get("backup") or not backup.exists() or _alive(run["pid"]):
        return False
    for name, path in _files(run["claim_id"]).items():
        saved = backup / path.name
        if run.get("existed", {}).get(name) and saved.exists():
            shutil.copy2(saved, path)
        elif not run.get("existed", {}).get(name):
            path.unlink(missing_ok=True)
    return True


def preflight() -> dict:
    """Is a live run likely to work? Checked only when asked. Says whether a
    key is set, never what it is."""
    import urllib.request
    keys = {}
    env_file = ROOT / ".env"
    names = set()
    if env_file.exists():
        for line in env_file.read_text(errors="ignore").splitlines():
            name, _, value = line.partition("=")
            if value.strip():
                names.add(name.strip())
    keys["groq_key"] = "GROQ_API_KEY" in names or bool(os.getenv("GROQ_API_KEY"))
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.5):
            keys["ollama"] = True
    except Exception:                              # noqa: BLE001
        keys["ollama"] = False
    return keys
