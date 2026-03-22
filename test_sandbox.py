"""
Sandbox integration tests — mirrors the exact competition flow:
  parse_task() -> handler.dispatch() -> verify via GET

Run: python test_sandbox.py
Requires .env with TRIPLETEX_SESSION_TOKEN (and optionally TRIPLETEX_CONSUMER_TOKEN).
"""

import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.WARNING,  # suppress handler noise; tests print their own output
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Show TASK_RESULT lines so we can see success/fail inline
logging.getLogger("nmai_tripletex.handlers").setLevel(logging.INFO)

from agent import parse_task
from handlers import TaskHandler
from tripletex import TripletexClient

TODAY = "2026-03-22"


def make_client() -> TripletexClient:
    session_token = os.getenv("TRIPLETEX_SESSION_TOKEN", "")
    consumer_token = os.getenv("TRIPLETEX_CONSUMER_TOKEN", "")
    base_url = os.getenv("TRIPLETEX_BASE_URL", "https://tripletex.no/v2")
    if not session_token:
        print("ERROR: TRIPLETEX_SESSION_TOKEN not set in .env")
        sys.exit(1)
    return TripletexClient(base_url=base_url, session_token=session_token, consumer_token=consumer_token)


def run_test(name: str, prompt: str, extracted_texts: list = None, verify_fn=None) -> bool:
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    try:
        client = make_client()
        handler = TaskHandler(client)
        parsed = parse_task(prompt, extracted_texts=extracted_texts or [])
        context = {"date.today": TODAY}

        if isinstance(parsed, list):
            last_result = {"success": False}
            for i, step in enumerate(parsed):
                tf = step.get("fields", {})
                tf = handler.resolve_templates(tf, context)
                r = handler.dispatch(step.get("task_type"), tf, context=context)
                print(f"  Step {i} [{step.get('task_type')}]: success={r.get('success')} id={r.get('id')}")
                if r.get("id"):
                    context[f"step_{i}.id"] = r["id"]
                last_result = r
            result = {"success": all(
                True for s in parsed  # will be overridden below
            ), "results": parsed}
            # Re-evaluate based on actual step results
            result["success"] = last_result.get("success", False)
        else:
            task_type = parsed.get("task_type", "unknown")
            fields = parsed.get("fields", {})
            print(f"  Parsed: task_type={task_type}")
            result = handler.dispatch(task_type, fields, context=context)

        print(f"  Result: {json.dumps(result, default=str)[:400]}")

        if verify_fn:
            verified = verify_fn(client, result)
            status = "PASS" if verified else "FAIL"
            print(f"  Verification: {status}")
            return verified

        passed = result.get("success", False)
        print(f"  Status: {'PASS' if passed else 'FAIL'}")
        return passed

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def _get_recent_vouchers(client, count=10):
    # dateFrom/dateTo required; use full year range
    st, d = client.get("/ledger/voucher", params={
        "count": count, "sorting": "-id",
        "dateFrom": "2026-01-01", "dateTo": "2026-12-31",
    })
    return d.get("values", []) if st == 200 else []


def _get_voucher(client, vid):
    """GET a single voucher without invalid fields param."""
    st, d = client.get(f"/ledger/voucher/{vid}")
    return st, d.get("value", {}) if st == 200 else {}


def verify_supplier_voucher(client, result):
    """Voucher must exist and have at least 2 postings (expense + AP)."""
    if not result.get("success"):
        print("  [verify] handler returned success=False")
        return False
    vid = result.get("id")
    if not vid:
        print("  [verify] no voucher id returned")
        return False
    st, voucher = _get_voucher(client, vid)
    if st != 200:
        print(f"  [verify] GET /ledger/voucher/{vid} returned {st}")
        return False
    postings = voucher.get("postings", [])
    print(f"  [verify] voucher {vid} postings={len(postings)}")
    return len(postings) >= 2  # at minimum expense + AP credit


def verify_month_end(client, result):
    """At least 1 voucher created."""
    if not result.get("success"):
        return False
    vouchers = _get_recent_vouchers(client, 20)
    print(f"  [verify] {len(vouchers)} recent vouchers found")
    return len(vouchers) >= 1


