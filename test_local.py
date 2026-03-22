"""
Local test suite for the NM i AI 2026 Tripletex agent.
Simulates exactly how the scorer calls the app.

Usage:
    # Terminal 1: start the server
    uvicorn main:app --host 0.0.0.0 --port 8000

    # Terminal 2: run all tests
    python test_local.py

    # Run a single test by number
    python test_local.py --test 6
"""

import argparse
import json
import sys
import time
import random

import requests

BASE_URL = "http://localhost:8000"
SOLVE_URL = f"{BASE_URL}/solve"

SANDBOX_BASE = "https://kkpqfuj-amager.tripletex.dev/v2"
SANDBOX_TOKEN = (
    "eyJ0b2tlbklkIjoyMTQ3NjM0ODkxLCJ0b2tlbiI6IjQzZjM3ZjVjLTAyZGEtNGUwZC1hMWYwLWNkMjUwMWE0ZDczMyJ9"
)
SANDBOX_AUTH = ("0", SANDBOX_TOKEN)

CREDENTIALS = {
    "base_url": SANDBOX_BASE,
    "session_token": SANDBOX_TOKEN,
}

DELAY_BETWEEN = 2  # seconds between tests to avoid Claude rate limiting


# ---------------------------------------------------------------------------
# Sandbox direct-API helpers (used for pre-test setup)
# ---------------------------------------------------------------------------

def sandbox_get(path, **params):
    return requests.get(
        f"{SANDBOX_BASE}{path}", auth=SANDBOX_AUTH, params=params,
        headers={"Accept": "application/json"}, timeout=15,
    )


def sandbox_post(path, body):
    return requests.post(
        f"{SANDBOX_BASE}{path}", auth=SANDBOX_AUTH, json=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=15,
    )


def sandbox_put(path, body=None, **params):
    return requests.put(
        f"{SANDBOX_BASE}{path}", auth=SANDBOX_AUTH, json=body, params=params,
        headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=15,
    )


# ---------------------------------------------------------------------------
# Pre-test setup: create a real invoice so tests 7 and 10 have something to work with
# ---------------------------------------------------------------------------

def _sandbox_value_id(r: requests.Response, label: str) -> int | None:
    """Extract id from a Tripletex {value: {id: ...}} response, with full logging on failure."""
    try:
        body = r.json()
    except Exception:
        print(f"  [setup] {label}: non-JSON response {r.status_code}: {r.text[:300]}")
        return None
    if r.status_code not in (200, 201):
        print(f"  [setup] {label}: HTTP {r.status_code} body={json.dumps(body)[:300]}")
        return None
    val = body.get("value") or {}
    id_ = val.get("id") if isinstance(val, dict) else None
    if id_ is None:
        print(f"  [setup] {label}: unexpected body (no value.id): {json.dumps(body)[:300]}")
    return id_


def find_or_create_customer(name: str) -> int | None:
    r = sandbox_get("/customer", name=name, fields="id,name", count=1)
    customers = r.json().get("values", [])
    if customers:
        return customers[0]["id"]
    r = sandbox_post("/customer", {"name": name})
    return _sandbox_value_id(r, f"POST /customer name={name!r}")


def create_test_invoice(customer_name: str, amount: float, description: str) -> dict | None:
    """Create a real open invoice in the sandbox and return {id, invoiceNumber}."""
    print(f"  [setup] Creating test invoice for '{customer_name}' ({amount} NOK)...")

    customer_id = find_or_create_customer(customer_name)
    if not customer_id:
        print(f"  [setup] Could not find or create customer '{customer_name}' — skipping invoice")
        return None
    print(f"  [setup] Customer id={customer_id}")

    r = sandbox_post("/order", {
        "customer": {"id": customer_id},
        "orderDate": "2026-03-20",
        "deliveryDate": "2026-03-20",
        "orderLines": [{"description": description, "count": 1, "unitPriceExcludingVatCurrency": amount}],
    })
    order_id = _sandbox_value_id(r, "POST /order")
    if not order_id:
        return None
    print(f"  [setup] Created order id={order_id}")

    r = sandbox_put(f"/order/{order_id}/:invoice", invoiceDate="2026-03-20", sendToCustomer="false")
    if r.status_code not in (200, 201):
        print(f"  [setup] Invoice creation failed: {r.status_code} {r.text[:300]}")
        return None

    inv = (r.json().get("value") or {})
    if not inv.get("id"):
        print(f"  [setup] Invoice response missing value.id: {r.text[:300]}")
        return None
    print(f"  [setup] Invoice id={inv['id']} number={inv.get('invoiceNumber')}")
    return {"id": inv["id"], "invoiceNumber": inv.get("invoiceNumber")}


# ---------------------------------------------------------------------------
# Sandbox verification helpers (direct API, called after agent solve)
# ---------------------------------------------------------------------------

