# Data and saved records

## Committed inputs

| Path | Contents |
|---|---|
| `claims/`, `policies/`, `invoices/` | Synthetic submissions, policy schedules and invoice PDFs |
| `manifest.json` | Official 60-claim split: 20 development and 40 held out |
| `photo_manifest.json` | Additional photo-backed claims |
| `photo_labels.json` | Reviewed reference labels used by photo claim generation |
| `photos/` | Eleven selected vehicle photographs and validation annotations |
| `assessment/` | Earlier assessment-only demonstration records; not full Flow authorisations |

The canonical source policy is `knowledge/policy_wording.pdf`, with extracted
wording beside it. The former identical copy under `data/policy/` was removed.
The photo-label draft formerly named `data/main.py` is preserved as
`docs/archive/photo_labels_draft.json`; it differs from the final labels and is
not read by the runtime.

## Generated locally, ignored by Git

`intake/`, `decisions/`, `review_queue/`, `logs/`, `evidence/` and `reports/`
contain per-run records. Keep them local or in configured shared storage.
They are not application source or a substitute for the evaluation archive.

Full VehiDE images and training annotations are not redistributed. Existing
ignore rules preserve local downloads. Regeneration tools are documented in
[scripts/README.md](../scripts/README.md).

Ground-truth labels are for scoring. Production agent inputs are built through
whitelists, not by handing complete labelled claim JSON to an agent. Ordinary
evaluation and dashboard live selection stay on development claims; the explicit
final-evaluation mode is the controlled route into the held-out split.
