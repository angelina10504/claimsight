"""
Step 1: check that the Policy Analyst can retrieve answers from the policy wording.

The policy PDF is attached as agent-level Knowledge. Embeddings run locally on
Ollama (nomic-embed-text), so no OpenAI key is needed.

The answers are GRADED, and the test fails if any is wrong:
    each answer must state the figure or fact the wording gives, and
    each answer must rest on a clause that APPLIES to a Private Car Package
    Policy (clause_scope.py): quoting a commercial-vehicle endorsement such as
    IMT 23, or naming an endorsement that is not attached, fails even when the
    words happen to be right. That is the error that passed silently on 18 Nov.
The expected answers are pinned to their source lines in the wording, and the
test checks those lines still say so, so an edited wording cannot leave the
grader checking against nothing.

Run:    uv run python test_policy_knowledge.py         (real Groq; spends tokens)
Grade a saved answer without any model call:
        uv run python test_policy_knowledge.py --grade answers.txt
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import clause_scope                                                                   # noqa: E402

WORDING = (ROOT / "knowledge" / "policy_wording.txt").read_text().split("\n")
PRIVATE_CAR = {"zero_depreciation": False}

# The correct answer to each question, the wording lines it comes from, and a
# phrase those lines must contain. Grading checks the answer against `must`;
# `source` is checked against the wording before any answer is graded.
EXPECTED = {
    1: {"source": (20, "6. By accidental external means"),
        "must": [r"accidental external means"],
        "also_one_of": [r"\byes\b", r"\bcovered\b"],
        "summary": "Yes - Section I, peril 6, line 20: 'by accidental external means'"},
    2: {"source": (27, "rubber/ nylon / plastic parts"),
        "must": [r"\b50\s*%"],
        "summary": "50% - rubber/nylon/plastic parts, tyres, batteries, air bags (line 27)"},
    3: {"source": (42, "50% shall be applied only on the material cost"),
        "must": [r"\b50\s*%", r"\b25\s*%", r"material"],
        "summary": "50% on the material cost only; material taken as 25% of the bill (lines 41-45)"},
    4: {"source": (47, "Consequential loss, depreciation, wear and tear"),
        "two_of": [r"wear and tear", r"mechanical|electrical breakdown", r"consequential loss",
                   r"intoxicat|under the influence|liquor|drugs",
                   r"tyres? (and tubes )?unless"],
        "summary": "Any two of the Section I exclusions (lines 46-54): wear and tear, breakdown, "
                   "consequential loss, intoxication, tyres unless the vehicle was damaged"},
}


def check_sources() -> list:
    """The expected answers still match the wording they are pinned to."""
    out = []
    for n, exp in EXPECTED.items():
        line, phrase = exp["source"]
        text = WORDING[line - 1] if line <= len(WORDING) else ""
        out.append((f"Q{n} source: wording line {line} still says '{phrase}'", phrase in text, text.strip()))
    return out


def split_answers(raw: str) -> dict:
    """The four numbered answers, keyed 1-4. '**1. ...', '1.' and '1)' all count."""
    marks = list(re.finditer(r"(?m)^\s*(?:\*\*|#+\s*)?\s*([1-4])[.)]", raw or ""))
    answers = {}
    for i, m in enumerate(marks):
        n = int(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        answers.setdefault(n, raw[m.start():end])
    return answers


def grade(raw: str) -> list:
    """(check, passed, detail) for every check. No model is called."""
    results = []
    answers = split_answers(raw)
    for n, exp in EXPECTED.items():
        text = answers.get(n, "")
        results.append((f"Q{n} was answered", bool(text.strip()), ""))
        if not text.strip():
            continue
        low = text.lower()
        for pattern in exp.get("must", []):
            results.append((f"Q{n} states /{pattern}/", bool(re.search(pattern, low)),
                            f"expected: {exp['summary']}"))
        if exp.get("also_one_of"):
            results.append((f"Q{n} says it is covered",
                            any(re.search(p, low) for p in exp["also_one_of"]), ""))
        if exp.get("two_of"):
            hits = [p for p in exp["two_of"] if re.search(p, low)]
            results.append((f"Q{n} names at least two Section I exclusions", len(hits) >= 2,
                            f"found {len(hits)}: {hits}"))
        # The citation must rest on a clause that can apply to this policy.
        cannot = clause_scope.inapplicable(text, PRIVATE_CAR)
        results.append((f"Q{n} cites only clauses that apply to a {clause_scope.POLICY_FORM}",
                        not cannot, "; ".join(cannot)))
    return results


def report(results: list) -> int:
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not ok else ""))
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} policy knowledge checks as expected")
    return 0 if passed == len(results) else 1


if __name__ == "__main__" and "--grade" in sys.argv:
    # Grade a saved answer: no crew, no model, no tokens.
    saved = Path(sys.argv[sys.argv.index("--grade") + 1]).read_text()
    sys.exit(report(check_sources() + grade(saved)))

from crewai import Agent, Crew, Process, Task                                          # noqa: E402
from crewai.knowledge.source.text_file_knowledge_source import TextFileKnowledgeSource  # noqa: E402

from config import describe, get_llm                                                  # noqa: E402

# Local embedder, so Knowledge does not fall back to OpenAI
EMBEDDER = {"provider": "ollama", "config": {"model_name": "nomic-embed-text"}}

# Column-aware text made by prepare_policy.py. The raw two-column PDF scrambles
# clauses when extracted directly, so the agent reads this cleaned version.
# Path is relative to the knowledge/ folder, which CrewAI requires.
policy_wording = TextFileKnowledgeSource(
    file_paths=["policy_wording.txt"],
    chunk_size=1200,     # big enough to hold a whole clause with its sub-points
    chunk_overlap=200,
)

policy_analyst = Agent(
    role="Motor Insurance Policy Analyst",
    goal="Answer coverage questions strictly from the policy wording, citing the clause for every statement.",
    backstory=(
        "You interpret the Private Car Package Policy for a claims team. You never answer "
        "from general knowledge: if the policy wording does not say it, you say it is not stated."
    ),
    llm=get_llm("text"),
    knowledge_sources=[policy_wording],
    embedder=EMBEDDER,
    verbose=True,
    allow_delegation=False,
)

QUESTIONS = """Answer each question using only the policy wording:

1. Is damage to the vehicle caused by accidental external means covered?
2. What depreciation applies to plastic parts such as bumpers when they are replaced?
3. How is depreciation applied to painting charges on a consolidated painting bill?
4. Name two situations the policy excludes from payment under Section I.

For each answer, quote or name the clause or section it comes from."""

check_task = Task(
    description=QUESTIONS,
    expected_output="Four numbered answers, each with the clause or section it is based on.",
    agent=policy_analyst,
)

crew = Crew(
    agents=[policy_analyst],
    tasks=[check_task],
    process=Process.sequential,
    verbose=True,
)

if __name__ == "__main__":
    sources = check_sources()
    if not all(ok for _, ok, _ in sources):
        # the grader would be checking against wording that has moved
        sys.exit(report(sources))
    print(f"Running policy knowledge check using {describe()}\n")
    result = crew.kickoff()

    print("\n" + "=" * 60)
    print("ANSWERS")
    print("=" * 60)
    print(result.raw)

    print("\n" + "=" * 60)
    print("GRADED against the policy wording")
    print("=" * 60)
    sys.exit(report(sources + grade(result.raw)))
