import json
import logging
import os
import re
import time

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

SYSTEM_PROMPT = """You are an accounting task parser for Tripletex (Norwegian accounting software).
Extract the task type and all relevant fields from the prompt.
The prompt may be in any of these languages: Norwegian (bokmål), Norwegian (nynorsk), English, Spanish, Portuguese, German, French.

Return ONLY valid JSON with this structure:
{
  "reasoning": "Brief explanation of why this task type was chosen and how fields were mapped",
  "task_type": "<one of the task types below>",
  "fields": {
    // all extracted field values
  }
}
OR if the prompt contains MULTIPLE distinct actions (e.g. "Create order AND convert to invoice"), return a JSON array of such objects.
If you return an array, you may use "{{step_N.id}}" (0-indexed) in the 'fields' of a later step to refer to the ID returned by step N.

FEW-SHOT MAPPING EXAMPLES:
1. "Create supplier Acme, org 123456789" -> {"reasoning": "User explicitly asked for a supplier.", "task_type": "create_supplier", "fields": {"name": "Acme", "organizationNumber": "123456789"}}
2. "Log 5 hours for John on project P" -> {"reasoning": "User asked to log hours without mention of invoicing.", "task_type": "register_hours", "fields": {"hours": 5, "employeeName": "John", "projectName": "P"}}
3. "Log 13 hours for Hannah Brown (hannah.brown@example.org) and invoice" -> {"reasoning": "Log hours with email and request invoice.", "task_type": "register_hours_and_invoice", "fields": {"hours": 13, "employeeName": "Hannah Brown", "employeeEmail": "hannah.brown@example.org", "invoiceRequired": true}}
4. "Registrer full betaling på faktura 98" -> {"reasoning": "Simple payment registration for an existing invoice.", "task_type": "register_payment", "fields": {"invoiceNumber": "98"}}
5. "Create order for X and convert to invoice" -> [
     {"reasoning": "Step 1: Create the order.", "task_type": "create_order", "fields": {"customerName": "X"}},
     {"reasoning": "Step 2: Convert the created order into an invoice.", "task_type": "create_invoice", "fields": {"orderId": "{{step_0.id}}", "customerName": "X"}}
   ]
6. "Sett fastpris 274950 kr på prosjektet Nettbutikk-utvikling for Skogheim AS (org.nr 826912324). Prosjektleiar er Bjørn Kvamme (bjrn.kvamme@example.org). Fakturer kunden for 50% av fastprisen." -> {"reasoning": "User wants to create a project with a fixed price and invoice a percentage.", "task_type": "create_project_invoice", "fields": {"projectName": "Nettbutikk-utvikling", "customerName": "Skogheim AS", "organizationNumber": "826912324", "fixedPrice": 274950.0, "invoicePercent": 50.0, "projectManagerName": "Bjørn Kvamme", "projectManagerEmail": "bjrn.kvamme@example.org"}}
7. "Run payroll for Lucy Walker for this month. Base salary 43050 NOK, one-time bonus 6100 NOK." -> {"reasoning": "User wants to run payroll with a base salary and a one-time bonus.", "task_type": "run_payroll", "fields": {"employeeName": "Lucy Walker", "baseSalary": 43050.0, "bonus": 6100.0}}
8. "Kjør lønn for Ola Nordmann, fastlønn 38000 kr, bonus 2500 kr." -> {"reasoning": "Norwegian payroll request with base salary and bonus.", "task_type": "run_payroll", "fields": {"employeeName": "Ola Nordmann", "baseSalary": 38000.0, "bonus": 2500.0}}
9. "Post month-end accruals: debit 5800 for 12000, credit 2910 for 12000." -> {"reasoning": "Month-end closing journal entry with specific account postings.", "task_type": "month_end_closing", "fields": {"description": "Month-end accrual", "date": "2026-03-31", "postings": [{"accountNumber": 5800, "amount": 12000.0}, {"accountNumber": 2910, "amount": -12000.0}]}}
10. "Year-end closing 2025: depreciate equipment (account 1200) by 50000, reverse prepaid insurance (1710) 8000, book tax at 22%." -> {"reasoning": "Year-end closing with depreciation, prepaid reversal and tax posting.", "task_type": "year_end_closing", "fields": {"year": 2025, "depreciations": [{"accountNumber": 1200, "amount": 50000.0, "description": "Equipment depreciation"}], "prepaidExpenseAccount": 1710, "prepaidExpenseAmount": 8000.0, "taxRate": 0.22}}

CRITICAL ROUTING RULE: If prompt mentions hours/timer/horas/Stunden/heures AND invoice/faktura/fatura/factura/Rechnung in the SAME prompt — ALWAYS use register_hours_and_invoice, never create_invoice alone.

Task types and their fields:
- create_employee: firstName, lastName, email, phone (optional), employeeNumber (optional), roles (list, e.g. ["ROLE_ADMINISTRATOR"]), dateOfBirth (optional, YYYY-MM-DD), startDate (optional, YYYY-MM-DD), department (optional, string), occupationCode (optional, string), salary (optional, number), employmentPercentage (optional, number), nationalIdentityNumber (optional, string), pdf_text (optional)
- create_customer: name, email (optional), phone (optional), organizationNumber (optional), addressLine1 (optional, street and number), postalCode (optional, 4-digit Norwegian zip), city (optional), country (optional, default Norway), isCustomer (always true)
- create_product: name, price, vatType (optional), unit (optional), productNumber (optional)
- create_invoice: customerName or customerId, orderLines (list of {description, quantity, unitPrice}), invoiceDate (YYYY-MM-DD), dueDate (YYYY-MM-DD)
- register_payment: invoiceId (optional), invoiceNumber (optional), customerName (optional), amount, paymentDate (YYYY-MM-DD)
- create_credit_note: invoiceId or invoiceNumber
- create_project: name, customerId or customerName, startDate (YYYY-MM-DD), endDate (optional)
- create_department: name, departmentNumber (optional)
- create_travel_expense: employeeId or employeeName, description, date (YYYY-MM-DD), costs (list of {description, amount}. IMPORTANT: If a daily allowance/per-diem/Tagegeld is mentioned, calculate the total allowance amount (days * rate) and include it as a cost in this list!)
- delete_travel_expense: employeeId or employeeName or description
- create_order: customerName or customerId, orderLines (list of {description, quantity, unitPrice}), orderDate (YYYY-MM-DD)
- update_customer: customerName, fields to update
- update_employee: employeeName, fields to update
- delete_employee: employeeName or employeeNumber
- enable_department_accounting: (no extra fields needed)
- create_contact_person: customerName, firstName, lastName, email (optional), phone (optional)
- update_product: name, price (optional), newName (optional), description (optional)
- create_supplier: name, email (optional), phone (optional), organizationNumber (optional)
- delete_customer: customerName
- delete_supplier: name (supplier name or organizationNumber)
- delete_order: orderId or customerName (order to find and delete)
- delete_product: name or productNumber (product to find and delete)
- get_employee: employeeId or employeeName or employeeNumber
- get_customer: customerId or customerName or organizationNumber
- get_invoice: invoiceId or invoiceNumber
- update_invoice: invoiceId or invoiceNumber, fields to update
- register_hours: employeeName or employeeId, projectName or projectId, date (YYYY-MM-DD), hours, comment (optional)
- create_voucher: date (YYYY-MM-DD), description, supplierName (optional), invoiceNumber (optional), postings (list of {accountId, amount, description, date})
- reverse_payment: invoiceId or invoiceNumber
- create_credit_note: invoiceId or invoiceNumber
- update_project: projectName or projectId, and fields to update
- close_project: projectName or projectId
- bank_reconciliation: (no extra fields — data from attached CSV)
- update_contact_person: contactId or (firstName + lastName + customerName)
- create_asset: name (required), description (optional), acquisitionCost (numeric), acquisitionDate (YYYY-MM-DD)
- delete_asset: assetId or name
- import_bank_statement: fromDate (YYYY-MM-DD), toDate (YYYY-MM-DD), fileFormat ("CSV" or "CAMT")
- initiate_year_end_closing: year (integer)
- create_payroll_tax_reconciliation: year (integer), term (1-6)
- upload_document: customerId/Name, employeeId/Name, projectId, supplierId, or productId
- register_hours_and_invoice: hours (number), employeeName/Email, employeeEmail (optional), activityName, projectName, customerName, hourlyRate, invoiceRequired (true), date (YYYY-MM-DD)
- create_project_invoice: projectName, customerName, organizationNumber (optional), fixedPrice, invoicePercent (default 100), invoiceAmount, startDate, endDate, invoiceDate, dueDate, projectManagerName (optional), projectManagerEmail (optional)
- run_payroll: employeeName, employeeEmail (optional), baseSalary (number), bonus (optional number, one-time bonus)
- month_end_closing: description, date (YYYY-MM-DD), postings (list of {accountNumber, amount, description})
- year_end_closing: year (int), depreciations (list of {accountNumber, amount, description}), prepaidExpenseAccount (optional, account number), prepaidExpenseAmount (optional, number), taxRate (default 0.22)
- unknown: (fallback)

For dates, use today's date (2026-03-20) if not specified.
STRICT TYPES: All ID fields MUST be integers. All amount fields MUST be floats. NEVER use strings for IDs or objects for amounts.
"""


_GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
if not _GOOGLE_API_KEY:
    raise RuntimeError("GOOGLE_API_KEY is not set — cannot start without Gemini credentials")

_client = genai.Client(api_key=_GOOGLE_API_KEY)
_generate_config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    temperature=0.0,
    response_mime_type="application/json"
)
logger.info("Agent: gemini-2.5-flash with Structured Output and T=0")


def _extract_retry_delay(exc: Exception) -> float:
    """Extract retry-after seconds from a rate limit error, defaulting to 60."""
    msg = str(exc)
    match = re.search(r'retry.?after["\s:]+(\d+(?:\.\d+)?)', msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r'(\d+(?:\.\d+)?)\s*second', msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return 60.0


def _call_model(user_content: str, image_parts: list = None) -> str:
    """Call Gemini and return raw response text. Supports vision if image_parts provided."""
    if image_parts:
        parts = []
        for img in image_parts:
            parts.append(types.Part.from_bytes(data=img["data"], mime_type=img["mime_type"]))
        parts.append(types.Part.from_text(text=user_content))
        response = _client.models.generate_content(
            model="gemini-2.5-flash",
            contents=parts,
            config=_generate_config,
        )
    else:
        response = _client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_content,
            config=_generate_config,
        )
    return response.text.strip()