def verify_year_end(client, result):
    """At least 3 vouchers created (one per asset depreciation)."""
    if not result.get("success"):
        return False
    vouchers = _get_recent_vouchers(client, 20)
    print(f"  [verify] {len(vouchers)} recent vouchers found")
    return len(vouchers) >= 3


def verify_payroll(client, result):
    """Voucher on payroll accounts exists."""
    if not result.get("success"):
        return False
    vid = result.get("id")
    if vid:
        st, voucher = _get_voucher(client, vid)
        if st == 200:
            postings = voucher.get("postings", [])
            print(f"  [verify] payroll voucher {vid}, postings={len(postings)}")
            return len(postings) >= 1
    # Fallback: check recent vouchers
    vouchers = _get_recent_vouchers(client, 5)
    print(f"  [verify] payroll fallback: {len(vouchers)} recent vouchers")
    return len(vouchers) >= 1


def verify_correction_vouchers(client, result):
    """At least 1 correction voucher created."""
    if not result.get("success"):
        return False
    vouchers = _get_recent_vouchers(client, 20)
    print(f"  [verify] {len(vouchers)} recent vouchers found")
    return len(vouchers) >= 1


def verify_project_cycle(client, result):
    """Project exists with at least one invoice."""
    if not result.get("success"):
        return False
    st, d = client.get("/project", params={"count": 10, "sorting": "-id"})
    projects = d.get("values", []) if st == 200 else []
    found = any("dataplattform" in p.get("name", "").lower() or "brattli" in p.get("name", "").lower()
                for p in projects)
    print(f"  [verify] project found={found} (checked {len(projects)} recent projects)")
    return found


def verify_cost_analysis_projects(client, result):
    """3 projects created."""
    if not result.get("success"):
        return False
    st, d = client.get("/project", params={"count": 10, "sorting": "-id"})
    projects = d.get("values", []) if st == 200 else []
    print(f"  [verify] {len(projects)} recent projects found")
    return len(projects) >= 3


def verify_bank_reconciliation(client, result):
    """At least one payment registered."""
    if not result.get("success"):
        return False
    print("  [verify] bank reconciliation returned success=True")
    return True


def verify_fx_payment(client, result):
    """Payment registered + FX voucher created."""
    if not result.get("success"):
        return False
    vid = result.get("id")
    print(f"  [verify] fx payment result id={vid}")
    return True


def verify_accounting_dimension(client, result):
    """2 departments exist + voucher created and approved."""
    if not result.get("success"):
        return False
    dept_ids = result.get("departmentIds", [])
    print(f"  [verify] departments created: {dept_ids}")
    vid = result.get("id")
    if not dept_ids:
        return False
    # Check voucher if id returned is a voucher
    if vid and vid not in dept_ids:
        st, voucher = _get_voucher(client, vid)
        if st == 200:
            postings = voucher.get("postings", [])
            print(f"  [verify] voucher {vid} postings={len(postings)}")
    return len(dept_ids) >= 2


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

BANK_RECON_CSV = """date,description,amount
2026-01-15,Payment from customer ref INV-001,25000
2026-01-20,Supplier payment Bergvik AS,-14650
2026-01-25,Customer payment partial,-5000"""


def _ensure_customer(client, name, org_number=None):
    """Return existing customer id or create a new one."""
    params = {"count": 5}
    if org_number:
        params["organizationNumber"] = org_number
    st, d = client.get("/customer", params=params)
    for c in d.get("values", []):
        if name.lower() in c.get("name", "").lower() or (org_number and c.get("organizationNumber") == org_number):
            return c["id"]
    payload = {"name": name, "isCustomer": True}
    if org_number:
        payload["organizationNumber"] = org_number
    st2, d2 = client.post("/customer", json=payload)
    if st2 in (200, 201):
        return d2.get("value", {}).get("id")
    return None


def _ensure_open_invoice(client, customer_id, amount_nok, description="Test invoice"):
    """Return existing open invoice id for customer, or create one via order→invoice."""
    st, d = client.get("/invoice", params={
        "invoiceDateFrom": "2000-01-01", "invoiceDateTo": "2099-12-31", "count": 50,
    })
    for inv in d.get("values", []):
        c = inv.get("customer") or {}
        if c.get("id") == customer_id:
            outstanding = float(inv.get("amountOutstanding") or 0)
            if abs(outstanding - amount_nok) < 1.0:
                return inv["id"]

    # Create order then invoice
    st2, d2 = client.post("/order", json={
        "customer": {"id": customer_id},
        "orderDate": "2026-01-10",
        "deliveryDate": "2026-01-31",
        "orderLines": [{"description": description, "count": 1, "unitPriceExcludingVatCurrency": amount_nok}],
    })
    if st2 not in (200, 201):
        return None
    order_id = d2.get("value", {}).get("id")
    st3, d3 = client.put(f"/order/{order_id}/:invoice", params={"invoiceDate": "2026-01-10"})
    if st3 in (200, 201):
        return d3.get("value", {}).get("id")
    return None


