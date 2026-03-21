import json
import re
import sys
import subprocess
from datetime import datetime, date
from pathlib import Path

STATE_FILE = "competition_state.json"
MEMORY_FILE = "MEMORY.md"

TIER_MAP = {
    "create_employee": 1, "create_customer": 1, "create_supplier": 1,
    "create_product": 1, "create_department": 1, "create_project": 1,
    "create_travel_expense": 1, "create_contact_person": 1, "create_asset": 1,
    "delete_employee": 1, "delete_customer": 1, "delete_supplier": 1,
    "delete_product": 1, "delete_asset": 1,
    "create_invoice": 2, "register_payment": 2, "create_credit_note": 2,
    "reverse_payment": 2, "create_order": 2, "register_hours": 2,
    "register_hours_and_invoice": 2, "update_employee": 2, "update_customer": 2,
    "update_project": 2, "close_project": 2, "create_project_invoice": 2,
    "import_bank_statement": 2, "upload_document": 2,
    "bank_reconciliation": 3, "year_end_closing": 3,
    "create_payroll_tax_reconciliation": 3, "initiate_year_end_closing": 3,
}

def load_state():
    if Path(STATE_FILE).exists():
        return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    return {
        "task_scores": {},
        "known_failures": {},
        "prompts_seen": [],
        "submission_count": {"used_today": 0, "daily_limit": 180},
        "deployment": {
            "cloud_run_url": "https://nmiai-247841323009.europe-north1.run.app",
            "project": "ainm26osl-792"
        }
    }

def save_state(state):
    state["last_updated"] = datetime.utcnow().isoformat()
    Path(STATE_FILE).write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

def count_handlers():
    try:
        content = Path("handlers.py").read_text(encoding="utf-8")
        handlers = re.findall(r'"([a-z_]+)":\s*self\.handle_', content)
        return len(handlers), handlers
    except:
        return 0, []

def update_memory_file(state):
    try:
        content = Path(MEMORY_FILE).read_text(encoding="utf-8")
    except:
        content = ""

    # Update last updated date
    today = date.today().isoformat()
    content = re.sub(
        r'<!-- Last updated: .* -->',
        f'<!-- Last updated: {today} -->',
        content
    )

    # Update task coverage section
    handler_count, handlers = count_handlers()
    scores = state.get("task_scores", {})
    perfect = [t for t, s in scores.items() if s.get("best_score", 0) >= 1.0]
    failing = [t for t, s in scores.items() if s.get("best_score", 0) == 0 and s.get("attempts", 0) > 0]

    coverage_block = f"""## Task Coverage
<!-- AUTO-UPDATED -->
{handler_count} handlers in dispatch()
Confirmed perfect: {", ".join(perfect) if perfect else "none yet"}
Confirmed failing: {", ".join(failing) if failing else "none"}
Total unique tasks seen: {len(state.get("prompts_seen", []))}
"""
    content = re.sub(
        r'## Task Coverage.*?(?=## |\Z)',
        coverage_block + "\n",
        content,
        flags=re.DOTALL
    )

    Path(MEMORY_FILE).write_text(content, encoding="utf-8")
    print(f"✅ MEMORY.md updated")

def update_from_logs(log_file):
    state = load_state()

    try:
        logs = Path(log_file).read_text(encoding="utf-8")
    except:
        print(f"Could not read {log_file}")
        return

    # Extract task data from logs
    task_pattern = re.findall(
        r'Incoming prompt: (.+?)\n.*?Task parsed: type=(\w+).*?\n.*?Handler result: (.+)',
        logs, re.DOTALL
    )

    # Simpler extraction
    prompts = re.findall(r'Incoming prompt: (.+)', logs)
    task_types = re.findall(r'Parsed task_type=(\w+)', logs)
    results = re.findall(r"Final solver result: (\{.+\})", logs)
    errors = re.findall(r"ERROR.*?validationMessages: (.+)", logs)

    new_tasks = 0
    for i, task_type in enumerate(task_types):
        if task_type == "unknown":
            continue

        result_str = results[i] if i < len(results) else ""
        success = "'success': True" in result_str

        # Initialize task if new
        if task_type not in state["task_scores"]:
            state["task_scores"][task_type] = {
                "tier": TIER_MAP.get(task_type, 2),
                "attempts": 0,
                "best_score": 0.0,
                "status": "unknown",
                "last_error": None
            }

        state["task_scores"][task_type]["attempts"] += 1

        if success:
            # We don't know exact score without sandbox verification
            # Mark as at least partial
            if state["task_scores"][task_type]["best_score"] == 0:
                state["task_scores"][task_type]["best_score"] = 0.5
            state["task_scores"][task_type]["status"] = "partial"
        else:
            state["task_scores"][task_type]["status"] = "failing"
            # Extract error
            if errors:
                state["task_scores"][task_type]["last_error"] = errors[0][:200]

        # Record prompt
        prompt = prompts[i] if i < len(prompts) else ""
        if prompt:
            existing = [p.get("prompt", "")[:60] for p in state["prompts_seen"]]
            if prompt[:60] not in existing:
                state["prompts_seen"].append({
                    "task_type": task_type,
                    "prompt": prompt,
                    "language": detect_language(prompt),
                    "result": "success" if success else "failed",
                    "timestamp": datetime.utcnow().isoformat()
                })
                new_tasks += 1

    save_state(state)
    update_memory_file(state)
    print(f"State updated — {len(task_types)} tasks processed, {new_tasks} new prompts recorded")