def _pp(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _get_invoice_baseline(customer_id: int) -> int:
    """Return the highest invoice ID currently in the sandbox for a customer."""
    r = sandbox_get(
        "/invoice",
        customerId=customer_id,
        invoiceDateFrom="2026-01-01",
        invoiceDateTo="2026-12-31",
        count=1000,
        fields="id",
    )
    invoices = r.json().get("values", [])
    return max((inv.get("id", 0) for inv in invoices), default=0)


def _get_product_baseline(name: str) -> int:
    """Return the highest product ID currently matching name in the sandbox."""
    r = sandbox_get("/product", name=name, count=100, fields="id")
    products = r.json().get("values", [])
    return max((p.get("id", 0) for p in products), default=0)


def verify_invoice(baseline_id: int, customer_id: int | None) -> bool:
    """
    Find the invoice created by the agent (id > baseline_id) and verify:
    - invoice exists
    - customer is linked (id not null)
    - associated order line matching 'System Development' or unitCostCurrency≈28500:
        count=1, unitCostCurrency=28500
    - invoiceDueDate is set
    Prints the full invoice object.
    """
    print(f"\n  [verify] baseline_id={baseline_id}  customer_id={customer_id}")

    if not customer_id:
        # Look up customer now as fallback
        r = sandbox_get("/customer", organizationNumber="841254546", fields="id,name", count=5)
        customers = r.json().get("values", [])
        if not customers:
            r = sandbox_get("/customer", name="Ironbridge Ltd", fields="id,name", count=5)
            customers = r.json().get("values", [])
        if not customers:
            print("  [verify] FAIL — customer Ironbridge Ltd not found in sandbox")
            return False
        customer_id = customers[0]["id"]
        print(f"  [verify] Resolved customer_id={customer_id}")

    # Fetch all invoices for this customer
    r = sandbox_get(
        "/invoice",
        customerId=customer_id,
        invoiceDateFrom="2026-01-01",
        invoiceDateTo="2026-12-31",
        count=1000,
        fields="id,invoiceNumber,invoiceDate,invoiceDueDate,customer,orders",
    )
    invoices = r.json().get("values", [])
    print(f"  [verify] Total invoices for customer: {len(invoices)}")

    # Filter to only those created after our baseline
    new_invoices = [inv for inv in invoices if inv.get("id", 0) > baseline_id]
    print(f"  [verify] New invoices (id > {baseline_id}): {[inv.get('id') for inv in new_invoices]}")

    if not new_invoices:
        print("  [verify] FAIL — no new invoice found with id > baseline")
        return False

    # Among new invoices, prefer the one whose order lines mention System Development or 28500
    invoice_id = None
    for inv in sorted(new_invoices, key=lambda x: x.get("id", 0), reverse=True):
        order_ids = [o.get("id") for o in (inv.get("orders") or []) if o.get("id")]
        for oid in order_ids:
            r2 = sandbox_get(f"/order/{oid}", fields="id,orderLines")
            if r2.status_code == 200:
                lines = r2.json().get("value", {}).get("orderLines", [])
                for line in lines:
                    desc = (line.get("description") or "").lower()
                    cost = float(line.get("unitCostCurrency") or 0)
                    if "system development" in desc or abs(cost - 28500) < 1:
                        invoice_id = inv["id"]
                        break
            if invoice_id:
                break
        if invoice_id:
            break

    # Fallback: use highest new invoice if no order line match
    if not invoice_id:
        invoice_id = max(new_invoices, key=lambda x: x.get("id", 0))["id"]
        print(f"  [verify] WARN no order line match — using highest new invoice id={invoice_id}")
    else:
        print(f"  [verify] Matched agent invoice id={invoice_id}")

    # Fetch full invoice details
    r = sandbox_get(f"/invoice/{invoice_id}", fields="*")
    if r.status_code != 200:
        print(f"  [verify] FAIL — GET /invoice/{invoice_id} returned {r.status_code}")
        return False
    full_invoice = r.json().get("value", {})
    print(f"\n  [verify] Full invoice object:\n{_pp(full_invoice)}\n")

    checks_passed = True

    # Check 1: customer is linked
    inv_customer = full_invoice.get("customer") or {}
    inv_customer_id = inv_customer.get("id")
    inv_customer_name = inv_customer.get("name", "")
    if inv_customer_id:
        print(f"  [verify] OK  customer id={inv_customer_id} name={inv_customer_name!r}")
    else:
        print("  [verify] FAIL customer.id is null on invoice")
        checks_passed = False

    # Check 2: invoiceDueDate
    due_date = full_invoice.get("invoiceDueDate") or full_invoice.get("dueDate")
    if due_date:
        print(f"  [verify] OK  invoiceDueDate = {due_date!r}")
    else:
        print("  [verify] FAIL invoiceDueDate is not set")
        checks_passed = False

    # Check 3: order line values
    order_ids = [o.get("id") for o in (full_invoice.get("orders") or []) if o.get("id")]
    if not order_ids:
        print("  [verify] WARN no order IDs on invoice — skipping order line check")
    else:
        order_id = order_ids[0]
        r2 = sandbox_get(f"/order/{order_id}", fields="id,orderLines")
        if r2.status_code == 200:
            order_obj = r2.json().get("value", {})
            order_lines = order_obj.get("orderLines", [])
            print(f"\n  [verify] Order {order_id} lines ({len(order_lines)} found):")
            for i, line in enumerate(order_lines):
                print(f"    line[{i}]: count={line.get('count')}  "
                      f"unitCostCurrency={line.get('unitCostCurrency')}  "
                      f"desc={line.get('description', '')!r}")
            if order_lines:
                line0 = order_lines[0]
                count_ok = abs(float(line0.get("count") or 0) - 1.0) < 0.01
                cost_ok = abs(float(line0.get("unitCostCurrency") or 0) - 28500.0) < 0.01
                print(f"  [verify] {'OK ' if count_ok else 'FAIL'} count = {line0.get('count')} (expected 1)")
                print(f"  [verify] {'OK ' if cost_ok else 'FAIL'} unitCostCurrency = {line0.get('unitCostCurrency')} (expected 28500)")
                if not count_ok:
                    checks_passed = False
                if not cost_ok:
                    checks_passed = False
            else:
                print("  [verify] WARN order has no order lines")
        else:
            print(f"  [verify] WARN could not fetch order {order_id}: {r2.status_code}")

    return checks_passed


def verify_product(baseline_id: int) -> bool:
    """
    Find the product 'Stockage cloud' created after baseline_id and verify:
    - product exists
    - price = 26850
    - vatType is set
    Prints the full product object.
    """
    print(f"\n  [verify] baseline_id={baseline_id}")

    r = sandbox_get("/product", name="Stockage cloud", count=100,
                    fields="id,name,priceExcludingVatCurrency,vatType,number")
    products = r.json().get("values", [])
    print(f"  [verify] Total 'Stockage cloud' products: {len(products)}")

    # Filter to those created after baseline
    new_products = [p for p in products if p.get("id", 0) > baseline_id]
    print(f"  [verify] New products (id > {baseline_id}): {[p.get('id') for p in new_products]}")

    if not new_products:
        # Fallback: if no new products, use any match (first run scenario)
        if products:
            print("  [verify] WARN no new products found — using highest existing")
            new_products = products
        else:
            print("  [verify] FAIL — product 'Stockage cloud' not found in sandbox")
            return False

    product = max(new_products, key=lambda x: x.get("id", 0))
    product_id = product["id"]
    print(f"  [verify] Using product id={product_id}")

    # Fetch full product details
    r = sandbox_get(f"/product/{product_id}", fields="*")
    if r.status_code != 200:
        print(f"  [verify] FAIL — GET /product/{product_id} returned {r.status_code}")
        return False
    full_product = r.json().get("value", {})
    print(f"\n  [verify] Full product object:\n{_pp(full_product)}\n")

    checks_passed = True

    # Check price
    price = (
        full_product.get("priceExcludingVatCurrency")
        or full_product.get("costExcludingVatCurrency")
        or full_product.get("price")
    )
    try:
        price_val = float(price)
    except (TypeError, ValueError):
        price_val = None

    if price_val is not None and abs(price_val - 26850.0) < 0.01:
        print(f"  [verify] OK  price = {price_val}")
    else:
        print(f"  [verify] FAIL price = {price!r} (expected 26850)")
        checks_passed = False

    # Check vatType
    vat_type = full_product.get("vatType")
    if vat_type:
        vat_id = vat_type.get("id") if isinstance(vat_type, dict) else vat_type
        vat_name = vat_type.get("name", "") if isinstance(vat_type, dict) else ""
        print(f"  [verify] OK  vatType = id={vat_id} name={vat_name!r}")
    else:
        print("  [verify] FAIL vatType is not set")
        checks_passed = False

    # Check productNumber (informational)
    pnum = full_product.get("number") or full_product.get("productNumber")
    if str(pnum) == "8912":
        print(f"  [verify] OK  productNumber = {pnum!r}")
    else:
        print(f"  [verify] WARN productNumber = {pnum!r} (expected '8912')")

    return checks_passed


# ---------------------------------------------------------------------------
# Test runner core
# ---------------------------------------------------------------------------

def solve(prompt: str) -> tuple:
    payload = {
        "prompt": prompt,
        "files": [],
        "tripletex_credentials": CREDENTIALS,
    }
    resp = requests.post(SOLVE_URL, json=payload, timeout=90)
    return resp.status_code, resp.json()


def run_test(number: int, name: str, prompt: str, before_fn=None, verify_fn=None) -> bool:
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"TEST {number}: {name}")
    print(sep)
    print(f"PROMPT:\n  {prompt}")
    print("-" * 64)

    try:
        if before_fn:
            print("  [before] Capturing baseline state...")
            try:
                before_fn()
            except Exception as be:
                print(f"  [before] WARNING: {be}")

        http_code, body = solve(prompt)
        print(f"HTTP {http_code}")
        print(f"RESPONSE:\n{json.dumps(body, indent=2, ensure_ascii=False)}")

        if http_code != 200:
            print(f"\nFAIL — HTTP {http_code}")
            return False

        status = body.get("status")
        if status != "completed":
            print(f"\nFAIL — status={status!r} (expected 'completed')")
            return False

        # success field is informational; some handlers return it, some don't.
        # The scorer checks status=="completed". We additionally flag success=False.
        success = body.get("success")
        if success is False:
            err = body.get("error", "")
            print(f"\nFAIL — success=False  error={err!r}")
            return False

        solve_passed = True

        # Run sandbox verification if provided
        verify_passed = True
        if verify_fn:
            print("\n" + "-" * 64)
            print("  SANDBOX VERIFICATION")
            print("-" * 64)
            try:
                verify_passed = verify_fn()
            except Exception as ve:
                print(f"  [verify] ERROR during verification: {ve}")
                verify_passed = False
            print(f"\n  Verification: {'OK' if verify_passed else 'FAILED'}")

        overall = solve_passed and verify_passed
        print(f"\n{'PASS' if overall else 'FAIL'}")
        return overall

    except requests.exceptions.ConnectionError:
        print("FAIL — cannot connect to localhost:8000")
        print("       Start the server: uvicorn main:app --host 0.0.0.0 --port 8000")
        return False
    except Exception as exc:
        print(f"FAIL — exception: {exc}")
        return False


