# Tests

The repository uses executable Python test scripts with their own assertions,
reporting and exit codes. Run them with the offline runner rather than assuming
that `pytest` executes all of these checks.

```bash
uv run python scripts/run_offline_tests.py
uv run python scripts/run_offline_tests.py test_a2a test_safety_fixes
uv run python scripts/run_offline_tests.py --list
```

The runner clears provider/telemetry keys, sets `CLAIMSIGHT_OFFLINE=1`, blocks
external socket connections inside the test process and preserves loopback for
the local A2A server. Each file runs in a separate process; a failure or timeout
makes the runner exit nonzero. MCP checks launch the actual local stdio server.

Individual checks can also be run directly, for example:

```bash
CLAIMSIGHT_OFFLINE=1 uv run python tests/test_safety_fixes.py
```

Direct execution does not install the runner's socket guard. The existing
scripts use stand-in crews or mocked model calls where required. The environment
flag alone is not a blanket guarantee that arbitrary application code cannot
call a model.

## What the offline checks cover

| Area | Files |
|---|---|
| Settlement and source-policy assumptions | `test_settlement.py`, `test_audit.py` |
| Dataset isolation and deterministic tools | `test_dataset.py`, `test_tools.py` |
| MCP and A2A boundaries | `test_mcp.py`, `test_a2a.py` |
| Input/output checks, privacy and citation scope | `test_guardrails.py`, `test_privacy.py`, `test_clause_scope.py`, `test_registration_masking.py` |
| Routing, evidence, reporting and safety replays | `test_routing.py`, `test_evidence_flow.py`, `test_report.py`, `test_safety_fixes.py` |
| Vision and photo-driven routing | `test_vision.py`, `test_photo_flow.py` |
| Logging, traces, limits and log archival | `test_monitoring.py`, `test_observability.py`, `test_llm_limits.py`, `test_log_archive.py` |
| Evaluation harness | `test_evaluation.py` |
| Dashboard and hosted read-only guard | `test_dashboard.py`, `test_dashboard_readonly.py` |

`fixtures/replay/` preserves the real model outputs used to reproduce the
November safety findings without repeating inference. Some tests are also
imported as stand-in crews by the dashboard's isolated evidence sandbox.
A passing protocol or stand-in test does not measure an agent's judgement.

## Live check — deliberately separate

```bash
uv run python tests/live/test_policy_knowledge.py
```

This retrieves policy wording and calls a real model, spending provider tokens.
It is never selected by the offline runner. To grade an existing answer without
calling a model:

```bash
CLAIMSIGHT_OFFLINE=1 uv run python tests/live/test_policy_knowledge.py --grade answers.txt
```
