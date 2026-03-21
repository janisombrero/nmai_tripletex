"""Standalone test for Gemini parsing layer — no server, no Tripletex calls."""
import json
from agent import parse_task

prompts = [
    "We have a new employee named Anna Larsen, born 12. March 1990. Create her with email anna.larsen@example.org and start date 1. August 2026.",
    "Créez le produit Stockage cloud avec le numéro de produit 8912. Le prix est de 26850 NOK hors TVA, avec le taux standard de 25%.",
    "Create and send an invoice to the customer Ironbridge Ltd (org no. 841254546) for 28500 NOK excluding VAT. The invoice is for System Development.",
    "Opprett tre avdelinger i Tripletex: Utvikling, Administrasjon og Lager.",
    "Créez le client Montagne SARL avec le numéro d'organisation 931564153. L'adresse est Kirkegata 19, 4611 Kristiansand. E-mail: post@montagne.no.",
]

passed = 0
failed = 0

for i, prompt in enumerate(prompts, 1):
    print(f"\n{'='*60}")
    print(f"[{i}] {prompt[:60]}{'...' if len(prompt) > 60 else ''}")
    print("-" * 60)

    result = parse_task(prompt)
    task_type = result.get("task_type", "unknown")
    fields = result.get("fields", {})

    print(f"task_type : {task_type}")
    print(f"fields    : {json.dumps(fields, ensure_ascii=False, indent=2)}")

    if "confidence" in result:
        print(f"confidence: {result['confidence']}")

    if task_type != "unknown":
        print("PASS")
        passed += 1
    else:
        print("FAIL")
        failed += 1

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed out of {len(prompts)} prompts")