# ---------------------------------------------------------------------------
# The 10 tests (prompts exactly as specified)
# ---------------------------------------------------------------------------

def build_tests(payment_invoice_number, credit_note_invoice_id) -> list[dict]:
    rand_id = random.randint(1000, 9999)
    # Shared state for test 5 (product) and test 6 (invoice) verifications.
    # before_fn captures baseline just before agent runs; verify_fn uses it.
    t5_ctx: dict = {}
    t6_ctx: dict = {}

    def before_product():
        t5_ctx["baseline_id"] = _get_product_baseline("Stockage cloud")
        print(f"  [before] Product baseline_id = {t5_ctx['baseline_id']}")

    def before_invoice():
        # Find (or confirm) Ironbridge Ltd customer ID
        r = sandbox_get("/customer", organizationNumber="841254546", fields="id,name", count=5)
        customers = r.json().get("values", [])
        if not customers:
            r = sandbox_get("/customer", name="Ironbridge Ltd", fields="id,name", count=5)
            customers = r.json().get("values", [])
        customer_id = customers[0]["id"] if customers else None
        t6_ctx["customer_id"] = customer_id
        t6_ctx["baseline_id"] = _get_invoice_baseline(customer_id) if customer_id else 0
        print(f"  [before] Invoice baseline_id = {t6_ctx['baseline_id']}  customer_id = {customer_id}")

    def verify_product_t5():
        return verify_product(t5_ctx.get("baseline_id", 0))

    def verify_invoice_t6():
        return verify_invoice(t6_ctx.get("baseline_id", 0), t6_ctx.get("customer_id"))

    return [
        {
            "number": 1,
            "name": "create_employee",
            "prompt": (
                f"We have a new employee named Anna Larsen{rand_id}, born 12. March 1990. "
                f"Create her with email anna.larsen{rand_id}@example.org and start date 1. August 2026. "
                "Department is IT Services, occupation code is 1234567, salary is 600000, "
                "employment percentage is 100, and national identity number is 12039012345."
            ),
        },
        {
            "number": 2,
            "name": "create_customer",
            "prompt": (
                "Créez le client Montagne SARL avec le numéro d'organisation 931564153. "
                "L'adresse est Kirkegata 19, 4611 Kristiansand. E-mail: post@montagne.no."
            ),
        },
        {
            "number": 3,
            "name": "create_supplier",
            "prompt": (
                "Register the supplier Silveroak Ltd with organization number 811867500. "
                "Email: faktura@silveroakltd.no."
            ),
        },
        {
            "number": 4,
            "name": "create_department (bulk)",
            "prompt": "Opprett tre avdelinger i Tripletex: Utvikling, Administrasjon og Lager.",
        },
        {
            "number": 5,
            "name": "create_product",
            "prompt": (
                "Créez le produit Stockage cloud avec le numéro de produit 8912. "
                "Le prix est de 26850 NOK hors TVA, avec le taux standard de 25%."
            ),
            "before_fn": before_product,
            "verify_fn": verify_product_t5,
        },
        {
            "number": 6,
            "name": "create_invoice",
            "prompt": (
                "Create and send an invoice to the customer Ironbridge Ltd "
                "(org no. 841254546) for 28500 NOK excluding VAT. "
                "The invoice is for System Development."
            ),
            "before_fn": before_invoice,
            "verify_fn": verify_invoice_t6,
        },
        {
            "number": 7,
            "name": "register_payment",
            "prompt": (
                "Kunden Nordhav AS (org.nr 841333608) har en utestående faktura på "
                "14200 kr eksklusiv MVA for Skylagring. "
                "Registrer full betaling på denne fakturaen."
            ),
        },
        {
            "number": 8,
            "name": "create_project",
            "prompt": (
                "Erstellen Sie das Projekt Integration Bergwerk verknüpft mit dem Kunden "
                "Bergwerk GmbH (Org.-Nr. 986555080). "
                "Projektleiter ist Leon Meyer (leon.meyer@example.org)."
            ),
        },
        {
            "number": 9,
            "name": "create_travel_expense",
            "prompt": (
                "Register a travel expense for employee 1, date 2026-03-20, "
                "amount 450 NOK, description: Taxi to airport."
            ),
        },
        {
            "number": 10,
            "name": "create_credit_note",
            "prompt": f"Create a credit note for invoice {credit_note_invoice_id} for 1000 NOK.",
        },
        {
            "number": 11,
            "name": "reverse_payment",
            "prompt": f"Betalingen for faktura {payment_invoice_number} ble returnert av banken. Reverser betalingen.",
        },
        {
            "number": 12,
            "name": "order_invoice_payment_chain",
            "prompt": (
                "Create an order for the customer Northwave Ltd (org no. 997677889) "
                "with the products Cloud Storage (4257) at 27150 NOK and System Development (7968) at 10100 NOK. "
                "Convert the order to an invoice and register payment."
            ),
        },
        {
            "number": 13,
            "name": "create_project_invoice_partial",
            "prompt": (
                "Sett fastpris 274950 kr på prosjektet Nettbutikk-utvikling for Skogheim AS (org.nr 826912324). "
                "Prosjektleiar er Bjørn Kvamme (bjrn.kvamme@example.org). Fakturer kunden for 50% av fastprisen."
            ),
        },
        {
            "number": 14,
            "name": "register_hours_and_invoice",
            "prompt": (
                "Log 13 hours for Hannah Brown (hannah.brown@example.org) on the activity Analyse "
                "in the project App Development for Brightstone Ltd (org no. 970020896). "
                "Hourly rate: 1850 NOK/h. Generate a project invoice."
            ),
        },
        {
            "number": 15,
            "name": "agent_fallback_project",
            "prompt": (
                "Create a project named Fallback Test and link it to customer Acme. "
                "Then create an activity named Analysis for this project."
            ),
        },
        ```python
{
    "number": 401,
    "name": "Portuguese Employee Creation from Contract PDF (Simulated Details)",
    "prompt": "Voce recebeu um contrato de trabalho (ver PDF anexo). Crie o funcionario no Tripletex com todos os detalhes do contrato: numero de identidade nacional, data de nascimento, departamento, codigo de ocupacao, salario, percentagem de emprego e data de inicio.",
    "before_fn":
    def before_fn():
        # Ensure department 'Marketing' exists for robust testing.
        # The agent should ideally create it if it doesn't exist, but this ensures a clean state for verification.
        r = sandbox_get("/department", name="Marketing")
        departments = r.json().get("values", [,
        ```python
{
    "number": 1001,
    "name": "Create Employee - Portuguese Full Integration with PDF Context",
    "prompt": "Voce recebeu uma carta de oferta (ver PDF anexo) para um novo funcionario. Complete a integracao: crie o funcionario, atribua o departamento correto, configure os detalhes de emprego com percentagem e salario anual, e configure as horas de trabalho padrao.",
    "before_fn":
        def before_create_employee_portuguese_full():
            # Define expected employee details for cleanup and verification
            expected_email = "maria.silva.test@example.org"
            expected_department_name = "Sales" # Assuming agent maps 'departamento correto' to 'Sales'

            # 1. Ensure 'Sales' department exists
            r = sandbox_get("/department", name=expected_department_name)
            departments = r.json().get("values", [,
        ```python
{
    "number": 101,
    "name": "Create Employee from Portuguese Contract",
    "prompt": """Crie o funcionário João Silva no Tripletex com os seguintes detalhes do contrato:
- Número de identidade nacional: 12345678901
- Data de nascimento: 15 de maio de 1990
- Departamento: Vendas
- Código de ocupação: 2111
- Salário: 50000 NOK
- Percentagem de emprego: 100%
- Data de início: 1 de julho de 2024
- Email: joao.silva@example.org""",
    "before_fn":
        def before_fn_create_employee_pt():
            # Clean up any existing employee with the same email from previous test runs
            r = sandbox_get("/employee", email="joao.silva@example.org")
            employees = r.json().get("values", [,
        ```python
{
    "number": 1001,
    "name": "Create Employee (Portuguese) from PDF - Full Integration",
    "prompt": "Voce recebeu uma carta de oferta (ver PDF anexo) para um novo funcionario. Complete a integracao: crie o funcionario, atribua o departamento correto, configure os detalhes de emprego com percentagem e salario anual, e configure as horas de trabalho padrao.",
    "files": {
        "carta_oferta.pdf": """Offer Letter
Date: 2024-01-15

Dear Mr. Carlos Silva,

We are pleased to offer you the position of Sales Representative at Tripletex.
Your start date will be 2024-02-01.
Your annual salary will be 600000 NOK.
This is a full-time position (100% employment percentage), with standard working hours of 37.5 hours per week.
You will be part of the Sales Department.
Your work email will be carlos.silva@example.com.

Sincerely,
[Hiring Manager]"""
    },
    "before_fn":
def before_fn_1001():
    # Clean up existing employee if any to ensure a clean test environment
    employee_email = "carlos.silva@example.com"
    r_existing = sandbox_get("/employee", email=employee_email)
    existing_employees = r_existing.json().get("values", [])
    for emp in existing_employees:
        # Assuming a DELETE endpoint or a specific POST action for deletion in the sandbox
        try:
            sandbox_post(f"/employee/{emp['id']}/delete")
            print(f"Deleted existing employee: {emp['firstName']} {emp['lastName']} ({emp['id']})")
        except Exception as e:
            print(f"Could not delete existing employee {emp['id']}: {e}. This might cause test conflicts.")

    # Ensure "Sales" department exists, create it if not
    r = sandbox_get("/department", name="Sales")
    departments = r.json().get("values", [])
    if not departments:
        print("Sales department not found, creating it.")
        sandbox_post("/department", json={"name": "Sales"})
        # Verify creation
        r_check = sandbox_get("/department", name="Sales")
        if not r_check.json().get("values"):
            raise Exception("Failed to create Sales department in sandbox.")
    else:
        print("Sales department already exists.")
,
    "verify_fn":
def verify_fn_1001():
    employee_email = "carlos.silva@example.com"
    r = sandbox_get("/employee", email=employee_email)
    employees = r.json().get("values", [])

    if not employees:
        print(f"Verification failed: Employee with email {employee_email} not found.")
        return False

    employee = employees[0]

    # Verify basic employee details
    if not (employee.get("firstName") == "Carlos" and employee.get("lastName") == "Silva"):
        print(f"Verification failed: Employee name mismatch. Expected Carlos Silva, got {employee.get('firstName')} {employee.get('lastName')}")
        return False
    if not (employee.get("email") == employee_email):
        print(f"Verification failed: Employee email mismatch. Expected {employee_email}, got {employee.get('email')}")
        return False

    # Verify employment details
    if not (employee.get("annualSalary") == 600000):
        print(f"Verification failed: Annual salary mismatch. Expected 600000, got {employee.get('annualSalary')}")
        return False
    if not (employee.get("employmentPercentage") == 100):
        print(f"Verification failed: Employment percentage mismatch. Expected 100, got {employee.get('employmentPercentage')}")
        return False

    # Verify department assignment
    r_deps = sandbox_get("/department", name="Sales")
    departments = r_deps.json().get("values", [])
    if not departments:
        print("Verification failed: Sales department not found in sandbox (should have been created by before_fn).")
        return False
    sales_department_id = departments[0]["id"]

    if not (employee.get("department") and employee["department"].get("id") == sales_department_id):
        print(f"Verification failed: Department mismatch. Expected Sales (ID {sales_department_id}), got {employee.get('department')}")
        return False

    # Verify standard work hours
    # Given the partial API, we check common field names for weekly work hours.
    hours_set_correctly = False
    for key in ["weeklyWorkHours", "standardWorkHours", "hoursPerWeek"]:
        if employee.get(key) == 37.5:
            hours_set_correctly = True
            break
    
    if not hours_set_correctly:
        print(f"Verification failed: Standard work hours mismatch. Expected 37.5 in fields like 'weeklyWorkHours', 'standardWorkHours', 'hoursPerWeek'. Found: {employee.get('weeklyWorkHours')}, {employee.get('standardWorkHours')}, {employee.get('hoursPerWeek')}")
        return False

    return True
}
    ])
            if employees:
                employee_id = employees[0]["id"]
                print(f"Deleting existing employee with ID: {employee_id} and email: joao.silva@example.org")
                # Assuming a delete endpoint exists, e.g., using a PUT with :delete action
                # Or a POST to a specific delete action endpoint
                sandbox_post(f"/employee/{employee_id}/:delete", {})
            return True, "Setup complete"
        ,
    "verify_fn":
        def verify_fn_create_employee_pt():
            r = sandbox_get("/employee", email="joao.silva@example.org")
            employees = r.json().get("values", [])

            if not employees:
                print("Verification failed: Employee 'João Silva' not found.")
                return False

            employee = employees[0]

            # Verify core employee details
            if not (employee.get("firstName") == "João" and
                    employee.get("lastName") == "Silva" and
                    employee.get("email") == "joao.silva@example.org" and
                    employee.get("dateOfBirth") == "1990-05-15" and
                    employee.get("startDate") == "2024-07-01" and
                    employee.get("employmentPercentage") == 100 and
                    employee.get("salary") == 50000 and
                    employee.get("nationalIdentityNumber") == "12345678901"):
                print(f"Verification failed: Mismatch in core employee details for João Silva.")
                print(f"Expected: {{'firstName': 'João', 'lastName': 'Silva', 'email': 'joao.silva@example.org', 'dateOfBirth': '1990-05-15', 'startDate': '2024-07-01', 'employmentPercentage': 100, 'salary': 50000, 'nationalIdentityNumber': '12345678901'}}")
                print(f"Actual: {employee}")
                return False

            # Verify department
            department = employee.get("department")
            if not (department and department.get("name") == "Vendas"):
                print(f"Verification failed: Department mismatch for João Silva. Expected 'Vendas', got {department.get('name') if department else 'None'}.")
                return False

            # Verify occupation
            occupation = employee.get("occupation")
            if not (occupation and occupation.get("code") == "2111"):
                print(f"Verification failed: Occupation code mismatch for João Silva. Expected '2111', got {occupation.get('code') if occupation else 'None'}.")
                return False

            print("Verification successful: Employee 'João Silva' created with all specified details.")
            return True
        ,
}
    ])
            if not departments:
                print(f"Creating '{expected_department_name}' department...")
                r = sandbox_post("/department", body={"name": expected_department_name})
                if r.status_code != 201:
                    print(f"Failed to create '{expected_department_name}' department: {r.status_code} {r.text}")
                    raise Exception(f"Failed to create '{expected_department_name}' department")
                print(f"Created '{expected_department_name}' department.")
            else:
                print(f"'{expected_department_name}' department already exists.")

            # 2. Clean up any existing employee with the test email to ensure a fresh test
            r = sandbox_get("/employee", email=expected_email)
            employees = r.json().get("values", [])
            for emp in employees:
                print(f"Deleting existing test employee: ID {emp['id']}, Email {emp['email']}")
                sandbox_delete(f"/employee/{emp['id']}")
            print("Pre-test setup complete.")
        ,
    "verify_fn":
        def verify_employee_portuguese_integration():
            # These values are assumed to be extracted by the agent from the (simulated) PDF
            expected_first_name = "Maria"
            expected_last_name = "Silva"
            expected_email = "maria.silva.test@example.org"
            expected_department_name = "Sales" # Agent is expected to assign to this department
            expected_employment_percentage = 100.0 # 'full-time' or similar from PDF
            expected_annual_salary = 600000.0 # Example annual salary in NOK
            expected_standard_working_hours_per_week = 37.5 # Example standard working hours

            print(f"Verifying employee with email: {expected_email}")
            # 1. Find the created employee by email
            r = sandbox_get("/employee", email=expected_email)
            employees = r.json().get("values", [])

            if not employees:
                print(f"Verification failed: Employee with email '{expected_email}' not found.")
                return False
            if len(employees) > 1:
                print(f"Verification warning: Found multiple employees with email '{expected_email}'. Checking the first one.")

            employee = employees[0]

            # 2. Verify basic employee details
            if employee.get("firstName") != expected_first_name:
                print(f"Verification failed: Expected first name '{expected_first_name}', got '{employee.get('firstName')}'")
                return False
            if employee.get("lastName") != expected_last_name:
                print(f"Verification failed: Expected last name '{expected_last_name}', got '{employee.get('lastName')}'")
                return False
            if employee.get("email") != expected_email:
                print(f"Verification failed: Expected email '{expected_email}', got '{employee.get('email')}'")
                return False

            # 3. Verify department assignment
            department = employee.get("department")
            if not department or department.get("name") != expected_department_name:
                print(f"Verification failed: Expected department name '{expected_department_name}', got '{department.get('name') if department else 'None'}'")
                return False

            # 4. Verify employment details: percentage and annual salary
            if employee.get("employmentPercentage") != expected_employment_percentage:
                print(f"Verification failed: Expected employment percentage {expected_employment_percentage}, got {employee.get('employmentPercentage')}")
                return False
            if employee.get("annualSalary") != expected_annual_salary:
                print(f"Verification failed: Expected annual salary {expected_annual_salary}, got {employee.get('annualSalary')}")
                return False

            # 5. Verify standard working hours
            if employee.get("standardWorkingHoursPerWeek") != expected_standard_working_hours_per_week:
                print(f"Verification failed: Expected standard working hours per week {expected_standard_working_hours_per_week}, got {employee.get('standardWorkingHoursPerWeek')}")
                return False

            print("Verification successful: Employee details match expectations.")
            return True
        ,
}
    ])
        if not departments:
            department_data = {"name": "Marketing", "isProjectDepartment": False}
            post_r = sandbox_post("/department", json=department_data)
            if post_r.status_code != 201: # Expect 201 Created for a new resource
                print(f"Failed to create department 'Marketing': {post_r.status_code} - {post_r.text}")
                return False
        return True,
    "verify_fn":
    def verify_fn():
        # Define the expected details that the agent should have extracted/generated from the (missing) PDF contract
        # and used to create the employee.
        expected_first_name = "Ana"
        expected_last_name = "Pereira"
        expected_email = "ana.pereira@example.com"
        expected_national_identity_number = "987654321"
        expected_date_of_birth = "1988-11-20"
        expected_department_name = "Marketing"
        expected_occupation_code = "2432" # Example: Marketing Manager
        expected_monthly_salary = 65000.00
        expected_employment_percentage = 100
        expected_start_date = "2024-08-15"

        # Search for the created employee by email
        r = sandbox_get("/employee", email=expected_email)
        employees = r.json().get("values", [])

        if not employees:
            print(f"Verify failed: Employee with email {expected_email} not found.")
            return False

        employee = employees[0]

        # Verify basic employee details
        if not (employee.get("firstName") == expected_first_name and
                employee.get("lastName") == expected_last_name and
                employee.get("email") == expected_email):
            print(f"Verify failed: Basic employee details mismatch for {expected_email}.")
            print(f"Expected: {expected_first_name} {expected_last_name}, {expected_email}")
            print(f"Found: {employee.get('firstName')} {employee.get('lastName')}, {employee.get('email')}")
            return False

        # Verify details specified in the Portuguese prompt
        if not (employee.get("nationalIdentityNumber") == expected_national_identity_number and
                employee.get("dateOfBirth") == expected_date_of_birth and
                employee.get("occupationCode") == expected_occupation_code and
                employee.get("employmentPercentage") == expected_employment_percentage and
                employee.get("startDate") == expected_start_date):
            print(f"Verify failed: Prompt-specific employee details mismatch for {expected_email}.")
            print(f"Expected - ID: {expected_national_identity_number}, DOB: {expected_date_of_birth}, OccCode: {expected_occupation_code}, EmpPerc: {expected_employment_percentage}, StartDate: {expected_start_date}")
            print(f"Found - ID: {employee.get('nationalIdentityNumber')}, DOB: {employee.get('dateOfBirth')}, OccCode: {employee.get('occupationCode')}, EmpPerc: {employee.get('employmentPercentage')}, StartDate: {employee.get('startDate')}")
            return False

        # Verify salary (assuming monthlySalary in Tripletex API)
        # Use a small tolerance for float comparison, and handle potential None for monthlySalary
        if not (abs((employee.get("monthlySalary") or 0.0) - expected_monthly_salary) < 0.01):
            print(f"Verify failed: Monthly salary mismatch for {expected_email}.")
            print(f"Expected: {expected_monthly_salary}, Found: {employee.get('monthlySalary')}")
            return False

        # Verify department
        department = employee.get("department")
        if not (department and department.get("name") == expected_department_name):
            print(f"Verify failed: Department mismatch for {expected_email}.")
            print(f"Expected: {expected_department_name}, Found: {department.get('name') if department else 'None'}")
            return False

        return True
}
    ]


