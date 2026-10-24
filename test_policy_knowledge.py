"""
Step 1: check that the Policy Analyst can retrieve answers from the policy wording.

The policy PDF is attached as agent-level Knowledge. Embeddings run locally on
Ollama (nomic-embed-text), so no OpenAI key is needed.

Run:  uv run python test_policy_knowledge.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

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
    print(f"Running policy knowledge check using {describe()}\n")
    result = crew.kickoff()

    print("\n" + "=" * 60)
    print("ANSWERS")
    print("=" * 60)
    print(result.raw)

    print("\n" + "=" * 60)
    print("EXPECTED (from the policy wording)")
    print("=" * 60)
    print("1. Yes - Section I, peril 6: 'by accidental external means'")
    print("2. 50% - rubber/nylon/plastic parts, tyres, batteries, air bags")
    print("3. 50% on the material cost only; material taken as 25% of the painting bill")
    print("4. Any two of: wear and tear, mechanical/electrical breakdown, consequential loss,")
    print("   driving under the influence, tyres unless vehicle damaged at the same time")
