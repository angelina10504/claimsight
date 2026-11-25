"""Run the offline script-based checks in isolated processes, without provider access.

Usage: uv run python scripts/run_offline_tests.py [test_safety_fixes test_dashboard]
       uv run python scripts/run_offline_tests.py --list

Live model tests live under tests/live/ and cannot be selected by this runner.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

# The A2A checks need loopback HTTP and the MCP checks need local subprocesses.
# Block Internet sockets before importing the test or any model framework.
BOOTSTRAP = """
import ipaddress
import runpy
import socket
import sys
from pathlib import Path

def guard(original):
    def connect(sock, address):
        if isinstance(address, tuple):
            host = str(address[0])
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host.lower() == 'localhost'
            if not local:
                raise OSError('External network access is disabled in offline tests')
        return original(sock, address)
    return connect

socket.socket.connect = guard(socket.socket.connect)
socket.socket.connect_ex = guard(socket.socket.connect_ex)
test = Path(sys.argv[1])
sys.path.insert(0, str(test.parent))
sys.argv = [str(test)]
runpy.run_path(str(test), run_name='__main__')
"""


def main() -> int:
    available = {p.stem: p for p in sorted((ROOT / "tests").glob("test_*.py"))}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Offline test names, with or without .py")
    parser.add_argument("--list", action="store_true", help="List checks without running them")
    args = parser.parse_args()
    names = [Path(name).stem for name in args.tests] or list(available)
    refused = [name for name in names if name not in available]
    if refused:
        parser.error("Not an offline test: " + ", ".join(refused))
    if args.list:
        print("\n".join(names))
        return 0

    env = dict(os.environ, CLAIMSIGHT_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1",
               CLAIMSIGHT_LOG_ARCHIVE="", LITELLM_LOCAL_MODEL_COST_MAP="True",
               LLM_PROVIDER="groq", CLAIMSIGHT_EMBEDDER="ollama",
               NO_PROXY="localhost,127.0.0.1,::1", PYTHONUNBUFFERED="1")
    for key in ("GROQ_API_KEY", "GROQ_API_KEYS", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY"):
        env[key] = ""
    # Some construction-only tests need a nonempty SDK credential. This is a
    # literal placeholder; the socket guard still blocks all provider traffic.
    env["OPENAI_API_KEY"] = "offline-test-placeholder"
    # The MCP launcher invokes bare `python`; keep it on this interpreter's
    # environment even when the runner itself was started by an absolute path.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    failed = []
    for name in names:
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, "-c", BOOTSTRAP, str(available[name])],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            ok = result.returncode == 0
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            ok, output = False, "Timed out after 300 seconds."
        print(f"{'PASS' if ok else 'FAIL'} {name} ({time.monotonic() - started:.1f}s)", flush=True)
        if not ok:
            failed.append(name)
            # Tests use synthetic data; also hide any key-shaped text in errors.
            clean = re.sub(r"\b(?:gsk_|sk-lf-|pk-lf-|sk-|AIza)[A-Za-z0-9_-]{8,}",
                           "[key hidden]", output)
            print(clean[-16000:], flush=True)
    print(f"\n{len(names) - len(failed)}/{len(names)} offline test scripts passed.")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