def detect_language(prompt):
    if any(w in prompt.lower() for w in ["registre", "gere", "fatura", "horas"]):
        return "pt"
    if any(w in prompt.lower() for w in ["créez", "enregistrez", "facture"]):
        return "fr"
    if any(w in prompt.lower() for w in ["erstellen", "registrieren", "rechnung"]):
        return "de"
    if any(w in prompt.lower() for w in ["crea", "registra", "factura"]):
        return "es"
    if any(w in prompt.lower() for w in ["opprett", "fakturer", "prosjekt"]):
        return "nb"
    if any(w in prompt.lower() for w in ["lager", "utvikling", "fakturaen gjeld"]):
        return "nn"
    return "en"

def print_state():
    state = load_state()
    scores = state.get("task_scores", {})
    handler_count, _ = count_handlers()

    print("\n" + "="*60)
    print("NM i AI 2026 — COMPETITION STATE")
    print("="*60)
    print(f"Submissions today: {state.get('submission_count', {}).get('used_today', 0)}/180")
    print(f"Handlers implemented: {handler_count}/30")
    print(f"Unique prompts seen: {len(state.get('prompts_seen', []))}")
    print(f"Last updated: {state.get('last_updated', 'never')}")

    if scores:
        print("\n--- TASK SCORES ---")
        perfect = [(t, s) for t, s in scores.items() if s.get("best_score", 0) >= 1.0]
        partial = [(t, s) for t, s in scores.items() if 0 < s.get("best_score", 0) < 1.0]
        failing = [(t, s) for t, s in scores.items() if s.get("best_score", 0) == 0 and s.get("attempts", 0) > 0]

        if perfect:
            print(f"\n✅ PERFECT ({len(perfect)}):")
            for t, s in perfect:
                print(f"   {t} (Tier {s.get('tier',1)}, {s.get('attempts',0)} attempts)")

        if partial:
            print(f"\n⚠️  PARTIAL ({len(partial)}):")
            for t, s in partial:
                print(f"   {t} (Tier {s.get('tier',1)}, score={s.get('best_score',0):.2f})")

        if failing:
            print(f"\n❌ FAILING ({len(failing)}):")
            for t, s in failing:
                err = s.get("last_error", "unknown error")
                print(f"   {t} (Tier {s.get('tier',1)}): {str(err)[:80]}")

        # Priority fix list
        fixable = [(t, s) for t, s in scores.items()
                   if s.get("best_score", 0) < 1.0 and s.get("attempts", 0) > 0]
        fixable.sort(key=lambda x: x[1].get("tier", 1) * (1 - x[1].get("best_score", 0)), reverse=True)

        if fixable:
            print(f"\n🎯 FIX PRIORITY (highest value first):")
            for t, s in fixable[:5]:
                tier = s.get("tier", 1)
                best = s.get("best_score", 0)
                max_gain = tier * (1 - best) * 2
                print(f"   {t} — Tier {tier}, max gain={max_gain:.1f}pts")

    print("\n--- UNKNOWN TASKS (not yet seen) ---")
    known = set(scores.keys())
    all_known = set(TIER_MAP.keys())
    unseen = all_known - known
    if unseen:
        for t in sorted(unseen):
            print(f"   {t} (Tier {TIER_MAP.get(t, '?')})")

    print("="*60 + "\n")

def increment_submissions(count=1):
    state = load_state()
    state["submission_count"]["used_today"] = \
        state["submission_count"].get("used_today", 0) + count
    save_state(state)
    print(f"Submissions: {state['submission_count']['used_today']}/180")

def record_finding(task_type, finding, status="unresolved"):
    state = load_state()
    if "known_failures" not in state:
        state["known_failures"] = {}
    if task_type not in state["known_failures"]:
        state["known_failures"][task_type] = {"findings": [], "status": status}
    state["known_failures"][task_type]["findings"].append({
        "finding": finding,
        "timestamp": datetime.utcnow().isoformat()
    })
    state["known_failures"][task_type]["status"] = status
    save_state(state)
    update_memory_file(state)
    print(f"Finding recorded for {task_type}: {finding[:60]}")

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "logs":
        update_from_logs(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "finding":
        record_finding(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2 and sys.argv[1] == "submit":
        increment_submissions()
    else:
        print_state()
