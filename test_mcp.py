"""
Test the ClaimSight MCP server the way a client sees it: launch it over stdio,
list its tools, call each one, and check every answer matches the tool
function called directly. Also checks that bad inputs are rejected cleanly.
No LLM involved.

Run:  uv run python test_mcp.py
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src" / "tools"))
import assessment_tools as direct   # noqa: E402

SERVER = StdioServerParameters(command=sys.executable, args=[str(ROOT / "src" / "mcp_server.py")])


def payload(result) -> dict:
    """The JSON a tool returned. A schema violation (e.g. amount "lots") is
    rejected by MCP itself before our code runs; it comes back as isError."""
    if getattr(result, "isError", False):
        return {"error": result.content[0].text.splitlines()[0]}
    if getattr(result, "structuredContent", None):
        data = result.structuredContent
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


async def main() -> None:
    claim = json.loads((ROOT / "data" / "claims" / "CLM-5004.json").read_text())
    dup = json.loads((ROOT / "data" / "claims" / "CLM-5008.json").read_text())
    lines = [{"description": i["description"], "amount": i["amount"]} for i in claim["line_items"]]
    settle_lines = [{k: i[k] for k in ("description", "amount", "material", "admitted", "reason")}
                    for i in claim["line_items"]]
    dup_parts = dup["ground_truth"]["damaged_parts"]

    cases = [
        ("check_policy_period", {"policy_no": claim["policy_no"], "incident_date": claim["incident_date"]},
         direct.check_policy_period(claim["policy_no"], claim["incident_date"])),
        ("check_part_prices", {"line_items": lines}, direct.check_part_prices(lines)),
        ("find_prior_claims", {"policy_no": dup["policy_no"], "claim_id": dup["claim_id"],
                               "incident_date": dup["incident_date"], "damaged_parts": dup_parts},
         direct.find_prior_claims(dup["policy_no"], dup["claim_id"], dup["incident_date"], dup_parts)),
        ("calculate_settlement", {"policy_no": claim["policy_no"], "line_items": settle_lines},
         direct.settle(claim["policy_no"], settle_lines)),
    ]
    bad_inputs = [
        ("check_policy_period", {"policy_no": "DROP TABLE", "incident_date": "2026-05-01"}),
        ("check_policy_period", {"policy_no": "POL-3004", "incident_date": "yesterday"}),
        ("check_part_prices", {"line_items": [{"description": "Bumper", "amount": "lots"}]}),
        ("calculate_settlement", {"policy_no": "POL-3004", "line_items": []}),
    ]

    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            print(f"Server exposes {len(listed.tools)} tools:")
            for tool in listed.tools:
                params = ", ".join(tool.inputSchema.get("properties", {}))
                print(f"  {tool.name}({params})")

            print("\nSame answer over MCP as calling the function directly?")
            passed = 0
            for name, args, expected in cases:
                got = payload(await session.call_tool(name, args))
                same = json.loads(json.dumps(got)) == json.loads(json.dumps(expected, default=str))
                passed += same
                print(f"  {'PASS' if same else 'FAIL'}  {name}")
            status = payload(await session.call_tool("get_claim_status", {"claim_id": "CLM-5004"}))
            print(f"  ----  get_claim_status(CLM-5004) -> {status.get('status')}")

            print("\nBad inputs rejected with a clear error?")
            rejected = 0
            for name, args in bad_inputs:
                got = payload(await session.call_tool(name, args))
                ok = "error" in got
                rejected += ok
                print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got.get('error', got)}")

    print(f"\n{passed}/{len(cases)} tools match, {rejected}/{len(bad_inputs)} bad inputs rejected")
    sys.exit(0 if passed == len(cases) and rejected == len(bad_inputs) else 1)


if __name__ == "__main__":
    asyncio.run(main())
