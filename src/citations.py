"""
Checking a coverage reason against the policy wording.

An agent can write "Section II exclusion 4" and it will look like a citation.
This policy has no numbered exclusions at all, so that reference is invented,
and a claim must never be rejected on it. Three different questions are worth
keeping apart:

    cited               does the reason name a clause at all?
    source verified     does that clause exist in the policy document?
    supports conclusion does the text at that clause actually exclude this?

The third matters as much as the second. "Section I item 6, by accidental
external means" is a real clause, correctly quoted, and it is a list of what
the policy COVERS. Citing it as grounds for refusing a claim is a real
citation supporting the opposite conclusion, so it cannot justify a rejection
either.

Where any of the three cannot be established, the claim goes to a person. No
model is involved: this is text matching against the policy document, which is
why its answers can be checked by hand.
"""

import re
import sys
from pathlib import Path
from typing import List, Optional

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
WORDING = ROOT / "knowledge" / "policy_wording.txt"

# Section headings this policy really has. Roman numerals only; "Section 32" in
# the text is a cross-reference to another Act, not a section of the policy.
SECTION_RE = re.compile(r"\bsection\s*-?\s*(i{1,3}|iv|v)\b(?!\w)", re.I)
# "item 6", "clause 3", "exclusion 4", "exception 2", "sub-section 2"
ITEM_RE = re.compile(r"\b(item|clause|exclusion|exception|para(?:graph)?|point)\s*"
                     r"(?:no\.?\s*)?(\d{1,2})\b", re.I)
IMT_RE = re.compile(r"\bimt\.?\s*(\d{1,2})\b", re.I)
NAMED_BLOCK_RE = re.compile(r"\b(general exceptions|special exclusions|"
                            r"limitations as to use|driver'?s clause|deductible)\b", re.I)
QUOTE_RE = re.compile(r"[\"“‘']([^\"”’']{20,})[\"”’']")

# Wording that marks a passage as taking cover away rather than giving it.
EXCLUDING = re.compile(r"(shall not be liable|will not be liable|not be liable|"
                       r"no liability|excluded|exclusion|exception|"
                       r"shall not apply|does not cover|not indemnif)", re.I)
# Wording that marks a passage as granting cover.
GRANTING = re.compile(r"(will indemnify|shall indemnify|the company will pay|"
                      r"indemnify the insured)", re.I)

STOP_WORDS = {"the", "and", "for", "this", "that", "with", "from", "under", "policy",
              "section", "item", "clause", "exclusion", "exception", "claim", "not",
              "are", "was", "were", "any", "all", "its", "his", "her", "been", "has",
              "have", "which", "shall", "will", "company", "insured", "vehicle"}

_cache: List[str] = []


def wording() -> str:
    """The policy wording as one plain, single-spaced, lowercase string."""
    if not _cache:
        text = WORDING.read_text(errors="ignore") if WORDING.exists() else ""
        for fancy, plain in {"‘": "'", "’": "'", "“": '"',
                             "”": '"', "–": "-", "—": "-"}.items():
            text = text.replace(fancy, plain)
        _cache.append(" ".join(text.lower().split()))
    return _cache[0]


def _section_block(text: str, numeral: str) -> Optional[str]:
    """The text of one section, up to wherever the next section heading starts."""
    start = None
    for match in re.finditer(rf"section\s*-?\s*{numeral}\b", text):
        after = text[match.end():match.end() + 80]
        if "." in after[:4] or after.strip()[:1].isalpha():
            start = match.start()
            break
    if start is None:
        return None
    nxt = SECTION_RE.search(text, start + 20)
    return text[start:nxt.start() if nxt else start + 6000]


def _numbered_item(block: str, number: str) -> Optional[str]:
    """Item N inside a block, as the enumerated lists in this policy are written
    ("6. By accidental external means;")."""
    for match in re.finditer(rf"(?:^|[;.]\s|\s){number}\.?\s+([a-z(][^;]{{4,180}})", block):
        return match.group(1).strip()
    return None


def _significant(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
            if w not in STOP_WORDS}


