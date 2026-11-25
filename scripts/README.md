# Utilities

Run from the repository root. Application/demo entry points remain at the root;
these scripts prepare data, check integrations or run the offline suite.

| Command | Purpose | Side effects |
|---|---|---|
| `uv run python scripts/run_offline_tests.py` | Run all offline script-based checks | Isolated test processes; temporary test data; no model tokens |
| `uv run python scripts/run_offline_tests.py --list` | List offline checks | No test execution |
| `uv run python scripts/generate_data.py` | Regenerate synthetic policies, invoices and claims | Rewrites dataset files; needs WeasyPrint system libraries |
| `uv run python scripts/prepare_policy.py` | Extract the canonical two-column policy PDF | Rewrites `knowledge/policy_wording.txt` |
| `uv run python scripts/make_photo_labels.py <annotations.json>` | Draft photo labels from VehiDE annotations | Rewrites `data/photo_labels.json`; review manually |
| `uv run python scripts/generate_photo_claims.py` | Generate photo-backed claims | Rewrites photo claims, policies and invoices |
| `uv run python scripts/check_vision.py` | Inspect provider models and diagnose a vision request | External API access and a real vision call; spends tokens |
| `uv run python scripts/check_langfuse.py` | Probe authentication and export a synthetic span | External telemetry request; no LLM call |

The committed dataset already exists. Preparation scripts are maintenance tools,
not prerequisites for every demo. On macOS, invoice generation may need
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` for WeasyPrint.
