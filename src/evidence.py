"""
Evidence a claimant sends in after ClaimSight asks for it.

A claim can stop for a reason that more paperwork would settle: no total on
the invoice, no incident date, a registration that does not match the policy.
Rather than failing the claim, the Flow records exactly what is missing, and
this module takes in what arrives, checks it, and lets the claim carry on.

    1. the Flow stops          data/decisions/<id>.json, status awaiting_evidence
    2. ClaimSight asks         outstanding(state) -> the exact items needed
    3. the claimant sends      add_evidence.py, which calls submit()
    4. the evidence is checked plain Python rules, no model: a file must exist
                               and be readable, a date must be a date, an
                               amount must be a positive number
    5. the claim resumes       apply_to_intake() patches what code can patch,
                               and plan() says which crews must run again

Each claim keeps a ledger at data/evidence/<claim_id>.json, so what was asked,
what arrived, what was accepted and what it changed is all on record. Nothing
here calls a language model: accepting evidence decides whether someone gets
paid, and that must be a rule a person can read.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

from models import EvidenceRequest   # noqa: E402

EVIDENCE = ROOT / "data" / "evidence"

# An item moves pending -> processing -> applied. "processing" exists so that a
# run which dies part-way can be told apart from one that finished: on the next
# attempt those items are put back to pending and tried again, rather than
# being lost (marked applied without a decision) or applied twice.
PENDING, PROCESSING, APPLIED, REFUSED = "pending", "processing", "applied", "refused"
MAX_ATTEMPTS = 3

FILES_OK = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".txt", ".json", ".csv"}
MAX_FILE_MB = 20

# What the claimant is asked for, and the field it fills. The keys are the gaps
# completeness_gaps() reports, so a request always maps back to something real.
FIELD_FOR_GAP = {
    "invoice total": "invoice.total",
    "incident date": "incident_date",
    "vehicle registration on the invoice": "invoice.registration",
    "invoice registration does not match the policy": "invoice.registration",
}


# ---------------------------------------------------------------- what is needed
def outstanding(state: dict) -> List[EvidenceRequest]:
    """What is still missing on this claim, and who can supply it.

    Documents are asked of the claimant. A coverage question the policy wording
    does not answer is asked of a person, because no document the claimant can
    send will settle it. Anything already accepted on the ledger is dropped."""
    claim_id = state.get("claim_id", "")
    decision = state.get("decision") or {}
    satisfied = [item["item"] for item in accepted(claim_id)]

    out: List[EvidenceRequest] = []
    for gap in ((state.get("intake") or {}).get("missing_fields") or []):
        if any(same_item(gap, done) for done in satisfied):
            continue
        photo = gap.startswith("photograph showing") or "clearer photograph" in gap
        out.append(EvidenceRequest(
            item=gap, from_whom="claimant",
            why=("the photograph on file does not show this billed part, so it cannot be "
                 "checked" if photo else "the claim cannot be assessed without it")))

    # The agent's wording and the code's doubt describe ONE open question.
    why = list(dict.fromkeys(
        [r for r in (decision.get("uncertainty_reasons") or []) if r]
        + [d for d in (state.get("coverage_doubts") or []) if d]))
    if why:
        out.append(EvidenceRequest(item="a coverage decision by a claims officer",
                                   why="; ".join(why), from_whom="adjuster"))
    return out


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def same_item(asked: str, sent: str) -> bool:
    """Is what arrived the thing that was asked for? Either name may be the
    longer phrasing ('total' answers 'invoice total'), so containment counts."""
    a, b = _norm(asked), _norm(sent)
    return bool(a) and bool(b) and (a in b or b in a)


# ---------------------------------------------------------------- the ledger
def write_json(path: Path, data) -> None:
    """Write a JSON file so a reader never sees a half-written one.

    The content goes to a temporary file in the same folder and is then moved
    over the target, which is atomic on one filesystem. Writing in place would
    leave a truncated ledger or intake behind if the process died mid-write,
    and a corrupt ledger loses the claimant's evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def path_for(claim_id: str) -> Path:
    return EVIDENCE / f"{claim_id}.json"


def ledger(claim_id: str) -> dict:
    path = path_for(claim_id)
    if path.exists():
        return json.loads(path.read_text())
    return {"claim_id": claim_id, "items": []}


def save_ledger(book: dict) -> None:
    write_json(path_for(book["claim_id"]), book)


def state_of(item: dict) -> str:
    """This item's state, understanding ledgers written before states existed,
    where the only record was applied: true/false."""
    if item.get("state"):
        return item["state"]
    if not item.get("accepted"):
        return REFUSED
    return APPLIED if item.get("applied") else PENDING