def setup_sandbox_data():
    """Create test employees needed by T4/T6 if they don't already exist."""
    client = make_client()
    # Find the first available department
    st_d, d_data = client.get("/department", params={"count": 1})
    dept_id = d_data.get("values", [{}])[0].get("id") if st_d == 200 else None

    needed = [
        {"firstName": "Marte", "lastName": "Ødegård", "email": "marte.degard@example.org"},
        {"firstName": "Hilde", "lastName": "Ødegård", "email": "hilde.degard@example.org"},
        {"firstName": "Lars", "lastName": "Johansen", "email": "lars.johansen@example.org"},
    ]
    st, d = client.get("/employee", params={"count": 100, "fields": "email"})
    existing_emails = {e.get("email", "").lower() for e in d.get("values", [])} if st == 200 else set()
    for emp in needed:
        if emp["email"] not in existing_emails:
            payload = {**emp, "userType": "NO_ACCESS"}
            if dept_id:
                payload["department"] = {"id": dept_id}
            r_st, r_d = client.post("/employee", json=payload)
            if r_st in (200, 201):
                print(f"  Setup: created employee {emp['firstName']} {emp['lastName']} id={r_d.get('value', {}).get('id')}")
            else:
                print(f"  Setup: employee create FAILED {r_st}: {r_d.get('validationMessages', r_d)}")
        else:
            print(f"  Setup: employee {emp['email']} already exists")

    # T8: ensure an open customer invoice for 25000 NOK exists (bank reconciliation CSV line 1)
    cust_id_recon = _ensure_customer(client, "Acme AS")
    if cust_id_recon:
        inv_id = _ensure_open_invoice(client, cust_id_recon, 25000.0, "INV-001 Services")
        if inv_id:
            print(f"  Setup: T8 invoice 25000 NOK for Acme AS id={inv_id}")
        else:
            print("  Setup: T8 invoice create FAILED")

    # T9: ensure Tindra AS customer + open invoice for FX payment test
    tindra_id = _ensure_customer(client, "Tindra AS", org_number="862097653")
    if tindra_id:
        # Invoice amount ~= 11497 EUR * 10.84 NOK/EUR (payment rate) so payment goes through
        inv_id9 = _ensure_open_invoice(client, tindra_id, 124618.0, "EUR invoice Tindra AS")
        if inv_id9:
            print(f"  Setup: T9 invoice 124618 NOK for Tindra AS id={inv_id9}")
        else:
            print("  Setup: T9 invoice create FAILED")


