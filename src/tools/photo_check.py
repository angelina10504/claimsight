"""
The photograph against the invoice: the one check no text tool can make.

Kept apart from assessment_tools.py, which imports CrewAI, so the dashboard
can show the comparison without loading an agent framework. assessment_tools
re-exports it, so there is still one implementation.
"""

from typing import List, Optional

from parts import part_name as _part_name


def check_billed_parts_against_photo(line_items: List[dict],
                                     damaged: List[str],
                                     undamaged: Optional[List[str]] = None,
                                     not_visible: Optional[List[str]] = None,
                                     photo_readable: bool = True) -> dict:
    """Do the parts billed on the invoice appear damaged in the photograph?

    This is the check no text tool can make. A phantom part - a part billed but
    never damaged - sits on the invoice at a fair price, on a valid policy,
    within the period, with no earlier claim. Every other tool passes it. Only
    the photograph shows the part was intact.

    The comparison is done here, in code, and not by the model that looked at
    the photograph: that model is never shown the invoice, so it cannot be led
    into agreeing with it. It reports what it saw; this decides what that means.

    A billed part is flagged ONLY when the photograph shows it undamaged: that
    is the one case where the photo is positive evidence against the line. A
    billed part the photograph does not show (out of frame, or a close-up that
    cannot tell front from rear) is "unchecked", with no flag: absence of
    evidence is not evidence, and a further photograph would settle it. The
    check is conclusive only when every billed part was seen one way or the
    other.

    An unreadable photograph is not evidence of fraud. It returns
    conclusive=False with no flags, so a bad photo cannot reject a claim.
    """
    if not isinstance(line_items, list) or not line_items:
        return {"error": "the invoice has no line items to check",
                "reason": "incomplete_document", "line_items": line_items}

    billed = {}
    for line in line_items:
        if not isinstance(line, dict):
            continue
        part = _part_name(str(line.get("description", "")))
        if part:
            billed.setdefault(part, str(line.get("description", "")))

    if not photo_readable:
        return {
            "conclusive": False,
            "billed_parts": sorted(billed),
            "damaged": [], "undamaged": [], "not_visible": [],
            "visible_parts": [],
            "unsupported_parts": [],
            "unsupported_lines": [],
            "unchecked_parts": sorted(billed),
            "not_billed": [],
            "flags": [],
            "suspicious": False,
            "note": "the photograph could not be read, so the billed parts "
                    "could not be checked against it. This is not a fraud "
                    "signal; assess on the paperwork, or ask for a clearer photo.",
        }

    def names(values):
        return {str(p).lower().strip() for p in (values or [])}

    seen_damaged = names(damaged)
    seen_intact = names(undamaged) - seen_damaged   # a contradiction is not evidence
    unsupported = sorted(part for part in billed if part in seen_intact)
    # Not visible, or a billed part the analyst was never asked about.
    unchecked = sorted(part for part in billed
                       if part not in seen_damaged and part not in seen_intact)
    not_billed = sorted(part for part in seen_damaged if part not in billed)

    flags = [f"{billed[part]}: billed, but the photograph shows this part "
             f"undamaged" for part in unsupported]

    notes = []
    if unsupported:
        notes.append(f"{len(unsupported)} billed part(s) shown undamaged")
    if unchecked:
        notes.append(f"{len(unchecked)} billed part(s) not visible in the photograph "
                     f"({', '.join(unchecked)}); not flagged - a further photograph "
                     f"would settle it")
    if not notes:
        notes.append("every billed part is visible as damaged in the photograph")

    return {
        "conclusive": not unchecked,
        "billed_parts": sorted(billed),
        "damaged": sorted(seen_damaged),
        "undamaged": sorted(seen_intact),
        "not_visible": sorted(names(not_visible)),
        "visible_parts": sorted(seen_damaged),      # alias of damaged
        "unsupported_parts": unsupported,
        "unsupported_lines": [billed[part] for part in unsupported],
        "unchecked_parts": unchecked,
        "not_billed": not_billed,      # damaged in the photo but not claimed
        "flags": flags,
        "suspicious": bool(flags),
        "note": "; ".join(notes),
    }