def fingerprint(item: str, kind: str, value: Any = None, file: Optional[str] = None) -> str:
    """What makes two submissions the same submission.

    The same value for the same item is one piece of evidence however many
    times it is sent; for a file it is the bytes that count, so the same
    photograph under a different name is still the same photograph."""
    parts = [_norm(item), kind]
    if file:
        path = Path(file)
        try:
            parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            parts.append(f"unreadable:{path.name}")
    else:
        parts.append(_norm(str(value)))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def accepted(claim_id: str) -> List[dict]:
    return [item for item in ledger(claim_id)["items"] if item.get("accepted")]


def unapplied(claim_id: str) -> List[dict]:
    """Accepted evidence the Flow has not acted on yet.

    Items left in "processing" by a run that died are included: they were never
    applied, so they are still owed a reassessment."""
    return [item for item in accepted(claim_id)
            if state_of(item) in (PENDING, PROCESSING)
            and item.get("attempts", 0) < MAX_ATTEMPTS]


def stuck(claim_id: str) -> List[dict]:
    """Evidence that has failed MAX_ATTEMPTS times. A person has to look."""
    return [item for item in accepted(claim_id)
            if state_of(item) in (PENDING, PROCESSING)
            and item.get("attempts", 0) >= MAX_ATTEMPTS]


# ---------------------------------------------------------------- checking
def check_amount(value: Any) -> Tuple[bool, Any, str]:
    try:
        amount = float(str(value).replace(",", "").replace("Rs", "").strip())
    except (TypeError, ValueError):
        return False, None, f"not a number: {value!r}"
    if amount <= 0:
        return False, None, f"an amount must be more than zero, got {amount}"
    return True, round(amount, 2), f"reads as Rs {amount:,.2f}"


def check_date(value: Any) -> Tuple[bool, Any, str]:
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError):
        return False, None, f"not a date in YYYY-MM-DD form: {value!r}"
    if parsed > date.today():
        return False, None, f"the incident date is in the future: {text}"
    return True, text, f"reads as {parsed:%d %B %Y}"


def check_plate(value: Any) -> Tuple[bool, Any, str]:
    text = re.sub(r"[\s-]", "", str(value)).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{3,4}", text):
        return False, None, f"not an Indian registration number: {value!r}"
    return True, text, f"reads as {text}"


CHECKS = {"invoice.total": check_amount, "incident_date": check_date,
          "invoice.registration": check_plate}


def check_file(path: Path) -> Tuple[bool, Any, str]:
    if not path.exists():
        return False, None, f"no such file: {path}"
    if path.is_dir():
        return False, None, f"that is a folder, not a file: {path}"
    size = path.stat().st_size
    if size == 0:
        return False, None, f"the file is empty: {path.name}"
    if size > MAX_FILE_MB * 1024 * 1024:
        return False, None, f"the file is larger than {MAX_FILE_MB} MB: {path.name}"
    if path.suffix.lower() not in FILES_OK:
        return False, None, (f"{path.suffix or 'that file type'} is not accepted; "
                             f"send one of {', '.join(sorted(FILES_OK))}")
    return True, str(path), f"{path.suffix.lstrip('.').upper()}, {size / 1024:.0f} KB"


# ---------------------------------------------------------------- submitting
def field_for(item: str) -> Optional[str]:
    """The intake field this evidence fills, if code can fill it."""
    for gap, field in FIELD_FOR_GAP.items():
        if same_item(gap, item):
            return field
    return None


def submit(claim_id: str, item: str, *, value: Any = None, file: Optional[str] = None,
           note: str = "", state: Optional[dict] = None) -> dict:
    """Record one piece of evidence against a claim, checked before it is kept.

    Exactly one of value or file. The result says whether it was accepted and
    why, and whether it answers something ClaimSight actually asked for."""
    if (value is None) == (file is None):
        return {"accepted": False, "verdict": "send either a value or a file, not both"}

    entry = {"id": f"ev-{uuid.uuid4().hex[:10]}",
             "item": item, "note": note, "kind": "file" if file else "value",
             "submitted_at": datetime.now().isoformat(timespec="seconds"),
             "state": PENDING, "attempts": 0, "last_error": None,
             "applied": False}          # kept so older readers still work

    if file is not None:
        ok, kept, why = check_file(Path(file).expanduser())
        if ok:
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            folder = EVIDENCE / claim_id
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / Path(kept).name
            shutil.copy2(kept, destination)
            try:
                entry["file"] = str(destination.relative_to(ROOT))
            except ValueError:          # a test redirects the folder outside the project
                entry["file"] = str(destination)
            entry["original_path"] = kept
    else:
        field = field_for(item)
        checker = CHECKS.get(field) if field else None
        if checker:
            ok, kept, why = checker(value)
        else:
            text = str(value).strip()
            ok, kept, why = (bool(text), text, "recorded as written" if text
                             else "nothing was sent")
        entry["value"] = kept
        entry["fills_field"] = field

    entry["accepted"], entry["verdict"] = ok, why

    asked = [r.item for r in outstanding(state)] if state else []
    entry["answers_request"] = any(same_item(a, item) for a in asked)
    if asked and not entry["answers_request"]:
        entry["verdict"] += (f" (ClaimSight did not ask for this; it asked for: "
                             f"{', '.join(asked)})")

    entry["state"] = PENDING if ok else REFUSED
    entry["fingerprint"] = fingerprint(item, entry["kind"], value,
                                       entry.get("original_path"))

    book = ledger(claim_id)
    # The same evidence sent twice is one piece of evidence. Without this, a
    # claimant pressing send again would queue a second reassessment of
    # something already decided.
    for existing in book["items"]:
        if existing.get("fingerprint") == entry["fingerprint"] and existing.get("accepted"):
            return {**existing, "duplicate": True,
                    "verdict": f"already on file as {existing.get('id', 'an earlier item')} "
                               f"({state_of(existing)}): {existing.get('verdict', '')}"}
    book["items"].append(entry)
    save_ledger(book)
    return entry


