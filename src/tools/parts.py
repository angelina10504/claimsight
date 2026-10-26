"""
The parts ClaimSight prices, and how an invoice line is matched to one.

Kept apart from assessment_tools.py, which imports CrewAI, so code that must
never load an agent framework (the dashboard) can still use them.
"""

from typing import Optional

# Reference market prices for replacement parts (Rs, before GST). A real
# insurer would pull these from a parts catalogue; here they match the
# generator's catalogue so the check is fair to genuine bills.
MARKET_PRICES = {
    "front bumper": 9000, "rear bumper": 8500, "headlight": 7200,
    "taillight": 4300, "side mirror": 3100, "grille": 2800,
    "bonnet": 12500, "front door": 14000, "rear door": 13200,
    "fender": 6800, "windshield": 11000, "door glass": 4800,
}


def part_name(description: str) -> Optional[str]:
    text = description.lower()
    for part in sorted(MARKET_PRICES, key=len, reverse=True):   # "door glass" before "door"
        if part in text:
            return part
    return None