def check(reason: str, verdict: str = "excluded", text: Optional[str] = None) -> dict:
    """Examine one coverage reason. Returns the three findings and the evidence.

    `verdict` is the conclusion the reason is being used to support, so that a
    passage granting cover is not accepted as grounds for refusing a claim."""
    text = wording() if text is None else " ".join(text.lower().split())
    reason = reason or ""
    low = " ".join(reason.lower().split())

    sections = [m.group(1).lower() for m in SECTION_RE.finditer(low)]
    items = [(m.group(1).lower(), m.group(2)) for m in ITEM_RE.finditer(low)]
    imts = IMT_RE.findall(low)
    blocks = [m.group(1).lower() for m in NAMED_BLOCK_RE.finditer(low)]
    quotes = [" ".join(q.lower().split()) for q in QUOTE_RE.findall(reason)]

    identifiers = ([f"section {s}" for s in sections]
                   + [f"{kind} {number}" for kind, number in items]
                   + [f"IMT {n}" for n in imts] + blocks)
    found: List[dict] = []
    missing: List[str] = []

    # a quoted passage is the strongest form: it either is in the document or is not
    for quote in quotes:
        if quote in text:
            where = text.find(quote)
            found.append({"identifier": f'quote "{quote[:60]}"', "text": quote,
                          "context": text[max(0, where - 200):where + len(quote) + 200]})
        else:
            missing.append(f'quoted text not in the policy: "{quote[:60]}"')

    section_blocks = {s: _section_block(text, s) for s in sections}
    for numeral, block in section_blocks.items():
        if block is None:
            missing.append(f"section {numeral} is not in this policy")

    if items:
        for kind, number in items:
            located = None
            for numeral, block in section_blocks.items():
                if block:
                    located = _numbered_item(block, number)
                    if located:
                        found.append({"identifier": f"section {numeral} {kind} {number}",
                                      "text": located, "context": block[:1500]})
                        break
            if located:
                continue
            # Only look in a named block when the reason did NOT name a section.
            # "Section II exclusion 4" is a claim about Section II; finding an
            # item 4 somewhere else in the policy does not make it true.
            if sections and not blocks:
                missing.append(f"{kind} {number} is not in section {sections[0]}")
                continue
            for name in blocks or ["general exceptions"]:
                where = text.find(name)
                if where >= 0:
                    block = text[where:where + 3000]
                    located = _numbered_item(block, number)
                    if located:
                        found.append({"identifier": f"{name} {kind} {number}",
                                      "text": located, "context": block[:1500]})
                        break
            if not located:
                missing.append(f"{kind} {number} is not in "
                               f"{'section ' + sections[0] if sections else 'this policy'}")
    else:
        for numeral, block in section_blocks.items():
            if block:
                found.append({"identifier": f"section {numeral}", "text": block[:300],
                              "context": block[:1500]})
        for name in blocks:
            where = text.find(name)
            if where >= 0:
                found.append({"identifier": name, "text": text[where:where + 300],
                              "context": text[where:where + 1500]})
            else:
                missing.append(f"{name} is not in this policy")
        for n in imts:
            where = text.find(f"imt. {n}")
            if where >= 0:
                found.append({"identifier": f"IMT {n}", "text": text[where:where + 200],
                              "context": text[where:where + 1200]})
            else:
                missing.append(f"IMT {n} is not in this policy")

    cited = bool(identifiers or quotes)
    source_verified = bool(found) and not missing

    # Does the located text support the conclusion it is cited for?
    supports = False
    why = ""
    if source_verified and verdict == "excluded":
        shared_needed = _significant(reason) - {"section", "item"}
        for passage in found:
            window = passage["context"] or passage["text"]
            excluding = bool(EXCLUDING.search(window))
            granting = bool(GRANTING.search(window[:400]))
            overlap = _significant(passage["text"]) & shared_needed
            if excluding and not granting:
                supports = True
                why = (f"{passage['identifier']} sits in text that takes cover away"
                       + (f", and shares '{sorted(overlap)[0]}' with the reason"
                          if overlap else ""))
                break
            if granting and not excluding:
                why = (f"{passage['identifier']} is part of what the policy COVERS, "
                       f"so it cannot support refusing the claim")
        if not supports and not why:
            why = "the cited text neither grants nor withholds cover clearly"
    elif source_verified:
        supports = True
        why = "the citation was found in the policy"

    if not cited:
        note = "no clause or quotation given, only the agent's own words"
    elif missing:
        note = "; ".join(missing[:2])
    elif not supports:
        note = why or "the cited clause does not support this conclusion"
    else:
        note = why or "verified against the policy wording"

    return {"reason": reason, "identifiers": identifiers, "cited": cited,
            "source_verified": source_verified, "supports_conclusion": supports,
            "passages": found, "missing": missing, "note": note}


def cites_policy(reason: str) -> bool:
    """Kept for callers that only want to know whether a reason is grounded in
    the document at all. Routing uses check() and looks at all three findings."""
    return check(reason).get("source_verified", False)