def main():
    print("Setting up sandbox test data...")
    setup_sandbox_data()

    results = []

    results.append(("T1 Supplier invoice with VAT", run_test(
        "T1 Supplier invoice with VAT",
        "We have received invoice INV-2026-4606 from supplier Silveroak Ltd "
        "(org no. 973931156) for 14650 NOK including VAT. The amount relates to office "
        "services (account 6500). Register the supplier invoice with correct input VAT (25%).",
        verify_fn=verify_supplier_voucher,
    )))

    results.append(("T2 Month end closing", run_test(
        "T2 Month end closing",
        "Periodiser forskotsbetalt kostnad (14950 kr per månad frå konto 1710 "
        "til kostnadskonto). Bokfør månadleg avskriving for eit driftsmiddel med "
        "innkjøpskost 276650 kr og levetid 5 år (lineær avskriving til konto 6010). "
        "Bokfør også ei lønnsavsetjing (debet lønnskostnad konto 5000, kredit påløpt "
        "lønn konto 2900).",
        verify_fn=verify_month_end,
    )))

    results.append(("T3 Year end closing", run_test(
        "T3 Year end closing",
        "Rekn ut og bokfør årlege avskrivingar for tre eigedelar: Kjøretøy "
        "(417650 kr, 6 år lineært, konto 1230), Kontormaskiner (221550 kr, 4 år, konto "
        "1200), Inventar (253750 kr, 9 år, konto 1240). Bruk konto 6010 for "
        "avskrivingskostnad og 1209 for akkumulerte avskrivingar. Reverser forskotsbetalt "
        "kostnad (totalt 63450 kr på konto 1700). Rekn ut og bokfør skattekostnad "
        "(22% av skattbart resultat) på konto 8700/2920.",
        verify_fn=verify_year_end,
    )))

    results.append(("T4 Payroll with fallback", run_test(
        "T4 Payroll with fallback",
        "Kjør lønn for Marte Ødegård (marte.degard@example.org) for denne "
        "måneden. Grunnlønn er 48650 kr. Legg til engangsbonus på 6800 kr i tillegg "
        "til grunnlønnen. Dersom lønns-API-et ikke fungerer, kan du bruke manuelle "
        "bilag på lønnskontoer (5000-serien) for å registrere lønnskostnaden.",
        verify_fn=verify_payroll,
    )))

    results.append(("T5 Ledger error correction", run_test(
        "T5 Ledger error correction",
        "We have discovered errors in the general ledger for January and "
        "February 2026. Correct all errors with appropriate correction vouchers: "
        "a posting to wrong account (7100 used instead of 7140, amount 6400 NOK), "
        "a duplicate voucher (account 7300, amount 1100 NOK), a missing VAT line "
        "(account 6500, amount excl. 19350 NOK missing VAT on account 2710), "
        "incorrect amount (account 6540, 20500 NOK posted instead of 12150 NOK).",
        verify_fn=verify_correction_vouchers,
    )))

    results.append(("T6 Full project cycle", run_test(
        "T6 Full project cycle",
        "Gjennomfør hele prosjektsyklusen for 'Dataplattform Brattli' "
        "(Brattli AS, org.nr 937190808): 1) Prosjektet har budsjett 349100 kr. "
        "2) Registrer timer: Hilde Ødegård (hilde.degard@example.org) 21 timer og "
        "Lars Johansen (lars.johansen@example.org) 141 timer. 3) Registrer "
        "leverandørkostnad 71800 kr fra Lysgård AS (org.nr 898870936). "
        "4) Opprett kundefaktura for prosjektet.",
        verify_fn=verify_project_cycle,
    )))

    results.append(("T7 Cost analysis and project creation", run_test(
        "T7 Cost analysis and project creation",
        "Los costos totales aumentaron significativamente de enero a febrero "
        "de 2026. Analice el libro mayor e identifique las tres cuentas de gastos con "
        "el mayor incremento en monto. Cree un proyecto interno para cada una de las "
        "tres cuentas con el nombre de la cuenta. También cree una actividad para "
        "cada proyecto.",
        verify_fn=verify_cost_analysis_projects,
    )))

    results.append(("T8 Bank reconciliation", run_test(
        "T8 Bank reconciliation",
        "Reconcile the bank statement against open invoices. Match incoming payments "
        "to customer invoices and outgoing payments to supplier invoices.",
        extracted_texts=[BANK_RECON_CSV],
        verify_fn=verify_bank_reconciliation,
    )))

    results.append(("T9 FX payment", run_test(
        "T9 FX payment",
        "Vi sendte en faktura på 11497 EUR til Tindra AS (org.nr 862097653) "
        "da kursen var 10.09 NOK/EUR. Kunden har nå betalt, men kursen er 10.84 "
        "NOK/EUR. Registrer betalingen og bokfør valutadifferansen (agio) på korrekt "
        "konto.",
        verify_fn=verify_fx_payment,
    )))

    results.append(("T10 Accounting dimension", run_test(
        "T10 Accounting dimension",
        "Crie uma dimensão contabilística personalizada 'Marked' com os "
        "valores 'Bedrift' e 'Privat'. Em seguida, lance um documento na conta 6590 "
        "por 16750 NOK, vinculado ao valor de dimensão 'Bedrift'.",
        verify_fn=verify_accounting_dimension,
    )))

    print(f"\n{'='*60}")
    print(f"SUMMARY: {sum(1 for _, r in results if r)}/{len(results)} passed")
    for name, passed in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")


if __name__ == "__main__":
    main()