def _set_state(claim_id: str, entries: List[dict], state: str,
               error: Optional[str] = None, count_attempt: bool = False) -> None:
    """Move these items to a state and save the ledger.

    Items are matched by their id, not by their timestamp: two submissions can
    share a second, and a timestamp is not an identifier."""
    ids = {e.get("id") for e in entries if e.get("id")}
    stamps = {e.get("submitted_at") for e in entries if not e.get("id")}
    book = ledger(claim_id)
    for item in book["items"]:
        if item.get("id") in ids or (not item.get("id")
                                     and item.get("submitted_at") in stamps):
            item["state"] = state
            item["applied"] = state == APPLIED
            item["last_error"] = error
            if count_attempt:
                item["attempts"] = item.get("attempts", 0) + 1
    save_ledger(book)


def mark_processing(claim_id: str, entries: List[dict]) -> None:
    """About to act on this evidence. Recorded before the Flow runs so a run
    that dies leaves a trace, and counted as an attempt so a claim cannot
    retry for ever."""
    _set_state(claim_id, entries, PROCESSING, count_attempt=True)


def mark_applied(claim_id: str, entries: List[dict]) -> None:
    """The Flow finished and the decision was saved. Only now is the evidence
    spent, so a later resume will not act on it again."""
    _set_state(claim_id, entries, APPLIED)


def mark_pending(claim_id: str, entries: List[dict], error: str) -> None:
    """The Flow failed. The evidence goes back in the queue with the reason, so
    the claimant does not have to send it again."""
    _set_state(claim_id, entries, PENDING, error=error)


# ---------------------------------------------------------------- applying
def apply_to_intake(intake: dict, items: List[dict]) -> Tuple[dict, List[str]]:
    """Patch the intake with evidence code can place itself, such as a missing
    total or incident date. Returns the intake and a line per change."""
    changed: List[str] = []
    for item in items:
        field = item.get("fills_field")
        if not field or item.get("kind") != "value":
            continue
        before: Any
        if field.startswith("invoice."):
            key = field.split(".", 1)[1]
            invoice = intake.setdefault("invoice", {})
            before, invoice[key] = invoice.get(key), item["value"]
        else:
            before, intake[field] = intake.get(field), item["value"]
        changed.append(f"{field}: {before!r} -> {item['value']!r} "
                       f"(from evidence: {item['item']})")
    return intake, changed


def plan(state: dict, items: List[dict]) -> dict:
    """Which crews have to run again, and why.

    The point is to re-run as little as possible: a total typed in by the
    claimant does not need the documents read again, so intake is reused and
    only the assessment runs. A new document does need reading."""
    reasons: List[str] = []
    run_intake = False
    for item in items:
        if item.get("kind") == "file" and _norm("invoice") in _norm(item["item"]):
            run_intake = True
            reasons.append(f"a new invoice document arrived, so it must be read again "
                           f"({item['item']})")
    patched = [item for item in items if item.get("fills_field")]
    if patched:
        reasons.append(f"{len(patched)} field(s) filled in from the evidence, "
                       f"so the documents do not need reading again")

    had_decision = bool(state.get("decision"))
    run_assessment = bool(items)
    if run_assessment:
        reasons.append("the assessment runs on the completed file"
                       if not had_decision else
                       "the assessment runs again on the new evidence")
    return {"run_intake": run_intake, "run_assessment": run_assessment, "why": reasons}


def summary(claim_id: str) -> List[str]:
    """One readable line per item on the ledger, for the report and the CLI."""
    lines = []
    for item in ledger(claim_id)["items"]:
        mark = "accepted" if item.get("accepted") else "REJECTED"
        what = item.get("file") or item.get("value")
        state = state_of(item)
        attempts = item.get("attempts", 0)
        tail = f" [{state}]" if item.get("accepted") else ""
        if attempts > 1:
            tail += f" after {attempts} attempts"
        if item.get("last_error"):
            tail += f"; last failure: {item['last_error'][:80]}"
        lines.append(f"{mark}{tail}: {item['item']} = {what} - {item['verdict']}")
    return lines