def test_model() -> bool:
    """Make a quick sanity-check call to Gemini. Called at startup."""
    try:
        result = _call_model("Reply with exactly the word: OK")
        logger.info("Model startup test passed — response: %r", result[:80])
        return True
    except Exception as e:
        logger.error("Model startup test FAILED: %s", e)
        return False


def parse_task(prompt: str, extracted_texts: list = None, image_parts: list = None) -> dict:
    """Call the LLM to parse the accounting task prompt. Retries on 429. Returns parsed dict."""
    user_content = prompt
    if extracted_texts:
        user_content += "\n\nAttached file contents:\n" + "\n\n".join(extracted_texts)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = _call_model(user_content, image_parts=image_parts)

            # Strip markdown code fences if present (e.g. ```json\n...\n```)
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            raw = raw.strip()

            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for i, step in enumerate(parsed):
                    logger.info("Step %d Reasoning: %s", i, step.get("reasoning"))
                logger.info("Parsed multi-task list with %d steps", len(parsed))
                return parsed
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected JSON object or list, got {type(parsed).__name__}: {raw[:120]}")
            
            logger.info("Reasoning: %s", parsed.get("reasoning"))
            logger.info("Parsed task_type=%s (attempt %d)", parsed.get("task_type"), attempt)
            return parsed

        except Exception as e:
            err = str(e)
            is_rate_limit = "429" in err or "rate" in err.lower() or "quota" in err.lower()
            if is_rate_limit and attempt < MAX_RETRIES:
                delay = _extract_retry_delay(e)
                logger.warning(
                    "Gemini 429 rate limit on attempt %d/%d — waiting %.0fs: %s",
                    attempt, MAX_RETRIES, delay, e,
                )
                time.sleep(delay)
            elif attempt < MAX_RETRIES:
                # Format/transient error — retry immediately without sleeping
                logger.warning("parse_task format error on attempt %d/%d, retrying: %s", attempt, MAX_RETRIES, e)
            else:
                logger.error("parse_task failed after %d attempts: %s", MAX_RETRIES, e)
                return {"task_type": "unknown", "fields": {}}
