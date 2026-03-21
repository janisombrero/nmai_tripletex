import time
import json
import requests
import sys
import logging
import random

rand_id = random.randint(1000, 9999)

# SETUP:
AGENT_URL = "http://localhost:8000/solve"
TRIPLETEX_BASE_URL = "https://kkpqfuj-amager.tripletex.dev/v2"
SESSION_TOKEN = "eyJ0b2tlbklkIjoyMTQ3NjM0ODkxLCJ0b2tlbiI6IjQzZjM3ZjVjLTAyZGEtNGUwZC1hMWYwLWNkMjUwMWE0ZDczMyJ9"
SANDBOX_AVAILABLE = True

class TripletexDebugClient:
    auth = ("0", SESSION_TOKEN)
    
    def get(self, path, params=None):
        try:
            resp = requests.get(f"{TRIPLETEX_BASE_URL}{path}", auth=self.auth, params=params or {}, timeout=15)
            if resp.status_code == 403:
                print(f"  ❌ Tripletex returned 403 Forbidden (check your token)")
            return resp.status_code, resp.json() if resp.content else {}
        except Exception as e:
            print(f"  ❌ API GET error: {e}")
            return 0, {}
    
    def snapshot(self):
        state = {}
        for path, key, fields in [
            ("/employee", "employees", "id,firstName,lastName,email"),
            ("/customer", "customers", "id,name,organizationNumber"),
            ("/supplier", "suppliers", "id,name,organizationNumber"),
            ("/product", "products", "id,name,number,priceExcludingVatCurrency"),
            ("/project", "projects", "id,name,customer,projectManager"),
            ("/department", "departments", "id,name"),
            ("/invoice", "invoices", "id,invoiceNumber,customer,amountExcludingVat,isCharged,invoiceDueDate"),
            ("/travelExpense", "travelExpenses", "id,employee,date,description"),
            ("/order", "orders", "id,customer,orderDate"),
        ]:
            params = {"count": 1000, "fields": fields}
            if path == "/invoice":
                params["invoiceDateFrom"] = "2020-01-01"
                params["invoiceDateTo"] = "2030-12-31"
            elif path == "/order":
                params["orderDateFrom"] = "2020-01-01"
                params["orderDateTo"] = "2030-12-31"
            status, data = self.get(path, params)
            if status == 200:
                state[key] = data.get("values", [])
            else:
                state[key] = []
        return state
    
    def diff(self, before, after):
        result = {}
        for key in after:
            before_ids = {e["id"] for e in before.get(key, []) if "id" in e}
            new_items = [e for e in after.get(key, []) if e.get("id") not in before_ids]
            if new_items:
                result[key] = new_items
        return result

class ScoringDebugger:
    
    SCORER_CHECKS = {
        "create_employee": [
            ("employee_found", 2, lambda diff, fields: len(diff.get("employees", [])) > 0),
        ],
        "create_customer": [
            ("customer_found", 2, lambda diff, fields: len(diff.get("customers", [])) > 0),
        ],
        "create_invoice": [
            ("invoice_found", 2, lambda diff, fields: len(diff.get("invoices", [])) > 0),
        ],
        "order_invoice_payment_chain": [
            ("invoice_found", 2, lambda diff, fields: len(diff.get("invoices", [])) > 0),
        ],
        "register_hours_and_invoice": [
            ("invoice_found", 2, lambda diff, fields: len(diff.get("invoices", [])) > 0),
        ],
        "create_project_invoice": [
            ("project_found", 2, lambda diff, fields: len(diff.get("projects", [])) > 0),
        ],
        "create_supplier": [
            ("supplier_found", 2, lambda diff, fields: len(diff.get("suppliers", [])) > 0),
        ],
        "create_product": [
            ("product_found", 2, lambda diff, fields: len(diff.get("products", [])) > 0),
        ],
    }
    
    TIER_MULTIPLIER = {
        "create_employee": 1,
        "create_customer": 1,
        "create_supplier": 1,
        "create_product": 1,
        "create_department": 1,
        "create_project": 1,
        "create_travel_expense": 1,
        "create_invoice": 2,
        "register_payment": 2,
        "create_credit_note": 2,
        "reverse_payment": 2,
        "create_order": 2,
        "register_hours_and_invoice": 2,
        "order_invoice_payment_chain": 2,
        "create_project_invoice": 3,
        "bank_reconciliation": 3,
        "year_end_closing": 3,
    }
    
    def __init__(self):
        self.client = TripletexDebugClient()
    
    def run_task(self, task_type, prompt, expected_fields=None):
        print(f"\nTASK: {task_type}")
        print(f"PROMPT: {prompt[:100]}...")
        
        before = self.client.snapshot()
        try:
            resp = requests.post(AGENT_URL, json={
                "prompt": prompt,
                "tripletex_credentials": {
                    "base_url": TRIPLETEX_BASE_URL,
                    "session_token": SESSION_TOKEN
                }
            }, timeout=120)
            agent_response = resp.json()
            print(f"⏱ Response: {json.dumps(agent_response)[:200]}")
        except Exception as e:
            print(f"❌ Agent call failed: {e}")
            return
        
        after = self.client.snapshot()
        diff = self.client.diff(before, after)
        
        checks = self.SCORER_CHECKS.get(task_type, [])
        points_earned = 0
        points_possible = sum(pts for _, pts, _ in checks)
        
        for check_name, points, check_fn in checks:
            passed = check_fn(diff, expected_fields or {})
            icon = "✅" if passed else "❌"
            points_earned += points if passed else 0
            print(f"  {icon} {check_name} ({points}pts)")
        
        return agent_response

# PREDEFINED TEST CASES
TEST_CASES = [
    {
        "task_type": "create_employee",
        "prompt": f"New employee: Jane Doe{rand_id}, born 12.03.1990, email jane{rand_id}@example.com, starts 2026-08-01.",
    },
    {
        "task_type": "create_customer",
        "prompt": f"Create customer: Acme Corp {rand_id}, org 912345678.",
    },
    {
        "task_type": "create_invoice",
        "prompt": "Create and send invoice to customer 'Acme AS' for 1500 NOK. Description: Consulting.",
    },
    {
        "task_type": "order_invoice_payment_chain",
        "prompt": "Create order for customer 'Internal' with product 'Cloud Storage' (4257) at 500 NOK. Convert to invoice and register payment.",
    },
    {
        "task_type": "register_hours_and_invoice",
        "prompt": f"Log 4 hours for ojanio@gmail.com on activity 'Fakturerbart arbeid' in project Internal. Rate 1000 NOK/h. Invoice Acme AS.",
    },
    {
        "task_type": "create_project_invoice",
        "prompt": "Create project 'Sandbox' for customer 'Internal', fixed price 10000 NOK. Invoice 100%.",
    },
    {
        "task_type": "create_supplier",
        "prompt": f"Register supplier: Global Parts {rand_id}, org 888777666.",
    },
    {
        "task_type": "create_product",
        "prompt": f"New product: Service {rand_id}, price 1200 NOK.",
    },
]

def main():
    debugger = ScoringDebugger()
    for tc in TEST_CASES:
        debugger.run_task(tc["task_type"], tc["prompt"])
        time.sleep(1)

if __name__ == "__main__":
    main()
