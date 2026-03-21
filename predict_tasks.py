import os
import re
import json
from google import genai

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GOOGLE_API_KEY)

# ---------------------------------------------------------------------------
# Step 1 — Load openapi and extract endpoints
# ---------------------------------------------------------------------------
with open("openapi_sandbox.json") as f:
    spec = json.load(f)

endpoints = []
for path, methods in spec.get("paths", {}).items():
    for method, details in methods.items():
        if method in ("get", "post", "put", "delete", "patch"):
            summary = details.get("summary", "")
            endpoints.append(f"{method.upper()} {path} — {summary}")

print(f"Total API endpoints: {len(endpoints)}")

# ---------------------------------------------------------------------------
# Step 2 — Load implemented task types from dispatch() in handlers.py
# ---------------------------------------------------------------------------
with open("handlers.py") as f:
    content = f.read()

implemented = re.findall(r'"([a-z_]+)":\s*self\.handle_', content)
# deduplicate while preserving order
seen = set()
implemented = [x for x in implemented if not (x in seen or seen.add(x))]

print(f"Currently implemented: {len(implemented)} task types")
print(implemented)
print()

# ---------------------------------------------------------------------------
# Step 3 — Ask Gemini to predict missing task types
# ---------------------------------------------------------------------------
prompt = f"""
You are analyzing NM i AI 2026, a Norwegian AI competition.
The Tripletex accounting agent task has exactly 30 task types across 3 tiers.

Available Tripletex API endpoints:
{chr(10).join(endpoints[:150])}

Currently implemented task types ({len(implemented)} total):
{implemented}

Competition context:
- Tier 1: Simple single-entity operations (create, delete basic entities)
- Tier 2: Multi-step workflows (invoice with payment, project with billing)
- Tier 3: Complex scenarios (bank reconciliation, salary runs, year-end closing)
- Prompts come in 7 languages: Norwegian (nb/nn), English, Spanish, Portuguese, German, French
- Tasks seen in logs: create_supplier, reverse_payment, create_project_invoice (fixed price + partial billing)

We need {30 - len(implemented)} more task types to reach 30.

For each predicted missing task type provide:
1. task_type (snake_case, exact string to add to dispatch)
2. Example prompt in Norwegian
3. Primary API endpoint
4. Tier (1, 2, or 3)
5. Confidence (high/medium/low)
6. Why you think this exists

Focus especially on:
- Salary and payroll operations
- Bank reconciliation workflows
- Asset management
- Purchase orders vs sales orders
- Project billing variants
- Employee contract management
- Budget operations
- Ledger/voucher postings
- Year-end operations
- Anything that uses DELETE endpoints not yet covered

Order by confidence then tier.
"""

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt,
)
print("=" * 70)
print("GEMINI PREDICTIONS")
print("=" * 70)
print(response.text)
print()

# ---------------------------------------------------------------------------
# Step 4 — Find gaps: task types in agent.py system prompt not in dispatch
# ---------------------------------------------------------------------------
with open("agent.py") as f:
    agent_content = f.read()

# Extract task_type identifiers from the SYSTEM_PROMPT block
# They appear as "- task_type_name:" in the prompt string
agent_types = re.findall(r'[-•]\s+([a-z_]+):\s', agent_content)
agent_types = list(dict.fromkeys(agent_types))  # deduplicate, preserve order

implemented_set = set(implemented)
gaps = [t for t in agent_types if t not in implemented_set]

print("=" * 70)
print("GAPS: in agent.py system prompt but NOT in dispatch()")
print("=" * 70)
if gaps:
    for g in gaps:
        print(f"  MISSING HANDLER: {g}")
else:
    print("  None — all agent task types have handlers.")
print()

# Also report reverse: in dispatch but not mentioned in agent prompt
undocumented = [t for t in implemented if t not in set(agent_types)]
print("=" * 70)
print("UNDOCUMENTED: in dispatch() but NOT in agent.py system prompt")
print("=" * 70)
if undocumented:
    for u in undocumented:
        print(f"  NO PROMPT ENTRY: {u}")
else:
    print("  None — all handlers are documented in the agent prompt.")