# ---------------------------------------------------------------------------
# Setup: pre-create invoices needed by test 7 and 10
# ---------------------------------------------------------------------------

def setup(run_all: bool) -> dict:
    """
    Pre-create invoices in the sandbox.
    Returns context dict with invoice numbers/IDs for use in prompts.
    Failures are non-fatal — tests fall back to prompts without specific IDs.
    """
    ctx = {
        "payment_invoice_number": None,
        "credit_note_invoice_id": 2147488820,  # fallback
    }

    print("\n" + "=" * 64)
    print("  PRE-TEST SETUP")
    print("=" * 64)

    try:
        # Test 7 — register_payment: need an open invoice for Nordhav AS
        inv7 = create_test_invoice("Nordhav AS", 14200, "Skylagring")
        if inv7:
            ctx["payment_invoice_number"] = inv7["invoiceNumber"]
            print(f"  [setup] Test 7 will use invoice number={inv7['invoiceNumber']}")
        else:
            print("  [setup] WARNING: Test 7 setup failed — payment test will search by customer name")
    except Exception as e:
        print(f"  [setup] ERROR in test 7 setup: {e}")

    try:
        # Test 10 — create_credit_note: need an open invoice
        inv10 = create_test_invoice("Ironbridge Ltd", 1000, "Credit note test")
        if inv10:
            ctx["credit_note_invoice_id"] = inv10["id"]
            print(f"  [setup] Test 10 will use invoice id={inv10['id']}")
        else:
            print(f"  [setup] WARNING: Test 10 setup failed — using fallback id={ctx['credit_note_invoice_id']}")
    except Exception as e:
        print(f"  [setup] ERROR in test 10 setup: {e}")

    return ctx


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_server() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="NM i AI 2026 — Tripletex agent local tests")
    parser.add_argument(
        "--test", type=int, metavar="N",
        help="Run only test number N (1–15)",
    )
    args = parser.parse_args()

    print()
    print("=" * 64)
    print("  NM i AI 2026 — Tripletex Agent Test Suite")
    print("=" * 64)
    print(f"  Sandbox : {SANDBOX_BASE}  [scorer proxy]")
    print(f"  Server  : {BASE_URL}")

    if not check_server():
        print("\nERROR: Server not reachable at localhost:8000")
        print("       Start with: uvicorn main:app --host 0.0.0.0 --port 8000")
        sys.exit(1)
    print("  Status  : server OK\n")

    run_all = args.test is None
    ctx = setup(run_all)

    # Build full test list with resolved invoice refs
    all_tests = build_tests(
        payment_invoice_number=ctx["payment_invoice_number"],
        credit_note_invoice_id=ctx["credit_note_invoice_id"],
    )

    if args.test is not None:
        selected = [t for t in all_tests if t["number"] == args.test]
        if not selected:
            print(f"ERROR: No test #{args.test}. Valid numbers: 1–{len(all_tests)}")
            sys.exit(1)
        tests_to_run = selected
    else:
        tests_to_run = all_tests

    results = []
    for i, t in enumerate(tests_to_run):
        passed = run_test(t["number"], t["name"], t["prompt"], t.get("before_fn"), t.get("verify_fn"))
        results.append((t["number"], t["name"], passed))
        if i < len(tests_to_run) - 1:
            print(f"\n  (waiting {DELAY_BETWEEN}s...)")
            time.sleep(DELAY_BETWEEN)

    # Summary
    print()
    print("=" * 64)
    print("  SUMMARY")
    print("=" * 64)
    passed_count = sum(1 for _, _, ok in results if ok)
    total = len(results)
    for number, name, ok in results:
        label = "PASS" if ok else "FAIL"
        print(f"  [{label}]  Test {number}: {name}")
    print("-" * 64)
    print(f"  {passed_count}/{total} passed")
    print("=" * 64)
    print()

    if passed_count < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
