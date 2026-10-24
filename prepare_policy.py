"""
Prepare the policy wording for RAG.

The ICICI Lombard policy PDF is printed in two columns. A plain text extraction
reads straight across the page, splicing half a sentence from the left column
onto half a sentence from the right, so every chunk the agent retrieves is
scrambled. This script crops each page into its left and right halves and
extracts them separately, giving clean, readable clauses.

Output: knowledge/policy_wording.txt  (used as a TextFileKnowledgeSource)

Run:  uv run python prepare_policy.py
"""

import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).parent
SOURCE = ROOT / "knowledge" / "policy_wording.pdf"
OUTPUT = ROOT / "knowledge" / "policy_wording.txt"

HEADER_BAND = 0.06   # top 6% of each page holds the insurer logo
FOOTER_BAND = 0.035  # bottom 3.5% holds the page number


def clean(text: str) -> str:
    text = text.replace("`", "Rs ")                 # the PDF prints the rupee sign as a backtick
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)  # join words hyphenated across lines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_columns(pdf_path: Path) -> str:
    pages_out = []
    with pdfplumber.open(pdf_path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            width, height = page.width, page.height
            top = height * HEADER_BAND
            bottom = height * (1 - FOOTER_BAND)
            middle = width / 2

            left = page.crop((0, top, middle, bottom)).extract_text() or ""
            right = page.crop((middle, top, width, bottom)).extract_text() or ""

            pages_out.append(f"[Policy wording, page {number}]\n{clean(left)}\n\n{clean(right)}")
    return "\n\n".join(pages_out)


def main() -> None:
    if not SOURCE.exists():
        sys.exit(f"Not found: {SOURCE}. Copy the policy PDF there first.")

    text = extract_columns(SOURCE)
    OUTPUT.write_text(text, encoding="utf-8")

    print(f"Wrote {OUTPUT.relative_to(ROOT)}  ({len(text):,} characters)")

    # Spot-check: the clauses the Policy Analyst most needs must read as whole sentences
    checks = {
        "painting rule": "material component shall be considered as 25%",
        "plastic parts": "plastic parts, tyres and tubes, batteries and air bags",
        "exclusions":    "Consequential loss, depreciation, wear and tear",
        "deductible":    "in respect of the deductible stated",
    }
    flat = re.sub(r"\s+", " ", text)
    for name, phrase in checks.items():
        print(f"  {'found  ' if phrase in flat else 'MISSING'}  {name}")


if __name__ == "__main__":
    main()
