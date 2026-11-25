"""
The dashboard's read-only guard, without spending a token.

A dashboard on a public URL must never start the Flow. flow_runner.read_only()
is true when CLAIMSIGHT_DASHBOARD_READONLY is set, or when the process is on a
hosting platform (Render sets RENDER, Cloud Run sets K_SERVICE). Then:

    flow_runner.start() refuses before the lock, the backup or Popen
    the page shows no Mode control and no Live assessment panel,
        even if the session already says "Live assessment"
    a platform cannot be switched back to live by setting the variable to 0
    locally, with no marker, live mode is exactly as before

Every process start is blocked or faked; nothing reaches a model.

Run:  CLAIMSIGHT_OFFLINE=1 uv run python tests/test_dashboard_readonly.py
"""

import os
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "dashboard"))

import flow_runner                            # noqa: E402

MARKERS = (flow_runner.READONLY_VAR, *flow_runner.PLATFORM_MARKERS)


def set_env(**values) -> None:
    """Clear every read-only marker, then set the given ones."""
    for name in MARKERS:
        os.environ.pop(name, None)
    os.environ.update(values)


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    saved_env = {name: os.environ.get(name) for name in MARKERS}
    try:
        # ------------------------------------------------------------ 1. the switch
        print("1. When the dashboard is read-only\n")
        set_env()
        run("locally, with no marker, it is not read-only", not flow_runner.read_only())
        for value in ("1", "true", "TRUE", "yes", "on"):
            set_env(CLAIMSIGHT_DASHBOARD_READONLY=value)
            run(f"CLAIMSIGHT_DASHBOARD_READONLY={value} makes it read-only", flow_runner.read_only())
        for value in ("0", "", "no", "false"):
            set_env(CLAIMSIGHT_DASHBOARD_READONLY=value)
            run(f"CLAIMSIGHT_DASHBOARD_READONLY={value!r} alone does not", not flow_runner.read_only())
        set_env(RENDER="true")
        run("on Render (RENDER=true) it is read-only", flow_runner.read_only())
        set_env(K_SERVICE="claimsight-dashboard")
        run("on Cloud Run (K_SERVICE set) it is read-only", flow_runner.read_only())
        set_env(RENDER="true", CLAIMSIGHT_DASHBOARD_READONLY="0")
        run("a platform cannot be switched back to live with the variable", flow_runner.read_only())

        # ------------------------------------------------------------ 2. the runner
        print("\n2. flow_runner.start() refuses before doing anything\n")
        started = []

        class FakeProcess:
            pid = 4242

            def __init__(self, *args, **kwargs):
                started.append(args)

        tmp = Path(tempfile.mkdtemp())
        old = (flow_runner.RUNTIME, flow_runner.Popen, flow_runner._data)
        live = tmp / "live"
        for folder in ("decisions", "review_queue", "logs"):
            (live / folder).mkdir(parents=True)
        flow_runner.RUNTIME, flow_runner.Popen = tmp / "runtime", FakeProcess
        flow_runner._data = lambda: {"decisions": live / "decisions",
                                     "review_queue": live / "review_queue",
                                     "logs": live / "logs",
                                     "manifest": ROOT / "data" / "manifest.json"}
        try:
            for label, env in (("RENDER", {"RENDER": "true"}),
                               ("K_SERVICE", {"K_SERVICE": "claimsight-dashboard"}),
                               ("the variable", {"CLAIMSIGHT_DASHBOARD_READONLY": "1"})):
                set_env(**env)
                ok, message = flow_runner.start("CLM-5000")
                run(f"with {label}: the start is refused, with a reason",
                    ok is False and message == flow_runner.READONLY_MESSAGE, message)
            run("no process was started", not started, str(started)[:160])
            run("no lock, backup or output file was written",
                not (tmp / "runtime").exists())

            set_env()
            ok, message = flow_runner.start("CLM-5000")
            run("locally, the same call still starts a (fake) run", ok and len(started) == 1, message)
            flow_runner.acknowledge()
        finally:
            flow_runner.RUNTIME, flow_runner.Popen, flow_runner._data = old

        # ------------------------------------------------------------ 3. the page
        print("\n3. The page offers no live control when read-only\n")
        from streamlit.testing.v1 import AppTest
        real_popen, real_run = subprocess.Popen, subprocess.run
        calls = []

        def blocked(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("args"))
            raise AssertionError("a process was started")

        subprocess.Popen, subprocess.run = blocked, blocked
        try:
            set_env(RENDER="true")
            at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=90)
            at.session_state["mode"] = "Live assessment"      # a stale or forged session
            at.session_state["page"] = "Claim Walkthrough"
            at.session_state["claim_id"] = "CLM-5000"
            at.run()
            errors = [str(x.value)[:200] for x in at.exception]
            run("the walkthrough renders on a platform", not errors, str(errors))
            radios = [r.key for r in at.sidebar.radio]
            run("there is no Mode control", "mode" not in radios, str(radios))
            captions = " ".join(str(c.value) for c in at.sidebar.caption)
            run("the sidebar says the deployment is read-only",
                "Read-only deployment" in captions, captions[:160])
            text = " ".join(m.value for m in at.markdown)
            buttons = [b.key for b in at.button]
            run("no Live assessment panel, even though the session asked for it",
                "Live assessment" not in text and not any(str(k).startswith("run-") for k in buttons),
                str(buttons)[:160])

            set_env()
            local = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=90)
            local.run()
            run("locally the Mode control is still there",
                "mode" in [r.key for r in local.sidebar.radio])
        finally:
            subprocess.Popen, subprocess.run = real_popen, real_run
        run("nothing was started by either page", not calls, str(calls)[:160])
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    print(f"\n{passed}/{total} read-only checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
