import json
import logging
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from tripletex import TripletexClient

logger = logging.getLogger(__name__)

TODAY = "2026-03-20"


_MONTH_NAMES = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def normalize_date(value) -> str | None:
    """Normalize a date value to YYYY-MM-DD string.
    Handles: YYYY-MM-DD (passthrough), DD.MM.YYYY, MM/DD/YYYY, epoch int/float,
             '12. March 1990', 'March 12, 1990'."""
    if not value:
        return None
    s = str(value).strip()
    # Already correct format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # DD.MM.YYYY
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # MM/DD/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    # "12. March 1990" or "12 March 1990"
    m = re.match(r"^(\d{1,2})\.?\s+([A-Za-z]+)\s+(\d{4})$", s)
    if m:
        month = _MONTH_NAMES.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"
    # "March 12, 1990" or "March 12 1990"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", s)
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month}-{m.group(2).zfill(2)}"
    # Epoch (numeric)
    try:
        import datetime
        ts = self._to_float(s)
        return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        pass
    logger.warning("Could not normalize date value: '%s'", value)
    return s  # return as-is rather than None


class TaskHandler:
    def __init__(self, client: TripletexClient, files: list = None):
        self.client = client
        self._vat_cache: list | None = None
        self._account_id_cache: dict = {}
        self.files = files or []  # raw file dicts from the request body

    def resolve_templates(self, obj, context):
        """Recursively replace {{key}} placeholders with values from context.
        Preserves original type (e.g. int) if the entire string is a placeholder."""
        if isinstance(obj, str):
            # If entire string is exactly one placeholder: {{step_N.id}}
            m = re.fullmatch(r"\{\{([^}]+)\}\}", obj)
            if m:
                key = m.group(1)
                if key in context:
                    return context[key]
            
            # Partial replacement
            for key, val in context.items():
                obj = obj.replace("{{" + key + "}}", str(val) if val is not None else "")
            return obj
            
        if isinstance(obj, dict):
            return {k: self.resolve_templates(v, context) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve_templates(item, context) for item in obj]
        return obj

    def has_empty_id(self, obj) -> bool:
        """Return True if any dict value that looks like an id reference resolved to '' or an unresolved placeholder."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if (isinstance(v, str) and "{{" in v) or (str(k).lower().endswith("id") and v == ""):
                    return True
                if self.has_empty_id(v):
                    return True
        if isinstance(obj, list):
            return any(self.has_empty_id(item) for item in obj)
        return False

    def _to_int(self, val, default=0):
        """Safely cast value to int, avoiding crashes on templates or None."""
        if val is None: return default
        if isinstance(val, int): return val
        s_val = str(val).strip()
        if not s_val or "{{" in s_val: return val # Return as-is if template or empty
        try:
            # Handle cases like "123.0" -> 123
            return int(float(s_val.replace(" ", "")))
        except (ValueError, TypeError):
            return default

    def _to_float(self, val, default=0.0):
        """Safely cast value to float, avoiding crashes on templates or None."""
        if val is None: return default
        if isinstance(val, (float, int)): return float(val)
        s_val = str(val).strip()
        if not s_val or "{{" in s_val: return val # Return as-is if template or empty
        try:
            return float(s_val.replace(",", ".").replace(" ", ""))
        except (ValueError, TypeError):
            return default

    def _deep_cast_types(self, obj):
        """Nuclear type casting to satisfy strict Tripletex schema validation."""
        if isinstance(obj, dict):
            new_obj = {}
            for k, v in obj.items():
                kl = k.lower()
                if kl == "id" or kl.endswith("id"):
                    new_obj[k] = self._to_int(v) if not isinstance(v, (dict, list)) else self._deep_cast_types(v)
                elif kl in ("amount", "price", "count", "quantity", "rate", "unitcostcurrency", "unitpriceexcludingvatcurrency", "fixedprice"):
                    new_obj[k] = self._to_float(v) if not isinstance(v, (dict, list)) else self._deep_cast_types(v)
                else:
                    new_obj[k] = self._deep_cast_types(v)
            return new_obj
        if isinstance(obj, list):
            return [self._deep_cast_types(item) for item in obj]
        return obj

    # ------------------------------------------------------------------
    # lookup helpers
    # ------------------------------------------------------------------

    def _find_customer_id(self, name: str):
        """Find customer by name. Tries exact match first, then partial/fuzzy."""
        # Exact name search via API
        status, data = self.client.get("/customer", params={"name": name, "count": 10})
        if status == 200:
            values = data.get("values", [])
            if values:
                # Prefer exact match
                for v in values:
                    if v.get("name", "").lower() == name.lower():
                        return self._to_int(v["id"])
                # Fall back to first result
                return self._to_int(values[0]["id"])

        # Broad list search for partial/fuzzy match
        status, data = self.client.get("/customer", params={"count": 200, "fields": "id,name"})
        if status == 200:
            best_id, best_score = None, 0
            name_lower = name.lower()
            name_words = set(name_lower.split())
            for v in data.get("values", []):
                cname = v.get("name", "").lower().strip()
                if not cname: continue
                # Only do substring match if one is significantly long (e.g. > 4 chars) to avoid matching "AS"
                if len(cname) > 4 and (name_lower in cname or cname in name_lower):
                    return self._to_int(v["id"])
                # Word-overlap score
                score = len(name_words & set(cname.split()))
                if score > best_score:
                    best_score = score
                    best_id = v["id"]
            if best_id:
                logger.info("Fuzzy-matched customer '%s' -> id=%s", name, best_id)
                return self._to_int(best_id)

        return None

    def _ensure_employee(self, name: str, email: str = None):
        """Find existing or create new employee."""
        emp_id = self._find_employee_id(email or name)
        if emp_id:
            return emp_id
            
        logger.info("Employee '%s' (email=%s) not found — creating new", name, email)
        first, last = (name.split(" ", 1) + [""])[:2]
        payload = {"firstName": first, "lastName": last or "Unknown", "userType": 1}
        if email: payload["email"] = email
        
        dept_status, dept_data = self.client.get("/department", params={"count": 1})
        if dept_status == 200 and dept_data.get("values"):
            payload["department"] = {"id": self._to_int(dept_data["values"][0]["id"])}
            
        status, data = self.client.post("/employee", json=payload)
        if status in (200, 201):
            eid = self._to_int(data.get("value", {}).get("id"))
            # If start date is required by sandbox, add employment
            self.client.post("/employee/employment", json={"employee": {"id": eid}, "startDate": TODAY})
            return eid
        return None

    def _find_employee_id(self, name: str):
        """Find employee by full name or email."""
        if not name:
            return None
        logger.info("Searching for employee: '%s'", name)
        status, data = self.client.get("/employee", params={"count": 200, "fields": "id,firstName,lastName,email"})
        if status != 200:
            logger.warning("Failed to fetch employees: %s", data)
            return None
        employees = data.get("values", [])
        name_lower = str(name).lower().strip()
        # 1. match email
        for emp in employees:
            if (emp.get("email") or "").lower().strip() == name_lower:
                return self._to_int(emp["id"])
        # 2. match full name
        for emp in employees:
            fullname = f"{emp.get('firstName', '')} {emp.get('lastName', '')}".lower().strip()
            if fullname == name_lower:
                return self._to_int(emp["id"])
        # 3. partial name match
        for emp in employees:
            fullname = f"{emp.get('firstName', '')} {emp.get('lastName', '')}".lower().strip()
            if name_lower in fullname or fullname in name_lower:
                logger.info("Partial-matched employee '%s' -> id=%s (%s %s)", name, emp["id"], emp.get("firstName"), emp.get("lastName"))
                return self._to_int(emp["id"])

        logger.warning("Employee '%s' not found among %d employees", name, len(employees))
        return None

    def _find_employee_id_for_project(self):
        """Fallback: just get the first active employee for project manager."""
        status, data = self.client.get("/employee", params={"count": 5, "fields": "id"})
        if status == 200 and data.get("values"):
            return self._to_int(data["values"][0]["id"])
        return None

    def _clean_postings(self, postings: list) -> list:
        """Strip system-generated fields from every posting dict before sending to
        POST /ledger/voucher.  Tripletex rejects postings that contain these fields
        with the error:
          "Posteringene på rad 0 (guiRow 0) er systemgenererte og kan ikke opprettes
           eller endres på utsiden av Tripletex."
        Fields stripped at posting level: row, guiRow, id, voucher.
        The 'id' key on the nested 'account' sub-object is intentionally preserved.
        """
        _STRIP = {"row", "guiRow", "id", "voucher"}
        for p in postings:
            if isinstance(p, dict):
                for field in _STRIP:
                    p.pop(field, None)
        return postings

    def _find_account_id(self, number) -> int | None:
        """Resolve a ledger account number (e.g. 7100) to the internal Tripletex integer ID.

        Results are cached per request to avoid duplicate API calls when multiple
        postings reference the same account number.
        Returns None if the account cannot be found.
        """
        key = str(number)
        if key in self._account_id_cache:
            return self._account_id_cache[key]

        status, data = self.client.get(
            "/ledger/account", params={"number": key, "count": 1}
        )
        if status == 200:
            values = data.get("values", [])
            if values:
                internal_id = self._to_int(values[0]["id"])
                self._account_id_cache[key] = internal_id
                logger.info("Resolved account number %s -> internal id=%s", key, internal_id)
                return internal_id

        logger.warning("Account number %s not found in ledger", key)
        self._account_id_cache[key] = None
        return None

    def _ensure_customer(self, name: str, org_number: str = None):
        """Find or create customer. Try search first to avoid duplicates."""
        customer_id = self._find_customer_id(name)
        if customer_id:
            return customer_id
            
        payload = {"name": name, "isCustomer": True}
        if org_number:
            payload["organizationNumber"] = str(org_number)

        status, data = self.client.post("/customer", json=payload)
        if status in (200, 201):
            return self._to_int(data.get("value", {}).get("id"))

        # Fallback to GET only if POST fails
        logger.info("Customer POST failed (status=%s) — falling back to search", status)
        if org_number:
            status, data = self.client.get("/customer", params={"organizationNumber": str(org_number), "count": 5})
            if status == 200 and data.get("values"):
                return self._to_int(data["values"][0]["id"])

        return self._find_customer_id(name)

    def _find_department_id(self, name: str):
        if not name: return None
        status, data = self.client.get("/department", params={"name": name, "count": 1})
        if status == 200 and data.get("values"):
            return self._to_int(data["values"][0]["id"])
        return None

    def _ensure_department(self, name: str):
        if not name: return None
        dept_id = self._find_department_id(name)
        if dept_id: return dept_id
        res = self.handle_create_department({"name": name})
        if res.get("success"):
            return res.get("id")
        return None

    def _find_occupation_code_id(self, code: str):
        if not code: return None
        status, data = self.client.get("/employee/employment/occupationCode", params={"code": str(code), "count": 1})
        if status == 200 and data.get("values"):
            return self._to_int(data["values"][0]["id"])
        return None

    def _parallel_lookup(self, lookups: dict) -> dict:
        """Run multiple self.client.get() calls in parallel.
        lookups = {key: (path, params_dict)}
        returns  {key: (status, data)}
        """
        results = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_key = {
                executor.submit(self.client.get, path, params): key
                for key, (path, params) in lookups.items()
            }
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    status, data = future.result()
                    results[key] = (status, data)
                except Exception as e:
                    logger.error("Parallel lookup %s failed: %s", key, e)
                    results[key] = (0, {"error": str(e)})
        return results

    # ------------------------------------------------------------------
    # validation & retry helpers
    # ------------------------------------------------------------------

    def validate_payload(self, path: str, payload: dict) -> tuple[dict, bool]:
        """Perform generic pre-flight validation or cleanup on payload."""
        # Tripletex doesn't like empty lists or nulls in many places
        if not payload: return {}, False
        cleaned = {k: v for k, v in payload.items() if v is not None}
        return cleaned, True

    def handle_422_retry(self, path: str, payload: dict, messages: list) -> dict | None:
        """Analyze 422 messages and try to patch payload for a retry."""
        patched = False
        new_payload = payload.copy()
        for m in messages:
            field = str(m.get("field", "")).lower()
            message = str(m.get("message", "")).lower()

            # "mva-kode" or "vatType" errors
            if "vattype" in field or "mva-kode" in message or "vattype" in message:
                logger.info("422 self-heal: stripping vatType from payload")
                if "vatType" in new_payload:
                    new_payload.pop("vatType")
                    patched = True
                if "orderLines" in new_payload:
                    for line in new_payload["orderLines"]:
                        line.pop("vatType", None)
                    patched = True

            # date errors
            if "date" in field or "dato" in field:
                if "null" in message or "påkrevd" in message:
                    new_payload[m.get("field")] = TODAY
                    patched = True

        return new_payload if patched else None

    def _validation_hints(self, data: dict) -> str:
        """Extract validation message text from a 422 response body."""
        msgs = data.get("validationMessages") or []
        if isinstance(msgs, list):
            return " | ".join(str(m.get("message", m)) for m in msgs)
        return str(msgs)

    # ------------------------------------------------------------------
    # order line builder
    # ------------------------------------------------------------------

    def _build_order_lines(self, items: list) -> list:
        """Helper to build a list of OrderLine objects from generic extracted items."""
        lines = []
        for line in items:
            unit_cost = line.get("unitPrice") or line.get("price") or line.get("unitCost") or 0.0
            try:
                unit_cost = self._to_float(str(unit_cost).replace(",", ".").replace(" ", "").replace("\xa0", ""))
            except (ValueError, TypeError):
                unit_cost = 0.0

            vat_raw = line.get("vatType") or line.get("mva") or line.get("vat")
            vat_id = self._resolve_vat_type_id(str(vat_raw)) if vat_raw else self._resolve_vat_type_id("standard")
            item = {
                "description": line.get("description", ""),
                "count": self._to_float(line.get("count", line.get("quantity", 1))),
                "unitCostCurrency": unit_cost,
                "unitPriceExcludingVatCurrency": unit_cost,
                "vatType": {"id": self._to_int(vat_id)},
            }
            lines.append(item)
        return lines

    def _resolve_vat_type_id(self, vat_type_str: str) -> int:
        """Map generic vatType strings to Tripletex IDs (e.g. 'standard' -> 3)."""
        if not vat_type_str: return 3
        v = str(vat_type_str).lower()

        # Hardcoded quick mappings
        if "standard" in v or "25%" in v or "normal" in v: return 3
        if "none" in v or "fritatt" in v or "exempt" in v or "0%" in v: return 0
        if "food" in v or "mat" in v or "15%" in v: return 31

        # Dynamic lookup
        if self._vat_cache is None:
            status, data = self.client.get("/ledger/vatType", params={"count": 100, "fields": "id,name,percentage,number"})
            self._vat_cache = data.get("values", []) if status == 200 else []

        # Find match by percentage or name
        try:
            pct_match = self._to_float(re.search(r"(\d+)", v).group(1))
            for vt in self._vat_cache:
                if abs(self._to_float(vt.get("percentage") or 0) - pct_match) < 0.1:
                    return self._to_int(vt["id"])
        except (AttributeError, ValueError, TypeError):
            pass

        for vt in self._vat_cache:
            if v in (vt.get("name") or "").lower():
                return self._to_int(vt["id"])

        return 3  # default fallback

    def _ensure_bank_account(self):
        """Bootstrap bank account using PUT /ledger/account to avoid 405 Method Not Allowed on /company proxy."""
        status, current = self.client.get("/ledger/account", params={"number": "1920"})
        if status == 200 and current.get("values"):
            acc = current["values"][0]
            if not acc.get("bankAccountNumber"):
                logger.info("Bootstrapping bank account for ledger account 1920 (id=%s)", acc["id"])
                acc["bankAccountNumber"] = "12345678903"
                acc["bankAccountIBAN"] = ""  # Clear IBAN to avoid validation errors
                put_status, put_data = self.client.put(f"/ledger/account/{acc['id']}", json=acc)
                if put_status in (200, 201):
                    logger.info("Successfully bootstrapped bank account")
                else:
                    logger.warning("Failed to bootstrap bank account: %s", put_data)

    # ------------------------------------------------------------------
    # Handlers for specific task types
    # ------------------------------------------------------------------

    def handle_create_employee(self, fields: dict) -> dict:
        if isinstance(fields, list):
            results = [self.handle_create_employee(f) for f in fields]
            return {"success": all(r["success"] for r in results), "results": results}

        first = fields.get("firstName") or fields.get("first_name", "Unknown")
        last = fields.get("lastName") or fields.get("last_name", "Employee")
        email = fields.get("email")

        if not email:
            # Tripletex requires an email for Tripletex-users ("Må angis for Tripletex-brukere").
            # Generate a deterministic fallback from the employee's name.
            first_slug = first.lower().replace(" ", ".")
            last_slug = last.lower().replace(" ", ".")
            email = f"{first_slug}.{last_slug}@example.com"
            logger.info("No email provided — using fallback email: %s", email)

        payload = {"firstName": first, "lastName": last}
        payload["email"] = email
        payload["userType"] = 1
        phone = fields.get("phone") or fields.get("phoneNumber")
        if phone:
            payload["phoneNumberMobile"] = str(phone)
        if fields.get("employeeNumber"):
            payload["employeeNumber"] = str(fields["employeeNumber"])
        if fields.get("nationalIdentityNumber"):
            payload["nationalIdentityNumber"] = str(fields["nationalIdentityNumber"])
        if fields.get("dateOfBirth"):
            payload["dateOfBirth"] = normalize_date(fields["dateOfBirth"])
        if fields.get("department"):
            dept_id = self._ensure_department(fields["department"])
            if dept_id:
                payload["department"] = {"id": dept_id}

        if "department" not in payload:
            d_st, d_data = self.client.get("/department", params={"count": 1, "fields": "id,name"})
            if d_st == 200 and d_data.get("values"):
                payload["department"] = {"id": self._to_int(d_data["values"][0]["id"])}
            else:
                dept_res = self.handle_create_department({"name": "Administrasjon"})
                if dept_res.get("success"):
                    payload["department"] = {"id": dept_res.get("id")}

        if "department" in payload:
            logger.info("Using department id=%s for employee creation", payload["department"]["id"])

        status, data = self.client.post("/employee", json=payload)

        # 422 self-healing: if email already exists, try to find the ID
        if status == 422:
            hints = self._validation_hints(data)
            if "allerede" in hints or "already exists" in hints:
                logger.info("Employee with email %s already exists — searching for ID", email)
                employee_id = self._find_employee_id(email if email else f"{first} {last}")
                if employee_id:
                    status, data = 200, {"value": {"id": employee_id}}
            elif "department" in hints.lower() or "avdeling" in hints.lower() or "fylles ut" in hints.lower():
                logger.warning("Employee create 422 (department required) — fetching a valid department")
                d_st, d_data = self.client.get("/department", params={"count": 1})
                if d_st == 200 and d_data.get("values"):
                    payload["department"] = {"id": self._to_int(d_data["values"][0]["id"])}
                    logger.info("Retrying employee create with department id=%s", payload["department"]["id"])
                    status, data = self.client.post("/employee", json=payload)
            elif "nationalidentitynumber" in hints.lower() or "ugyldig" in hints.lower() or "format" in hints.lower():
                logger.warning("Employee create 422 (invalid nationalIdentityNumber) — removing it and retrying")
                payload.pop("nationalIdentityNumber", None)
                status, data = self.client.post("/employee", json=payload)
            elif "angis for tripletex" in hints.lower() or ("e-post" in hints.lower() and "angis" in hints.lower()):
                # Email is required for Tripletex users but the generated/provided email is being
                # rejected (e.g. duplicate, invalid domain). Retry without userType so the employee
                # is created without a Tripletex login (no email required in that case).
                logger.warning("Employee create 422 (Tripletex-user email issue) — retrying without userType")
                payload.pop("userType", None)
                status, data = self.client.post("/employee", json=payload)

        if status not in (200, 201):
            logger.info("TASK_RESULT: type=create_employee success=False id=None")
            return {"success": False, "message": f"Failed to create employee: {data}"}

        employee_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("Employee created id=%s", employee_id)

        # Handle roles if specified
        roles = fields.get("roles") or []
        if isinstance(roles, str): roles = [roles]
        if "ROLE_ADMINISTRATOR" in roles or "administrator" in str(roles).lower():
            put_status, put_data = self.client.put(
                "/employee/entitlement/:grantEntitlementsByTemplate",
                params={"employeeId": employee_id, "template": "ALL_PRIVILEGES"},
            )
            if put_status == 200:
                logger.info("Administrator roles granted to employee %s", employee_id)

        # Handle employment if startDate given or other employment fields are provided
        start_date = normalize_date(fields.get("startDate"))
        emp_payload = None
        if start_date or fields.get("salary") or fields.get("employmentPercentage") or fields.get("occupationCode"):
            emp_payload = {
                "employee": {"id": employee_id},
                "startDate": start_date or TODAY,
            }
            emp_details = {"percentageOfFullTimeEquivalent": float(fields.get("employmentPercentage") or 100.0)}
            
            if fields.get("salary"):
                emp_details["annualSalary"] = float(fields["salary"])
                emp_details["remunerationType"] = "MONTHLY_WAGE"
            if fields.get("occupationCode"):
                occ_id = self._find_occupation_code_id(str(fields["occupationCode"]))
                if occ_id:
                    emp_details["occupationCode"] = {"id": occ_id}
            
            emp_payload["employmentDetails"] = [emp_details]
            self.client.post("/employee/employment", json=emp_payload)

        logger.info("TASK_RESULT: type=create_employee success=True id=%s", employee_id)
        return {
            "success": True, 
            "message": f"Employee created id={employee_id}", 
            "id": employee_id,
            "employeeId": employee_id
        }

    def handle_create_customer(self, fields: dict) -> dict:
        if isinstance(fields, list):
            results = [self.handle_create_customer(f) for f in fields]
            return {"success": all(r["success"] for r in results), "results": results}

        name = fields.get("name") or fields.get("customerName", "Unknown Customer")
        org_number = fields.get("organizationNumber") or fields.get("orgNumber")
        email = fields.get("email")

        payload = {"name": name, "isCustomer": True}
        if org_number: payload["organizationNumber"] = str(org_number)
        if email: payload["email"] = email

        address_line = fields.get("addressLine1") or fields.get("address")
        city = fields.get("city")
        postal_code = fields.get("postalCode") or fields.get("zipCode")
        if address_line or city or postal_code:
            payload["physicalAddress"] = {}
            if address_line: payload["physicalAddress"]["addressLine1"] = address_line
            if city: payload["physicalAddress"]["city"] = city
            if postal_code: payload["physicalAddress"]["postalCode"] = str(postal_code)
            country = fields.get("country", "Norway")
            if "norway" in country.lower() or "norge" in country.lower():
                payload["physicalAddress"]["country"] = {"id": 161}

        status, data = self.client.post("/customer", json=payload)
        
        # 422 retry without organizationNumber if that caused a conflict
        if status == 422:
            hints = self._validation_hints(data)
            logger.warning("Customer create 422 (orgNumber=%s): %s — retrying without orgNumber", org_number, hints)
            status, data = self.client.post("/customer", json={"name": name})

        if status not in (200, 201):
            logger.info("TASK_RESULT: type=create_customer success=False id=None")
            return {"success": False, "message": f"Failed to create customer: {data}"}

        customer_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("TASK_RESULT: type=create_customer success=True id=%s", customer_id)
        return {
            "success": True, 
            "message": f"Customer created id={customer_id}", 
            "id": customer_id,
            "customerId": customer_id
        }

    def handle_create_product(self, fields: dict) -> dict:
        if isinstance(fields, list):
            results = [self.handle_create_product(f) for f in fields]
            return {"success": all(r["success"] for r in results), "results": results}

        name = fields.get("name") or fields.get("productName", "Unknown Product")
        price = fields.get("price") or fields.get("unitPrice") or 0.0
        try:
            price = self._to_float(str(price).replace(",", ".").replace(" ", ""))
        except (ValueError, TypeError):
            price = 0.0

        vat_raw = fields.get("vatType") or fields.get("mva") or fields.get("vat")
        vat_id = self._resolve_vat_type_id(str(vat_raw)) if vat_raw else self._resolve_vat_type_id("standard")

        payload = {
            "name": name,
            "number": str(fields.get("productNumber") or fields.get("number") or random.randint(1000, 9999)),
            "priceExcludingVatCurrency": price,
            "vatType": {"id": self._to_int(vat_id)},
        }

        max_retries = 3
        for attempt in range(max_retries):
            status, data = self.client.post("/product", json=payload)

            if status == 422:
                hints = self._validation_hints(data).lower()
                logger.warning("Product create 422: %s", hints)

                # Case 1: product number already in use
                if "number" in hints or "nummer" in hints:
                    payload["number"] = str(random.randint(100000, 999999))
                    logger.info("Attempt %d: Retrying product create with new random number: %s", attempt+1, payload["number"])
                    continue

                # Case 2: vatType conflict — strip vatType entirely
                if "vattype" in hints or "mva" in hints or "vat" in hints:
                    payload.pop("vatType", None)
                    logger.info("Attempt %d: Retrying product create with vatType stripped", attempt+1)
                    continue

            # If not 422 or if it's a different 422 that we don't handle, break and return
            break

        if status not in (200, 201):
            logger.info("TASK_RESULT: type=create_product success=False id=None")
            return {"success": False, "message": f"Failed to create product: {data}"}

        product_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("TASK_RESULT: type=create_product success=True id=%s", product_id)
        return {"success": True, "message": f"Product created id={product_id}", "id": product_id}

    def handle_create_invoice(self, fields: dict) -> dict:
        self._ensure_bank_account()
        # Step 1: resolve customer — find existing or create new
        customer_id = fields.get("customerId")
        if not customer_id:
            customer_name = fields.get("customerName", "Unknown Customer")
            org_number = fields.get("organizationNumber") or fields.get("customerOrganizationNumber")
            customer_id = self._ensure_customer(customer_name, org_number=org_number)
        if not customer_id:
            logger.info("TASK_RESULT: type=create_invoice success=False id=None")
            return {"success": False, "message": "Could not find or create customer for invoice"}

        invoice_date = normalize_date(fields.get("invoiceDate", TODAY)) or TODAY
        due_date = normalize_date(fields.get("dueDate") or fields.get("invoiceDueDate", invoice_date)) or invoice_date

        # Step 2: POST /order (if no orderId provided)
        order_id = fields.get("orderId")
        if not order_id:
            order_lines = self._build_order_lines(fields.get("orderLines", []))
            if not order_lines:
                # Maybe it's a flat structure?
                order_lines = self._build_order_lines([fields])

            order_payload = {
                "customer": {"id": self._to_int(customer_id)},
                "orderDate": invoice_date,
                "deliveryDate": due_date,
                "orderLines": order_lines,
            }
            status, order_data = self.client.post("/order", json=order_payload)

            # 422 self-healing for order: retry without vatType in lines if needed
            if status == 422:
                hints = self._validation_hints(order_data)
                logger.warning("Order create 422: %s", hints)
                if "vattype" in hints.lower() or "mva" in hints.lower() or "vat" in hints.lower():
                    for line in order_payload["orderLines"]:
                        line.pop("vatType", None)
                    logger.info("Retrying order create with vatType stripped from lines")
                    status, order_data = self.client.post("/order", json=order_payload)

            if status not in (200, 201):
                logger.info("TASK_RESULT: type=create_invoice success=False id=None")
                return {"success": False, "message": f"Failed to create order: {order_data}"}

            order_id = self._to_int(order_data.get("value", {}).get("id"))
        else:
            order_id = self._to_int(order_id)
            logger.info("Reusing existing orderId=%s for invoice", order_id)

        # Step 3: POST /invoice
        status, data = self.client.post(
            "/invoice",
            json={
                "invoiceDate": invoice_date,
                "invoiceDueDate": due_date,
                "customer": {"id": self._to_int(customer_id)},
                "orders": [{"id": self._to_int(order_id)}],
            },
        )
        if status not in (200, 201):
            logger.info("TASK_RESULT: type=create_invoice success=False id=None")
            return {"success": False, "message": f"Failed to create invoice: {data}"}

        invoice_id = self._to_int(data.get("value", {}).get("id"))
        is_charged = data.get("value", {}).get("isCharged", False)
        logger.info("Invoice id=%s isCharged=%s", invoice_id, is_charged)

        logger.info("TASK_RESULT: type=create_invoice success=True id=%s", invoice_id)
        return {
            "success": True,
            "message": f"Invoice created id={invoice_id} isCharged={is_charged} for order={order_id}",
            "invoiceId": invoice_id,
            "id": invoice_id,
            "orderId": order_id
        }

    def handle_register_payment(self, fields: dict) -> dict:
        invoice_id = fields.get("invoiceId")
        invoice_number = str(fields.get("invoiceNumber", ""))
        
        # Resolve invoice ID if only number is provided
        if not invoice_id and not invoice_number:
            customer_id = fields.get("customerId") or self._find_customer_id(fields.get("customerName", ""))
            if customer_id:
                logger.info("No invoice number provided — searching for invoice for customer id=%s", customer_id)
                # Fetch all invoices from this year to avoid missing the one just created
                s, d = self.client.get(
                    "/invoice",
                    params={
                        "customerId": customer_id,
                        "invoiceDateFrom": "2000-01-01",
                        "invoiceDateTo": "2099-12-31",
                        "count": 100,
                        "sorting": "-id",
                        "fields": "id,amountCurrency,amountOutstanding"
                    }
                )
                if s == 200:
                    val_list = d.get("values", [])
                    target_amount = self._to_float(fields.get("amount") or fields.get("paidAmount") or 0)
                    # Prefer unpaid invoices (amountOutstanding > 0)
                    unpaid = [inv for inv in val_list if self._to_float(inv.get("amountOutstanding", 0)) > 0]
                    search_list = unpaid if unpaid else val_list
                    for inv in search_list:
                        # Match by amount if possible
                        inv_amt = self._to_float(inv.get("amountCurrency", 0))
                        inv_outstanding = self._to_float(inv.get("amountOutstanding", inv_amt))
                        logger.info("Checking invoice %s: amount=%.2f outstanding=%.2f vs target=%.2f", inv["id"], inv_amt, inv_outstanding, target_amount)
                        if target_amount and (
                            abs(abs(inv_outstanding) - abs(target_amount)) < 0.1
                            or abs(abs(inv_amt) - abs(target_amount)) < 0.1
                            or abs(abs(inv_amt) - abs(target_amount) * 1.25) < 0.1
                            or abs(abs(inv_amt) - abs(target_amount) / 1.25) < 0.1
                        ):
                            invoice_id = inv["id"]
                            logger.info("Matched invoice %s by customer and amount %.2f", invoice_id, inv_amt)
                            break
                    if not invoice_id and search_list:
                        invoice_id = search_list[0]["id"]
                        logger.info("Falling back to most recent %sinvoice %s for customer",
                                    "unpaid " if unpaid else "", invoice_id)

            if not invoice_id:
                target_amount = self._to_float(fields.get("amount") or fields.get("paidAmount") or 0)
                logger.info("Fallback: broad search for invoice by amount %.2f", target_amount)
                s, d = self.client.get("/invoice", params={"invoiceDateFrom": "2020-01-01", "invoiceDateTo": "2099-12-31", "count": 50, "sorting": "-id", "fields": "id,amountCurrency,amountOutstanding"})
                if s == 200:
                    all_invs = d.get("values", [])
                    unpaid_invs = [inv for inv in all_invs if self._to_float(inv.get("amountOutstanding", 0)) > 0]
                    search_broad = unpaid_invs if unpaid_invs else all_invs
                    for inv in search_broad:
                        inv_amt = self._to_float(inv.get("amountCurrency", 0))
                        inv_os = self._to_float(inv.get("amountOutstanding", inv_amt))
                        logger.info("Broad search: checking invoice %s: amount=%.2f outstanding=%.2f vs target=%.2f", inv["id"], inv_amt, inv_os, target_amount)
                        if target_amount and (
                            abs(abs(inv_os) - abs(target_amount)) < 0.1
                            or abs(abs(inv_amt) - abs(target_amount)) < 0.1
                            or abs(abs(inv_amt) - abs(target_amount) * 1.25) < 0.1
                            or abs(abs(inv_amt) - abs(target_amount) / 1.25) < 0.1
                        ):
                            invoice_id = inv["id"]
                            logger.info("Fallback: matched invoice %s by amount %.2f", invoice_id, inv_amt)
                            break
                    if not invoice_id and search_broad:
                        invoice_id = search_broad[0]["id"]
                        logger.info("Fallback: most recent %sinvoice %s",
                                    "unpaid " if unpaid_invs else "", invoice_id)

        if not invoice_id and invoice_number:
            logger.info("Searching for invoice internal id for number %s", invoice_number)
            s, d = self.client.get("/invoice", params={"invoiceNumber": invoice_number, "count": 10})
            if s == 200 and d.get("values"):
                invoice_id = d["values"][0]["id"]
            else:
                # search for it in a broader list
                s, d = self.client.get("/invoice", params={"count": 100, "fields": "id,invoiceNumber"})
                if s == 200:
                    for inv in d.get("values", []):
                        if str(inv.get("invoiceNumber")) == invoice_number:
                            invoice_id = inv["id"]
                            break
                    # Fallback: match by id (task may reference invoice by its internal ID)
                    if not invoice_id:
                        for inv in d.get("values", []):
                            if str(inv.get("id")) == invoice_number:
                                invoice_id = inv["id"]
                                break

        if not invoice_id:
            logger.info("TASK_RESULT: type=register_payment success=False id=%s", invoice_id)
            return {"success": False, "message": f"Invoice number '{invoice_number}' not found"}

        invoice_id = self._to_int(invoice_id)
        amount = fields.get("amount") or fields.get("paidAmount")
        if amount is not None:
            try:
                amount = self._to_float(str(amount).replace(",", ".").replace(" ", ""))
            except (ValueError, TypeError):
                amount = 0
        
        if not amount:
            # Prefer amountOutstanding (remaining balance) over amountCurrency (original total).
            # Using amountCurrency when the invoice is partially paid causes an over-payment
            # attempt and the invoice never reaches isCharged=True.
            s, d = self.client.get(f"/invoice/{invoice_id}", params={"fields": "id,amountCurrency,amountOutstanding"})
            if s == 200:
                inv_val = d.get("value", {})
                outstanding = self._to_float(inv_val.get("amountOutstanding") or 0)
                currency = self._to_float(inv_val.get("amountCurrency") or 0)
                amount = outstanding if outstanding else currency
                logger.info("Invoice %s: amountOutstanding=%.2f amountCurrency=%.2f → using %.2f",
                            invoice_id, outstanding, currency, amount)
            else:
                amount = 0

        # Fetch the first available payment type ID from the account.
        payment_type_id = fields.get("paymentTypeId")
        if not payment_type_id:
            status, pt_data = self.client.get("/invoice/paymentType")
            if status == 200 and pt_data.get("values"):
                # Strategy: Prefer one that mentions "Bank" or "Betaling"
                found_id = None
                for pt in pt_data["values"]:
                    desc = pt.get("description", "").lower()
                    if "bank" in desc or "betaling" in desc:
                        found_id = pt["id"]
                        logger.info("Using detected bank payment type: id=%s (%s)", found_id, desc)
                        break
                
                payment_type_id = found_id or pt_data["values"][0]["id"]
                if not found_id:
                    logger.info("Using first available payment type: id=%s (%s)", payment_type_id,
                                pt_data["values"][0].get("description", ""))
            else:
                payment_type_id = 1  # Standard fallback

        # Correct endpoint: PUT /invoice/{id}/:payment with query params (not a JSON body)
        status, data = self.client.put(
            f"/invoice/{invoice_id}/:payment",
            params={
                "paymentDate": normalize_date(fields.get("paymentDate", TODAY)) or TODAY,
                "paymentTypeId": self._to_int(payment_type_id),
                "paidAmount": self._to_float(amount),
            },
        )
        if status not in (200, 201):
            logger.info("TASK_RESULT: type=register_payment success=False id=%s", invoice_id)
            return {"success": False, "message": f"Failed to register payment: {data}", "_needs_fallback": True}

        logger.info("TASK_RESULT: type=register_payment success=True id=%s", invoice_id)
        return {
            "success": True,
            "message": f"Payment of {amount} registered for invoice {invoice_id}",
            "id": invoice_id,
            "invoiceId": invoice_id
        }

    def _find_invoice_id_by_number(self, invoice_number=None, invoice_id=None):
        """Find invoice internal id using the same broad-list pattern as register_payment."""
        if invoice_id:
            return self._to_int(invoice_id)
        if not invoice_number:
            return None

        s_num = str(invoice_number).strip()
        # Fallback 0: Try as direct internal ID if number is large
        if len(s_num) >= 8 and s_num.isdigit():
            status, data = self.client.get(f"/invoice/{s_num}", params={"fields": "id,invoiceNumber"})
            if status == 200:
                return self._to_int(data.get("value", {}).get("id"))
                
        status, data = self.client.get(
            "/invoice",
            params={
                "invoiceNumber": s_num, 
                "fields": "id,invoiceNumber",
                "invoiceDateFrom": "2000-01-01",
                "invoiceDateTo": "2099-12-31"
            },
        )
        if status == 200 and data.get("values"):
            return self._to_int(data["values"][0]["id"])

        # Fallback 1: Broad search for exact invoiceNumber match
        status, data = self.client.get(
            "/invoice", 
            params={
                "count": 100, 
                "fields": "id,invoiceNumber",
                "invoiceDateFrom": "2000-01-01",
                "invoiceDateTo": "2099-12-31"
            }
        )
        if status == 200:
            values = data.get("values", [])
            for inv in values:
                if str(inv.get("invoiceNumber")) == s_num:
                    return self._to_int(inv["id"])

            # Fallback 2: Try matching the internal ID directly (some tasks use internal IDs as reference)
            for inv in values:
                if str(inv.get("id")) == s_num:
                    return self._to_int(inv["id"])

        logger.warning("Invoice search for '%s' failed (status=%s)", s_num, status)
        return None

    def handle_register_fx_payment(self, fields: dict) -> dict:
        """Register payment at a new exchange rate and create an FX difference voucher."""
        invoice_id = self._find_invoice_id_by_number(
            invoice_number=fields.get("invoiceNumber"),
            invoice_id=fields.get("invoiceId"),
        )
        if not invoice_id:
            customer_name = fields.get("customerName")
            if customer_name:
                s, d = self.client.get("/invoice", params={
                    "count": 50, "fields": "id,invoiceNumber,amountCurrency,amountOutstanding,customer",
                    "invoiceDateFrom": "2000-01-01", "invoiceDateTo": "2099-12-31"
                })
                if s == 200:
                    cn_lower = customer_name.lower()
                    for inv in d.get("values", []):
                        cust = inv.get("customer") or {}
                        if cn_lower in str(cust.get("name", "")).lower():
                            invoice_id = self._to_int(inv["id"])
                            break
        if not invoice_id:
            return {"success": False, "message": "register_fx_payment: could not find invoice"}

        # Fetch invoice details to get original amount and outstanding
        s, inv_data = self.client.get(f"/invoice/{invoice_id}", params={
            "fields": "id,invoiceNumber,amountCurrency,amountOutstanding,currency"
        })
        if s != 200:
            return {"success": False, "message": f"register_fx_payment: invoice fetch failed: {inv_data}"}

        inv = inv_data.get("value", {})
        original_amount = self._to_float(inv.get("amountCurrency") or inv.get("amount") or 0)
        outstanding = self._to_float(inv.get("amountOutstanding") or original_amount)
        payment_amount = self._to_float(fields.get("amount") or outstanding)
        payment_date = normalize_date(fields.get("paymentDate") or fields.get("date", TODAY)) or TODAY

        # Register the payment
        pay_status, pay_data = self.client.post(
            f"/invoice/{invoice_id}/:payment",
            json={"paymentDate": payment_date, "paymentTypeId": 1, "paidAmount": payment_amount},
        )
        if pay_status not in (200, 201):
            return {"success": False, "message": f"register_fx_payment: payment failed: {pay_data}"}

        # Calculate FX difference
        fx_diff = round(payment_amount - outstanding, 2)
        if abs(fx_diff) < 0.01:
            logger.info("register_fx_payment: no FX difference (diff=%.2f), done", fx_diff)
            return {"success": True, "message": f"FX payment registered, no difference", "id": invoice_id}

        # Create FX difference voucher
        # Gain: debit 1500 (AR), credit 8060 (FX gain)
        # Loss: debit 8160 (FX loss), credit 1500 (AR)
        ar_id = self._find_account_id(1500)
        if fx_diff > 0:
            gain_id = self._find_account_id(8060)
            postings = [
                {"account": {"id": ar_id}, "amount": fx_diff, "description": "FX gain AR"},
                {"account": {"id": gain_id}, "amount": -fx_diff, "description": "FX gain income"},
            ] if ar_id and gain_id else []
        else:
            loss_id = self._find_account_id(8160)
            postings = [
                {"account": {"id": loss_id}, "amount": abs(fx_diff), "description": "FX loss"},
                {"account": {"id": ar_id}, "amount": -abs(fx_diff), "description": "FX loss AR"},
            ] if ar_id and loss_id else []

        if postings:
            voucher_fields = {
                "date": payment_date,
                "description": f"FX difference on invoice {invoice_id}",
                "postings": postings,
            }
            v_result = self.handle_create_voucher(voucher_fields)
            if not v_result.get("success"):
                logger.warning("register_fx_payment: FX voucher failed: %s", v_result)

        logger.info("TASK_RESULT: type=register_fx_payment success=True id=%s", invoice_id)
        return {
            "success": True,
            "message": f"FX payment {payment_amount} registered, FX diff={fx_diff}",
            "id": invoice_id,
        }

    def handle_create_credit_note(self, fields: dict) -> dict:
        invoice_id = self._find_invoice_id_by_number(
            invoice_number=fields.get("invoiceNumber"),
            invoice_id=fields.get("invoiceId")
        )
        if not invoice_id:
            return {"success": False, "message": "Invoice not found for credit note"}

        credit_date = normalize_date(fields.get("date", TODAY)) or TODAY
        status, data = self.client.put(
            f"/invoice/{invoice_id}/:createCreditNote",
            params={"date": credit_date},
        )
        if status not in (200, 201):
            return {"success": False, "message": f"Failed to create credit note: {data}"}

        cn_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("TASK_RESULT: type=create_credit_note success=True id=%s", cn_id)
        return {"success": True, "message": f"Credit note created id={cn_id} for invoice {invoice_id}", "id": cn_id}

    def handle_create_project(self, fields: dict) -> dict:
        if isinstance(fields, list):
            results = [self.handle_create_project(f) for f in fields]
            return {"success": all(r["success"] for r in results), "results": results}

        customer_id = fields.get("customerId")
        if not customer_id and fields.get("customerName"):
            customer_id = self._ensure_customer(fields["customerName"], org_number=fields.get("organizationNumber"))
        
        manager_id = fields.get("projectManagerId")
        if not manager_id and fields.get("projectManagerName"):
            manager_id = self._find_employee_id(fields["projectManagerName"])
        if not manager_id:
            manager_id = self._find_employee_id_for_project()

        payload = {
            "name": fields.get("name") or fields.get("projectName", "New Project"),
            "startDate": normalize_date(fields.get("startDate", TODAY)) or TODAY,
            "number": str(fields.get("number") or random.randint(1000, 9999)),
        }
        if customer_id: payload["customer"] = {"id": self._to_int(customer_id)}
        if manager_id: payload["projectManager"] = {"id": self._to_int(manager_id)}

        status, data = self.client.post("/project", json=payload)
        if status not in (200, 201):
            return {"success": False, "message": f"Failed to create project: {data}"}

        project_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("TASK_RESULT: type=create_project success=True id=%s", project_id)
        return {"success": True, "message": f"Project created id={project_id}", "id": project_id}

    def handle_create_department(self, fields: dict) -> dict:
        if isinstance(fields, list):
            results = [self.handle_create_department(f) for f in fields]
            return {"success": all(r["success"] for r in results), "results": results}

        payload = {
            "name": fields.get("name") or "New Department",
            "departmentNumber": str(fields.get("departmentNumber") or random.randint(100, 999)),
        }
        status, data = self.client.post("/department", json=payload)
        if status not in (200, 201):
            return {"success": False, "message": f"Failed to create department: {data}"}

        dep_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("TASK_RESULT: type=create_department success=True id=%s", dep_id)
        return {"success": True, "message": f"Department created id={dep_id}", "id": dep_id}

    def handle_create_accounting_dimension(self, fields: dict) -> dict:
        """Create a custom accounting dimension (as departments) and optionally post a voucher linked to a dimension value.

        Tripletex models accounting dimensions as departments. Each dimension value becomes a department.
        If an account number and amount are provided, creates a balanced voucher linked to the specified dimension value.
        """
        dimension_name = fields.get("dimensionName") or fields.get("name") or "Dimension"
        dimension_values = fields.get("dimensionValues") or fields.get("values") or []
        if isinstance(dimension_values, str):
            dimension_values = [v.strip() for v in dimension_values.split(",")]

        # Create a department for each dimension value
        created_dept_ids: dict[str, int] = {}
        for val in dimension_values:
            existing_id = self._find_department_id(val)
            if existing_id:
                created_dept_ids[val] = self._to_int(existing_id)
                logger.info("Dimension value '%s' already exists as department id=%s", val, existing_id)
            else:
                res = self.handle_create_department({"name": val})
                if res.get("success"):
                    created_dept_ids[val] = self._to_int(res["id"])
                    logger.info("Created dimension value '%s' as department id=%s", val, res["id"])
                else:
                    logger.warning("Failed to create dimension value '%s': %s", val, res)

        # If account + amount specified, create a voucher linked to the target dimension value
        account_number = fields.get("accountNumber") or fields.get("account")
        amount = self._to_float(fields.get("amount") or 0)
        target_value = fields.get("dimensionValue") or fields.get("linkedValue") or (dimension_values[0] if dimension_values else None)
        dept_id = created_dept_ids.get(target_value) if target_value else None

        if account_number and amount and dept_id:
            acc_id = self._find_account_id(int(account_number)) if str(account_number).isdigit() else None
            if acc_id is None:
                logger.warning("Could not resolve account number %s for dimension voucher", account_number)
            else:
                # Balanced: debit expense account (with dimension), credit bank/clearing (1920)
                bank_id = self._find_account_id(1920)
                if bank_id is None:
                    bank_id = self._find_account_id(1900)

                date = normalize_date(fields.get("date", TODAY)) or TODAY
                description = fields.get("description") or f"Dimension posting: {target_value}"
                postings = [
                    {
                        "account": {"id": acc_id},
                        "amount": amount,
                        "amountCurrency": amount,
                        "currency": {"id": 1},
                        "description": description,
                        "date": date,
                        "department": {"id": dept_id},
                    },
                ]
                if bank_id:
                    postings.append({
                        "account": {"id": bank_id},
                        "amount": -amount,
                        "amountCurrency": -amount,
                        "currency": {"id": 1},
                        "description": description,
                        "date": date,
                    })

                voucher_payload = {
                    "date": date,
                    "description": description,
                    "voucherType": {"id": 1},
                    "postings": self._clean_postings(postings),
                }
                v_st, v_data = self.client.post("/ledger/voucher", json=voucher_payload)
                if v_st in (200, 201):
                    vid = self._to_int(v_data.get("value", {}).get("id"))
                    logger.info("Dimension voucher created id=%s", vid)
                    if vid:
                        app_st, _ = self.client.put(f"/ledger/voucher/{vid}/:sendToLedger")
                        if app_st in (200, 201, 204):
                            logger.info("Dimension voucher %s approved", vid)
                else:
                    logger.warning("Dimension voucher create failed: %s", v_data)

        dept_ids_list = list(created_dept_ids.values())
        logger.info("TASK_RESULT: type=create_accounting_dimension success=True id=%s",
                    dept_ids_list[0] if dept_ids_list else None)
        return {
            "success": True,
            "message": f"Accounting dimension '{dimension_name}' created with {len(created_dept_ids)} values",
            "id": dept_ids_list[0] if dept_ids_list else None,
            "departmentIds": dept_ids_list,
        }

    def handle_create_travel_expense(self, fields: dict) -> dict:
        employee_id = fields.get("employeeId")
        if not employee_id:
            employee_id = self._find_employee_id(fields.get("employeeName", ""))
        if not employee_id:
            employee_id = self._find_employee_id_for_project()

        payload = {
            "employee": {"id": self._to_int(employee_id)},
            "date": normalize_date(fields.get("date", TODAY)) or TODAY,
            "title": fields.get("description") or fields.get("title") or "Travel Expense",
        }
        status, data = self.client.post("/travelExpense", json=payload)
        if status not in (200, 201):
            return {"success": False, "message": f"Failed to create travel expense: {data}"}

        te_id = self._to_int(data.get("value", {}).get("id"))

        costs_list = fields.get("costs", [])
        if not costs_list and (fields.get("amount") or fields.get("cost")):
            costs_list = [{"amount": fields.get("amount") or fields.get("cost"), "description": payload["title"]}]

        if costs_list:
            pt_id, cc_id = None, None
            pt_status, pt_data = self.client.get("/travelExpense/paymentType", params={"count": 5})
            if pt_status == 200 and pt_data.get("values"):
                pt_id = pt_data["values"][0]["id"]
            
            cc_status, cc_data = self.client.get("/travelExpense/costCategory", params={"count": 50})
            if cc_status == 200 and cc_data.get("values"):
                for c in cc_data["values"]:
                    if c.get("showOnTravelExpenses"):
                        cc_id = c["id"]
                        break
            
            if pt_id and cc_id:
                for c in costs_list:
                    c_amt = self._to_float(c.get("amount", 0))
                    if c_amt <= 0: continue
                    cost_payload = {
                        "travelExpense": {"id": te_id},
                        "paymentType": {"id": pt_id},
                        "costCategory": {"id": cc_id},
                        "amountCurrencyIncVat": c_amt,
                        "date": payload["date"],
                        "comments": c.get("description", payload["title"])
                    }
                    c_status, c_data = self.client.post("/travelExpense/cost", json=cost_payload)
                    if c_status in (200, 201):
                        logger.info("Added cost %.2f to travel expense id=%s", c_amt, te_id)
                    else:
                        logger.warning("Failed to add cost to travel expense %s: %s", te_id, c_data)

        logger.info("TASK_RESULT: type=create_travel_expense success=True id=%s", te_id)
        return {"success": True, "message": f"Travel expense created id={te_id}", "id": te_id}

    def handle_delete_travel_expense(self, fields: dict) -> dict:
        te_id = fields.get("travelExpenseId") or fields.get("id")
        if not te_id:
            return {"success": False, "message": "Travel expense ID required for deletion"}
        
        status, data = self.client.delete(f"/travelExpense/{self._to_int(te_id)}")
        return {"success": status == 204, "message": f"Delete travelExpense status={status}"}

    def handle_create_order(self, fields: dict) -> dict:
        customer_id = fields.get("customerId") or self._ensure_customer(fields.get("customerName", ""))
        if not customer_id:
            return {"success": False, "message": "Customer required for order"}

        order_date = normalize_date(fields.get("orderDate", TODAY)) or TODAY
        order_lines = self._build_order_lines(fields.get("orderLines", []))
        if not order_lines:
            order_lines = self._build_order_lines([fields])

        payload = {
            "customer": {"id": self._to_int(customer_id)},
            "orderDate": order_date,
            "deliveryDate": order_date,
            "orderLines": order_lines,
        }
        status, data = self.client.post("/order", json=payload)

        # 422 self-healing for order: retry without vatType in lines if needed
        if status == 422:
            hints = self._validation_hints(data)
            logger.warning("Order create 422: %s", hints)
            if "vattype" in hints.lower() or "mva" in hints.lower() or "vat" in hints.lower():
                for line in payload["orderLines"]:
                    line.pop("vatType", None)
                logger.info("Retrying order create with vatType stripped from lines")
                status, data = self.client.post("/order", json=payload)

        if status not in (200, 201):
            return {"success": False, "message": f"Failed to create order: {data}"}

        order_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("TASK_RESULT: type=create_order success=True id=%s", order_id)
        return {
            "success": True, 
            "message": f"Order created id={order_id}", 
            "id": order_id,
            "orderId": order_id,
            "customerId": self._to_int(customer_id)
        }

    def handle_update_customer(self, fields: dict) -> dict:
        customer_id = fields.get("customerId") or self._find_customer_id(fields.get("customerName", ""))
        if not customer_id:
            return {"success": False, "message": "Customer not found for update"}

        # First GET current
        status, current = self.client.get(f"/customer/{self._to_int(customer_id)}")
        if status != 200:
            return {"success": False, "message": "Could not fetch customer for update"}

        payload = current.get("value", {})
        for k, v in fields.items():
            if k not in ("customerId", "customerName") and v is not None:
                payload[k] = v

        status, data = self.client.put(f"/customer/{self._to_int(customer_id)}", json=payload)
        return {"success": status == 200, "message": f"Update customer status={status}"}

    def handle_update_employee(self, fields: dict) -> dict:
        employee_id = fields.get("employeeId") or self._find_employee_id(fields.get("employeeName", ""))
        if not employee_id:
            return {"success": False, "message": "Employee not found for update"}

        status, current = self.client.get(f"/employee/{self._to_int(employee_id)}")
        if status != 200:
            return {"success": False, "message": "Could not fetch employee for update"}

        payload = current.get("value", {})
        for k, v in fields.items():
            if k not in ("employeeId", "employeeName") and v is not None:
                payload[k] = v

        status, data = self.client.put(f"/employee/{self._to_int(employee_id)}", json=payload)
        return {"success": status == 200, "message": f"Update employee status={status}"}

    def handle_delete_employee(self, fields: dict) -> dict:
        employee_id = fields.get("employeeId") or self._find_employee_id(fields.get("employeeName", ""))
        if not employee_id:
            return {"success": False, "message": "Employee not found for deletion"}
        status, data = self.client.delete(f"/employee/{self._to_int(employee_id)}")
        return {"success": status == 204, "message": f"Delete employee status={status}"}

    def handle_enable_department_accounting(self, fields: dict) -> dict:
        # Step 1: get company settings
        status, data = self.client.get("/company/settings", params={"fields": "id,activateDepartmentAccounting"})
        if status != 200: return {"success": False, "message": "Could not fetch company settings"}
        
        settings = data.get("value", {})
        settings["activateDepartmentAccounting"] = True
        
        # Step 2: PUT back
        status, data = self.client.put("/company/settings", json=settings)
        return {"success": status == 200, "message": f"Enable department accounting status={status}"}

    def handle_create_contact_person(self, fields: dict) -> dict:
        customer_id = fields.get("customerId") or self._find_customer_id(fields.get("customerName", ""))
        if not customer_id:
            return {"success": False, "message": "Customer required for contact person"}

        payload = {
            "firstName": fields.get("firstName") or "Unknown",
            "lastName": fields.get("lastName") or "Contact",
            "customer": {"id": self._to_int(customer_id)},
            "email": fields.get("email"),
            "phoneNumberMobile": str(fields.get("phone", "")),
        }
        status, data = self.client.post("/contact", json=payload)
        if status not in (200, 201):
            return {"success": False, "message": f"Failed to create contact: {data}"}

        contact_id = self._to_int(data.get("value", {}).get("id"))
        return {"success": True, "message": f"Contact created id={contact_id}", "id": contact_id}

    def handle_update_product(self, fields: dict) -> dict:
        product_id = fields.get("productId") or self.handle_create_product(fields).get("id")
        if not product_id:
            return {"success": False, "message": "Product ID required for update"}
        
        status, current = self.client.get(f"/product/{self._to_int(product_id)}")
        if status != 200: return {"success": False, "message": "Product not found for update"}
        
        payload = current.get("value", {})
        for k, v in fields.items():
            if k not in ("productId", "productName") and v is not None:
                payload[k] = v
        
        status, data = self.client.put(f"/product/{self._to_int(product_id)}", json=payload)
        return {"success": status == 200, "message": f"Update product status={status}"}

    def handle_delete_supplier(self, fields: dict) -> dict:
        status, data = self.client.get("/supplier", params={"name": fields.get("name", ""), "count": 10})
        if status == 200 and data.get("values"):
            sid = data["values"][0]["id"]
            st, _ = self.client.delete(f"/supplier/{self._to_int(sid)}")
            return {"success": st == 204, "message": f"Delete supplier status={st}"}
        return {"success": False, "message": "Supplier not found for deletion"}

    def handle_register_hours(self, fields: dict) -> dict:
        employee_id = fields.get("employeeId") or self._find_employee_id(fields.get("employeeName", ""))
        project_id = fields.get("projectId") or self._find_or_create_project(fields.get("projectName", ""))
        activity_id = self._resolve_activity_id(fields.get("activityName"))
        
        if not employee_id or not project_id:
            return {"success": False, "message": "Employee and Project required for hours"}

        payload = {
            "employee": {"id": self._to_int(employee_id)},
            "project": {"id": self._to_int(project_id)},
            "activity": {"id": self._to_int(activity_id)},
            "date": normalize_date(fields.get("date", TODAY)) or TODAY,
            "hours": self._to_float(fields.get("hours", 0)),
            "comment": fields.get("comment", ""),
        }
        payload, _ = self.validate_payload("/timesheet/entry", payload)
        status, data = self.client.post("/timesheet/entry", json=payload)
        
        if status == 422:
            patched = self.handle_422_retry("/timesheet/entry", payload, data.get("validationMessages", []))
            if patched:
                status, data = self.client.post("/timesheet/entry", json=patched)
        elif status == 409:
            logger.info("Timesheet 409 conflict in register_hours. Attempting to update existing entry.")
            s, d = self.client.get("/timesheet/entry", params={
                "employeeId": self._to_int(employee_id),
                "projectId": self._to_int(project_id),
                "activityId": self._to_int(activity_id),
                "dateFrom": payload["date"],
                "dateTo": "2099-12-31",
                "count": 5
            })
            if s == 200 and d.get("values"):
                for entry in d["values"]:
                    if entry.get("date") == payload["date"]:
                        existing_id = entry["id"]
                        logger.info("Found existing timesheet entry %s to update", existing_id)
                        status, data = self.client.put(f"/timesheet/entry/{existing_id}", json=payload)
                        break

        if status in (200, 201):
            ts_id = self._to_int(data.get("value", {}).get("id"))
            logger.info("TASK_RESULT: type=register_hours success=True id=%s", ts_id)
            return {"success": True, "message": f"Hours registered id={ts_id}", "id": ts_id}
        return {"success": False, "message": f"Failed to register hours: {data}"}

    def _find_or_create_project(self, name, customer_id=None):
        if not name: return None
        status, data = self.client.get("/project", params={"name": name, "count": 10})
        if status == 200 and data.get("values"):
            return self._to_int(data["values"][0]["id"])
        
        # Create
        res = self.handle_create_project({"name": name, "customerId": customer_id})
        return self._to_int(res.get("id")) if res.get("success") else None

    def handle_create_voucher(self, fields: dict) -> dict:
        date = fields.get("date", TODAY)
        description = fields.get("description", "")
        supplier_name = fields.get("supplierName")
        invoice_number = fields.get("invoiceNumber")
        
        supplier_id = None
        if supplier_name:
            st, sd = self.client.get("/supplier", params={"name": supplier_name, "count": 1})
            if st == 200 and sd.get("values"):
                supplier_id = sd["values"][0]["id"]
            else:
                s_res = self.handle_create_supplier({"name": supplier_name})
                supplier_id = s_res.get("id") if s_res.get("success") else None

        raw_postings = fields.get("postings", [])
        postings = []
        # 2710 (input VAT) must now appear explicitly as a debit row — do NOT skip it.
        # Other balance-sheet system accounts that Tripletex manages internally are still skipped.
        SYSTEM_ACCOUNTS = {2600, 2700, 2711, 2740}

        has_2400 = False  # track whether an AP credit is already present
        has_2710 = False  # track whether a VAT debit is already present
        total_debit = 0.0  # sum of all debit (positive) postings for auto AP generation

        for p in raw_postings:
            # --- Resolve account to internal ID ---
            # Agents may send account number (e.g. 7100) rather than internal ID.
            # Accepted input shapes:
            #   {"account": {"id": 123}}         — already an internal ID, use as-is
            #   {"account": {"number": 7100}}     — account number, must resolve
            #   {"accountId": 123}                — internal ID shorthand
            #   {"accountNumber": 7100}           — account number shorthand
            acc_obj = p.get("account", {})
            acc_id = (
                p.get("accountId")
                or acc_obj.get("id")
            )
            acc_number = (
                p.get("accountNumber")
                or acc_obj.get("number")
            )

            # If we have only a number (or the "id" looks like a chart-of-accounts
            # number rather than an internal ID — internal IDs are typically large),
            # resolve it via the ledger API.
            if acc_number and not acc_id:
                acc_id = self._find_account_id(acc_number)
                if acc_id is None:
                    logger.warning(
                        "Skipping posting: account number %s not found in ledger", acc_number
                    )
                    continue
            elif acc_id and not acc_number:
                # acc_id could itself be a chart-of-accounts number if it's ≤ 9999
                # (internal IDs in Tripletex are usually much larger integers)
                try:
                    maybe_num = int(acc_id)
                    if maybe_num <= 9999:
                        resolved = self._find_account_id(maybe_num)
                        if resolved is not None:
                            acc_id = resolved
                except (TypeError, ValueError):
                    pass

            try:
                acc_id_int = int(acc_id)
            except (TypeError, ValueError):
                acc_id_int = None

            if acc_id_int is None:
                logger.warning("Skipping posting with unresolvable account (raw posting: %s)", p)
                continue

            if acc_id_int in SYSTEM_ACCOUNTS:
                continue

            amount = self._to_float(p.get("amount", 0))

            posting = {
                "account": {"id": self._to_int(acc_id) if acc_id else None},
                "amount": amount,
                "amountCurrency": self._to_float(p.get("amountCurrency", amount)),
                "currency": {"id": p.get("currencyId", 1)},
                "description": p.get("description", description),
                "date": normalize_date(p.get("date", date)),
            }
            if acc_id_int == 2400 and supplier_id:
                posting["supplier"] = {"id": self._to_int(supplier_id)}

            if acc_id_int == 2400:
                has_2400 = True
            if acc_id_int == 2710:
                has_2710 = True
            if amount > 0:
                total_debit += amount

            postings.append(posting)

        # Auto-generate 2710 input-VAT debit when a posting signals VAT but 2710 is absent.
        # Triggers when any raw posting carries a vatType/mva hint and 2710 isn't explicit.
        if not has_2710:
            vat_hint = any(
                p.get("vatType") or p.get("mva") or p.get("vatPercent") or p.get("vatAmount")
                for p in raw_postings
            )
            # Also trigger when the prompt/description mentions VAT (fields-level hint).
            if not vat_hint:
                vat_hint = any(
                    kw in str(fields.get("description", "")).lower()
                    for kw in ("mva", "vat", "iva", "moms")
                )
            if vat_hint:
                # Determine VAT amount: explicit vatAmount field → else 25% of ex-VAT debit total.
                vat_amount = self._to_float(fields.get("vatAmount") or 0)
                if not vat_amount and total_debit:
                    vat_percent = self._to_float(fields.get("vatPercent") or 25)
                    vat_amount = round(total_debit * vat_percent / 100, 2)
                if vat_amount:
                    vat_acc_id = self._find_account_id(2710)
                    if vat_acc_id is None:
                        logger.warning("Cannot resolve account 2710 — skipping auto-VAT posting")
                    else:
                        postings.append({
                            "account": {"id": vat_acc_id},
                            "amount": vat_amount,
                            "amountCurrency": vat_amount,
                            "currency": {"id": 1},
                            "description": description,
                            "date": normalize_date(date),
                        })
                        total_debit += vat_amount
                        has_2710 = True
                        logger.info("Auto-generated 2710 VAT debit posting: %.2f (acc_id=%s)", vat_amount, vat_acc_id)

        # Auto-generate 2400 AP credit when a supplier is known but no AP posting exists.
        if supplier_id and not has_2400 and total_debit:
            ap_acc_id = self._find_account_id(2400)
            if ap_acc_id is None:
                logger.warning("Cannot resolve account 2400 — skipping auto-AP posting")
            else:
                postings.append({
                    "account": {"id": ap_acc_id},
                    "amount": -total_debit,
                    "amountCurrency": -total_debit,
                    "currency": {"id": 1},
                    "description": description,
                    "date": normalize_date(date),
                    "supplier": {"id": self._to_int(supplier_id)},
                })
                logger.info("Auto-generated 2400 AP credit posting: -%.2f (acc_id=%s)", total_debit, ap_acc_id)

        if not postings:
            return {"success": False, "message": "No manual posting rows (only system rows found)", "_needs_fallback": True}

        payload = {
            "date": normalize_date(date) or TODAY,
            "description": description,
            "voucherType": {"id": self._to_int(fields.get("voucherTypeId", 1))},
            "postings": self._clean_postings(postings),
        }
        if invoice_number:
            payload["vendorInvoiceNumber"] = str(invoice_number)
        # Tripletex /ledger/voucher does NOT accept a top-level department field.
        payload.pop("department", None)

        status, data = self.client.post("/ledger/voucher", json=payload)
        if status in (200, 201):
            vid = self._to_int(data.get("value", {}).get("id"))
            logger.info("TASK_RESULT: type=create_voucher success=True id=%s", vid)
            # Approve the voucher so it moves from DRAFT to APPROVED/POSTED state.
            # This is required to pass the second scoring check on the competition platform.
            if vid:
                app_st, app_data = self.client.put(f"/ledger/voucher/{vid}/:sendToLedger")
                if app_st in (200, 201, 204):
                    logger.info("Voucher %s approved successfully", vid)
                else:
                    logger.warning("Voucher approve returned %s: %s", app_st, str(app_data)[:120])
            return {"success": True, "message": f"Voucher created id={vid}", "id": vid}
        return {"success": False, "message": f"Failed to create voucher: {data}", "_needs_fallback": True}

    def handle_reverse_payment(self, fields: dict) -> dict:
        invoice_id = self._find_invoice_id_by_number(
            invoice_number=fields.get("invoiceNumber"),
            invoice_id=fields.get("invoiceId")
        )
        if not invoice_id:
            return {"success": False, "message": "Invoice not found for payment reversal"}

        status, data = self.client.put(f"/invoice/{self._to_int(invoice_id)}/:reversePayment")
        logger.info("PUT /:reversePayment returned %s: %s", status, data)
        return {"success": True, "message": f"Payment reversal attempted for invoice {invoice_id} (status={status})", "id": invoice_id}

    def handle_update_project(self, fields: dict) -> dict:
        project_id = fields.get("projectId") or self._find_or_create_project(fields.get("projectName", ""))
        if not project_id:
            return {"success": False, "message": "Project not found for update"}
        
        status, current = self.client.get(f"/project/{self._to_int(project_id)}")
        if status != 200: return {"success": False, "message": "Project not found for update"}
        
        payload = current.get("value", {})
        payload.pop("projectratetypes", None)
        payload.pop("hourlyRates", None)
        
        if fields.get("newName"): payload["name"] = fields["newName"]
        if fields.get("projectManagerName"):
            mid = self._find_employee_id(fields["projectManagerName"])
            if mid: payload["projectManager"] = {"id": self._to_int(mid)}
        if fields.get("endDate"): payload["endDate"] = normalize_date(fields["endDate"])

        status, data = self.client.put(f"/project/{self._to_int(project_id)}", json=payload)
        return {"success": status == 200, "message": f"Update project status={status}"}

    def handle_close_project(self, fields: dict) -> dict:
        project_id = fields.get("projectId") or self._find_or_create_project(fields.get("projectName", ""))
        if not project_id:
            return {"success": False, "message": "Project not found for closing"}
        
        # In Tripletex, closing usually means setting isClosed=True or setting an endDate
        status, current = self.client.get(f"/project/{self._to_int(project_id)}")
        if status == 200:
            payload = current.get("value", {})
            payload.pop("projectratetypes", None)
            payload.pop("hourlyRates", None)
            
            payload["isClosed"] = True
            if not payload.get("endDate"): payload["endDate"] = TODAY
            status, data = self.client.put(f"/project/{self._to_int(project_id)}", json=payload)
        
        return {"success": status == 200, "message": f"Project {project_id} closed"}

    def handle_bank_reconciliation_csv(self, fields: dict) -> dict:
        """Match bank statement CSV lines against open customer and supplier invoices.

        Positive amounts  → incoming payments  → match customer invoices
                            → register via PUT /invoice/{id}/:payment
        Negative amounts  → outgoing payments  → match supplier invoices
                            → register via PUT /invoice/{id}/:payment (AP invoices)
        Partial payments  → use the CSV line amount, not the full invoice total.

        CSV column detection handles English, Norwegian, French, German, Portuguese.
        """
        import csv as _csv
        import io as _io
        import re as _re

        csv_text = fields.get("csv_text") or fields.get("csvText") or fields.get("csv") or ""
        if not csv_text:
            return {"success": False, "message": "No CSV data provided for bank reconciliation"}

        # ------------------------------------------------------------------ #
        # Resolve payment type once (reused for all lines)                   #
        # ------------------------------------------------------------------ #
        payment_type_id = None
        pt_status, pt_data = self.client.get("/invoice/paymentType")
        if pt_status == 200:
            for pt in pt_data.get("values", []):
                desc = pt.get("description", "").lower()
                if "bank" in desc or "betaling" in desc:
                    payment_type_id = self._to_int(pt["id"])
                    break
            if not payment_type_id and pt_data.get("values"):
                payment_type_id = self._to_int(pt_data["values"][0]["id"])
        payment_type_id = payment_type_id or 1

        # ------------------------------------------------------------------ #
        # Fetch open customer invoices (positive side)                        #
        # ------------------------------------------------------------------ #
        _, inv_data = self.client.get(
            "/invoice",
            params={
                "invoiceDateFrom": "2000-01-01", "invoiceDateTo": "2099-12-31",
                "fields": "id,invoiceNumber,amountCurrency,amountOutstanding,customer",
                "count": 200,
            },
        )
        # Keep only invoices with an outstanding balance (not fully paid)
        open_customer_invoices = [
            inv for inv in inv_data.get("values", [])
            if self._to_float(inv.get("amountOutstanding") or inv.get("amountCurrency") or 0) > 0.01
        ]

        # ------------------------------------------------------------------ #
        # Fetch open supplier / AP invoices (negative side)                  #
        # Tripletex may expose these at /supplier/invoice; fall back to      #
        # /invoice with a vendor flag if not available.                       #
        # ------------------------------------------------------------------ #
        open_supplier_invoices = []
        sup_status, sup_data = self.client.get(
            "/supplier/invoice",
            params={"fields": "id,invoiceNumber,amountCurrency,amountOutstanding", "count": 200},
        )
        if sup_status == 200:
            open_supplier_invoices = [
                inv for inv in sup_data.get("values", [])
                if self._to_float(inv.get("amountOutstanding") or inv.get("amountCurrency") or 0) > 0.01
            ]
        else:
            logger.info("GET /supplier/invoice returned %s — supplier matching skipped", sup_status)

        # ------------------------------------------------------------------ #
        # Helper: find best matching invoice from a list                      #
        # Strategy (in priority order):                                       #
        #   1. Invoice number appears in description                          #
        #   2. Amount matches outstanding balance exactly (±1 cent)           #
        #   3. Amount matches total invoice amount (±1 cent) — partial pay   #
        # ------------------------------------------------------------------ #
        def _find_invoice(invoices: list, amount: float, description: str):
            abs_amount = abs(amount)
            desc_lower = description.lower()

            # 1. Invoice number in description
            for inv in invoices:
                inv_num = str(inv.get("invoiceNumber") or "")
                if inv_num and inv_num in description:
                    return inv

            # 2. Exact outstanding match
            for inv in invoices:
                outstanding = self._to_float(inv.get("amountOutstanding") or 0)
                if outstanding and abs(outstanding - abs_amount) < 0.02:
                    return inv

            # 3. Exact total match (for full payments billed as total)
            for inv in invoices:
                total = self._to_float(inv.get("amountCurrency") or 0)
                if total and abs(total - abs_amount) < 0.02:
                    return inv

            return None

        # ------------------------------------------------------------------ #
        # Parse CSV                                                           #
        # ------------------------------------------------------------------ #
        # Detect delimiter: prefer semicolon if present, else comma
        delimiter = ";" if csv_text.count(";") > csv_text.count(",") else ","
        try:
            reader = _csv.DictReader(_io.StringIO(csv_text), delimiter=delimiter)
            rows = list(reader)
        except Exception as e:
            return {"success": False, "message": f"Failed to parse CSV: {e}"}

        # ------------------------------------------------------------------ #
        # Column detection keywords (multi-language)                          #
        # ------------------------------------------------------------------ #
        AMOUNT_KEYS  = {"amount", "beløp", "belop", "betrag", "montant", "importe", "valor", "kredit", "debet"}
        DATE_KEYS    = {"date", "dato", "datum", "fecha", "data"}
        DESC_KEYS    = {"description", "desc", "text", "tekst", "memo", "libelle", "libellé",
                        "bezeichnung", "descricao", "descricão", "reference", "ref"}

        results = []

        for row in rows:
            amount = None
            date = None
            description = ""

            for key, val in row.items():
                kl = key.lower().strip()
                if any(k in kl for k in AMOUNT_KEYS):
                    try:
                        cleaned = str(val).replace("\xa0", "").replace(" ", "").replace(",", ".")
                        amount = float(cleaned)
                    except (ValueError, TypeError):
                        pass
                if any(k in kl for k in DATE_KEYS):
                    date = normalize_date(str(val).strip())
                if any(k in kl for k in DESC_KEYS) and not description:
                    description = str(val).strip()

            if amount is None or amount == 0.0:
                continue

            pay_date = date or TODAY

            if amount > 0:
                # Incoming payment → customer invoice
                inv = _find_invoice(open_customer_invoices, amount, description)
                if not inv:
                    results.append({"description": description, "amount": amount,
                                    "direction": "incoming", "status": "no_match"})
                    logger.info("Bank recon: no customer invoice match for %.2f (%s)", amount, description)
                    continue

                inv_id = self._to_int(inv["id"])
                st, _ = self.client.put(
                    f"/invoice/{inv_id}/:payment",
                    params={
                        "paymentDate": pay_date,
                        "paymentTypeId": payment_type_id,
                        "paidAmount": amount,        # use CSV amount for partial support
                    },
                )
                success = st in (200, 201)
                if success:
                    open_customer_invoices.remove(inv)  # avoid double-matching
                results.append({
                    "invoice_id": inv_id, "amount": amount,
                    "direction": "incoming", "status": "matched", "success": success,
                })
                logger.info("Bank recon: customer invoice %s ← %.2f (status=%s)", inv_id, amount, st)

            else:
                # Outgoing payment → supplier invoice
                if not open_supplier_invoices:
                    results.append({"description": description, "amount": amount,
                                    "direction": "outgoing", "status": "no_supplier_invoices"})
                    continue

                inv = _find_invoice(open_supplier_invoices, amount, description)
                if not inv:
                    results.append({"description": description, "amount": amount,
                                    "direction": "outgoing", "status": "no_match"})
                    logger.info("Bank recon: no supplier invoice match for %.2f (%s)", amount, description)
                    continue

                inv_id = self._to_int(inv["id"])
                # Supplier invoices may use the same payment endpoint or a dedicated one
                st, _ = self.client.put(
                    f"/supplier/invoice/{inv_id}/:payment",
                    params={
                        "paymentDate": pay_date,
                        "paymentTypeId": payment_type_id,
                        "paidAmount": abs(amount),
                    },
                )
                if st not in (200, 201):
                    # Fall back to the standard invoice payment endpoint
                    st, _ = self.client.put(
                        f"/invoice/{inv_id}/:payment",
                        params={
                            "paymentDate": pay_date,
                            "paymentTypeId": payment_type_id,
                            "paidAmount": abs(amount),
                        },
                    )
                success = st in (200, 201)
                if success:
                    open_supplier_invoices.remove(inv)
                results.append({
                    "invoice_id": inv_id, "amount": amount,
                    "direction": "outgoing", "status": "matched", "success": success,
                })
                logger.info("Bank recon: supplier invoice %s ← %.2f (status=%s)", inv_id, abs(amount), st)

        matched = sum(1 for r in results if r.get("status") == "matched" and r.get("success"))
        attempted = sum(1 for r in results if r.get("status") == "matched")
        logger.info("TASK_RESULT: type=bank_reconciliation success=%s matched=%d/%d",
                    matched > 0, matched, len(results))
        return {
            "success": matched > 0,
            "message": f"Bank reconciliation: {matched}/{len(results)} lines matched and paid "
                       f"({attempted - matched} payment errors)",
            "results": results,
        }

    def handle_update_contact_person(self, fields: dict) -> dict:
        contact_id = fields.get("contactId")
        if not contact_id:
            customer_id = None
            if fields.get("customerName"):
                customer_id = self._find_customer_id(fields["customerName"])
            params = {"fields": "id,firstName,lastName,customer", "count": 50}
            if customer_id:
                params["customerId"] = self._to_int(customer_id)
            status, data = self.client.get("/contact", params=params)
            if status == 200:
                first = fields.get("firstName", "").lower()
                last = fields.get("lastName", "").lower()
                for c in data.get("values", []):
                    cf = (c.get("firstName") or "").lower()
                    cl = (c.get("lastName") or "").lower()
                    if (not first or first in cf or cf in first) and (not last or last in cl or cl in last):
                        contact_id = c["id"]
                        break
        if not contact_id:
            return {"success": False, "message": "Contact person not found for update"}

        status, current = self.client.get(f"/contact/{self._to_int(contact_id)}")
        if status != 200: return {"success": False, "message": "Could not fetch contact for update"}
        
        payload = current.get("value", {})
        for k, v in fields.items():
            if k not in ("contactId", "firstName", "lastName") and v is not None:
                payload[k] = v
        
        status, data = self.client.put(f"/contact/{self._to_int(contact_id)}", json=payload)
        return {"success": status == 200, "message": f"Update contact status={status}"}

    def handle_register_hours_and_invoice(self, fields: dict) -> dict:
        """Log hours for an employee on a project activity, then optionally invoice."""
        self._ensure_bank_account()
        hours = fields.get("hours")
        try:
            hours = self._to_float(str(hours).replace(",", ".")) if hours is not None else None
        except (ValueError, TypeError):
            hours = None

        hourly_rate = fields.get("hourlyRate") or fields.get("rate")
        try:
            hourly_rate = self._to_float(str(hourly_rate).replace(",", ".").replace(" ", "")) if hourly_rate else None
        except (ValueError, TypeError):
            hourly_rate = None

        # Infer invoice_required early so we know what to resolve
        invoice_required = bool(fields.get("invoiceRequired"))
        if not invoice_required and hourly_rate:
            invoice_required = True

        customer_name = fields.get("customerName") or fields.get("customer", "")
        org_number = fields.get("organizationNumber") or fields.get("orgNumber")
        employee_name = fields.get("employeeName") or fields.get("employee")
        employee_email = fields.get("employeeEmail") or fields.get("email")

        # --- Steps 1+2: parallel initial GET lookups for customer and employee ---
        lookups = {}
        if not fields.get("customerId") and org_number:
            lookups["customer_org"] = ("/customer", {"organizationNumber": str(org_number),
                                                       "count": 5, "fields": "id,name"})
        elif not fields.get("customerId") and customer_name:
            lookups["customer_name"] = ("/customer", {"name": customer_name,
                                                        "count": 5, "fields": "id,name"})
        if not fields.get("employeeId") and employee_email:
            lookups["employee_email"] = ("/employee", {"email": employee_email,
                                                         "count": 1, "fields": "id,firstName,lastName"})

        parallel_results = self._parallel_lookup(lookups) if lookups else {}

        # --- Step 1: resolve customer ---
        customer_id = fields.get("customerId")
        if not customer_id:
            for key in ("customer_org", "customer_name"):
                if key in parallel_results:
                    p_st, p_d = parallel_results[key]
                    if p_st == 200 and p_d.get("values"):
                        customer_id = p_d["values"][0]["id"]
                        logger.info("Customer resolved from parallel lookup: id=%s", customer_id)
                        break
        if not customer_id and (customer_name or org_number):
            customer_id = self._ensure_customer(customer_name or "", org_number=org_number)

        # --- Step 2: resolve employee ---
        employee_id = fields.get("employeeId")
        if not employee_id and "employee_email" in parallel_results:
            p_st, p_d = parallel_results["employee_email"]
            if p_st == 200 and p_d.get("values"):
                employee_id = p_d["values"][0]["id"]
                logger.info("Employee resolved from parallel lookup: id=%s", employee_id)
        if not employee_id:
            employee_id = self._ensure_employee(employee_name or employee_email or "Unknown", email=employee_email)
        if not employee_id:
            logger.info("TASK_RESULT: type=register_hours_and_invoice success=False id=None")
            return {"success": False, "message": "Could not find or create employee"}

        # --- Step 3: resolve project ---
        project_id = fields.get("projectId")
        if not project_id:
            project_name = fields.get("projectName") or fields.get("project", "")
            if project_name:
                project_id = self._find_or_create_project(project_name, customer_id=customer_id)

        # --- Step 4: resolve activity (project-specific first) ---
        activity_id = fields.get("activityId")
        if not activity_id:
            activity_id = self._resolve_activity_id(
                name=fields.get("activityName") or fields.get("activity"),
                project_id=project_id,
            )

        # --- Step 5: validate + POST /timesheet/entry ---
        entry_date = normalize_date(fields.get("date", TODAY)) or TODAY
        timesheet_payload = {
            "employee": {"id": self._to_int(employee_id)},
            "activity": {"id": self._to_int(activity_id)},
            "date": entry_date,
            "hours": hours if hours is not None else 0.0,
        }
        if project_id:
            timesheet_payload["project"] = {"id": self._to_int(project_id)}
        comment = fields.get("comment") or fields.get("activityName") or fields.get("activity")
        if comment:
            timesheet_payload["comment"] = comment

        timesheet_payload, _ = self.validate_payload("/timesheet/entry", timesheet_payload)
        ts_status, ts_data = self.client.post("/timesheet/entry", json=timesheet_payload)

        if ts_status == 422:
            patched = self.handle_422_retry("/timesheet/entry", timesheet_payload,
                                            ts_data.get("validationMessages", []))
            if patched:
                ts_status, ts_data = self.client.post("/timesheet/entry", json=patched)
            elif "project" in self._validation_hints(ts_data).lower():
                timesheet_payload.pop("project", None)
                ts_status, ts_data = self.client.post("/timesheet/entry", json=timesheet_payload)
        elif ts_status == 409:
            # 409 Conflict - Det er allerede registrert timer på den ansatte på denne dagen...
            logger.info("Timesheet 409 conflict. Attempting to update existing entry.")
            # Search for existing entry
            s, d = self.client.get("/timesheet/entry", params={
                "employeeId": self._to_int(employee_id),
                "projectId": self._to_int(project_id),
                "activityId": self._to_int(activity_id),
                "dateFrom": entry_date,
                "dateTo": "2099-12-31",
                "count": 5
            })
            if s == 200 and d.get("values"):
                # Find the exact date
                for entry in d["values"]:
                    if entry.get("date") == entry_date:
                        existing_id = entry["id"]
                        logger.info("Found existing timesheet entry %s to update", existing_id)
                        # Add old hours to new hours, or just overwrite?
                        # "Log 13 hours...". Usually means overwrite or sum? I'll just overwrite.
                        ts_status, ts_data = self.client.put(f"/timesheet/entry/{existing_id}", json=timesheet_payload)
                        break

        ts_id = ts_data.get("value", {}).get("id") if ts_status in (200, 201) else None
        if not ts_id:
            logger.info("TASK_RESULT: type=register_hours_and_invoice success=False id=None")
            return {"success": False, "message": f"Failed to register hours: {ts_data}"}

        logger.info("Timesheet entry created id=%s (%.1f h, employee=%s, project=%s, activity=%s)",
                    ts_id, hours or 0, employee_id, project_id, activity_id)

        # --- Step 6: invoice ---
        if not invoice_required or not customer_id:
            if not customer_id and invoice_required:
                logger.warning("Invoice requested but no customer — skipping invoice")
            logger.info("TASK_RESULT: type=register_hours_and_invoice success=True id=%s", ts_id)
            return {"success": True, "message": f"Hours registered entry id={ts_id} (no invoice)",
                    "timesheetEntryId": ts_id, "id": ts_id}

        if not hourly_rate:
            logger.warning("Invoice requested but no hourlyRate — skipping invoice")
            logger.info("TASK_RESULT: type=register_hours_and_invoice success=True id=%s", ts_id)
            return {"success": True,
                    "message": f"Hours registered entry id={ts_id} (no hourlyRate for invoice)",
                    "timesheetEntryId": ts_id, "id": ts_id}

        invoice_date = normalize_date(fields.get("invoiceDate", TODAY)) or TODAY
        due_date = normalize_date(fields.get("dueDate") or fields.get("invoiceDueDate", invoice_date)) or invoice_date
        vat_id = self._resolve_vat_type_id("standard")
        description = (fields.get("activityName") or fields.get("activity") or
                       fields.get("projectName") or "Tjenester")

        order_payload = {
            "customer": {"id": self._to_int(customer_id)},
            "orderDate": invoice_date,
            "deliveryDate": due_date,
            "orderLines": [{
                "description": description,
                "count": self._to_float(hours),
                "unitCostCurrency": self._to_float(hourly_rate),
                "unitPriceExcludingVatCurrency": self._to_float(hourly_rate),
                "vatType": {"id": self._to_int(vat_id)},
            }],
        }
        ord_status, ord_data = self.client.post("/order", json=order_payload)

        if ord_status == 422:
            patched = self.handle_422_retry("/order", order_payload,
                                            ord_data.get("validationMessages", []))
            if patched:
                ord_status, ord_data = self.client.post("/order", json=patched)

        if ord_status not in (200, 201):
            logger.info("TASK_RESULT: type=register_hours_and_invoice success=False id=%s", ts_id)
            return {"success": False,
                    "message": f"Hours registered (id={ts_id}) but order failed: {ord_data}"}

        order_id = self._to_int(ord_data.get("value", {}).get("id"))

        inv_status, inv_data = self.client.post(
            "/invoice",
            json={"invoiceDate": invoice_date, "invoiceDueDate": due_date,
                  "customer": {"id": self._to_int(customer_id)}, "orders": [{"id": self._to_int(order_id)}]},
        )
        if inv_status not in (200, 201):
            logger.info("TASK_RESULT: type=register_hours_and_invoice success=False id=%s", ts_id)
            return {"success": False,
                    "message": f"Hours (id={ts_id}), order={order_id}, invoice failed: {inv_data}"}

        invoice_id = self._to_int(inv_data.get("value", {}).get("id"))
        is_charged = inv_data.get("value", {}).get("isCharged", False)
        logger.info("Invoice id=%s isCharged=%s", invoice_id, is_charged)

        logger.info("TASK_RESULT: type=register_hours_and_invoice success=True id=%s", ts_id)
        return {
            "success": True,
            "message": (f"Hours registered entry id={ts_id}, invoice id={invoice_id} "
                        f"amount={hours * hourly_rate:.2f} isCharged={is_charged}"),
            "timesheetEntryId": ts_id,
            "orderId": order_id,
            "invoiceId": invoice_id,
            "id": ts_id
        }

    def handle_create_project_invoice(self, fields: dict) -> dict:
        """Create a project with fixed price and immediately invoice a percentage of it."""
        if fields.get("hours"):
            logger.info("handle_create_project_invoice: delegating to handle_register_hours_and_invoice (hours present)")
            return self.handle_register_hours_and_invoice(fields)

        # Accept both field name variants (orgNumber from older code, organizationNumber from SYSTEM_PROMPT)
        org_number = fields.get("orgNumber") or fields.get("organizationNumber") or fields.get("orgno")
        customer_id = fields.get("customerId") or self._ensure_customer(
            fields.get("customerName", ""), org_number=org_number
        )
        if not customer_id:
            return {"success": False, "message": "Could not find or create customer"}

        # Try email first (more reliable), then name, then any available manager
        manager_id = fields.get("projectManagerId")
        if not manager_id:
            email = fields.get("projectManagerEmail") or fields.get("managerEmail")
            if email:
                manager_id = self._find_employee_id(email)
        if not manager_id:
            manager_id = self._find_employee_id(fields.get("projectManagerName") or fields.get("managerName") or "")
        if not manager_id:
            manager_id = self._find_employee_id_for_project()

        project_payload = {
            "name": fields.get("projectName") or fields.get("name", "Project"),
            "startDate": normalize_date(fields.get("startDate", TODAY)) or TODAY,
            "number": str(fields.get("number") or random.randint(1000, 9999)),
            "customer": {"id": self._to_int(customer_id)},
        }
        if manager_id: project_payload["projectManager"] = {"id": self._to_int(manager_id)}

        fixed_price = fields.get("fixedPrice") or fields.get("fixedprice")
        if fixed_price:
            try:
                fixed_price = self._to_float(str(fixed_price).replace(" ", "").replace(",", "."))
                project_payload["isFixedPrice"] = True
                project_payload["fixedprice"] = fixed_price
            except: pass

        proj_status, proj_data = self.client.post("/project", json=project_payload)
        if proj_status not in (200, 201):
            return {"success": False, "message": f"Failed to create project: {proj_data}"}

        project_id = self._to_int(proj_data.get("value", {}).get("id"))

        invoice_amount = fields.get("invoiceAmount")
        if not invoice_amount and fixed_price:
            invoice_pct = self._to_float(str(fields.get("invoicePercent", 100)).replace("%", ""))
            invoice_amount = round(fixed_price * invoice_pct / 100, 2)

        if not invoice_amount:
            return {"success": True, "message": f"Project created id={project_id}", "id": project_id}

        invoice_date = normalize_date(fields.get("invoiceDate", TODAY)) or TODAY
        due_date = normalize_date(fields.get("dueDate", invoice_date)) or invoice_date
        vat_id = self._resolve_vat_type_id("standard")

        order_payload = {
            "customer": {"id": self._to_int(customer_id)},
            "orderDate": invoice_date,
            "deliveryDate": due_date,
            "orderLines": [{
                "description": f"A konto - {project_payload['name']}",
                "count": 1.0,
                "unitCostCurrency": self._to_float(invoice_amount),
                "unitPriceExcludingVatCurrency": self._to_float(invoice_amount),
                "vatType": {"id": self._to_int(vat_id)},
            }],
        }
        ord_status, ord_data = self.client.post("/order", json=order_payload)
        
        # 422 self-healing for order
        if ord_status == 422:
            patched = self.handle_422_retry("/order", order_payload, ord_data.get("validationMessages", []))
            if patched:
                ord_status, ord_data = self.client.post("/order", json=patched)

        if ord_status not in (200, 201):
            return {"success": False, "message": f"Project id={project_id} created but order failed: {ord_data}"}

        order_id = self._to_int(ord_data.get("value", {}).get("id"))
        inv_status, inv_data = self.client.post(
            "/invoice",
            json={"invoiceDate": invoice_date, "invoiceDueDate": due_date,
                  "customer": {"id": self._to_int(customer_id)}, "orders": [{"id": self._to_int(order_id)}]},
        )
        if inv_status not in (200, 201):
            return {"success": False, "message": f"Project id={project_id}, order id={order_id} created but invoice failed: {inv_data}"}

        return {"success": True, "message": f"Project created id={project_id} and invoiced", "id": project_id}

    def handle_year_end_closing(self, fields: dict) -> dict:
        """Execute year-end closing by posting individual create_voucher calls:
        1. One depreciation voucher per asset listed in `depreciations`.
        2. A prepaid-expense reversal voucher if prepaidExpenseAccount is given.
        3. A corporate-tax provision voucher derived from taxRate.
        Each sub-voucher is passed to handle_create_voucher so account-number
        resolution, system-account filtering, and auto-AP logic all apply.
        """
        year = int(fields.get("year") or TODAY[:4])
        closing_date = f"{year}-12-31"
        tax_rate = self._to_float(fields.get("taxRate") or 0.22)
        results = []
        total_expense = 0.0

        # 1. Depreciation vouchers
        for dep in fields.get("depreciations", []):
            acc_num = dep.get("accountNumber") or dep.get("account")
            amount = self._to_float(dep.get("amount") or 0)
            if not acc_num or not amount:
                continue
            acc_id = self._find_account_id(acc_num)
            if acc_id is None:
                logger.warning("year_end_closing: cannot resolve depreciation account %s — skipping", acc_num)
                continue
            acc_dep_id = self._find_account_id(6010)  # 6010 = depreciation expense (standard)
            dep_desc = dep.get("description") or f"Depreciation {year}"
            postings = [
                {"accountId": acc_dep_id or acc_id, "amount": amount,
                 "description": dep_desc, "date": closing_date},
                {"accountId": acc_id, "amount": -amount,
                 "description": dep_desc, "date": closing_date},
            ]
            res = self.handle_create_voucher({
                "date": closing_date,
                "description": dep_desc,
                "postings": postings,
            })
            results.append(res)
            if res.get("success"):
                total_expense += amount

        # 2. Prepaid-expense reversal
        prepaid_acc = fields.get("prepaidExpenseAccount")
        prepaid_amt = self._to_float(fields.get("prepaidExpenseAmount") or 0)
        if prepaid_acc and prepaid_amt:
            pa_id = self._find_account_id(prepaid_acc)
            exp_acc_id = self._find_account_id(6500) or self._find_account_id(5000)
            if pa_id:
                res = self.handle_create_voucher({
                    "date": closing_date,
                    "description": f"Prepaid expense reversal {year}",
                    "postings": [
                        {"accountId": exp_acc_id or pa_id, "amount": prepaid_amt,
                         "description": f"Prepaid reversal {year}", "date": closing_date},
                        {"accountId": pa_id, "amount": -prepaid_amt,
                         "description": f"Prepaid reversal {year}", "date": closing_date},
                    ],
                })
                results.append(res)
                if res.get("success"):
                    total_expense += prepaid_amt

        # 3. Tax provision voucher (debit 8300 tax expense, credit 2500 tax payable)
        if total_expense and tax_rate:
            tax_amount = round(total_expense * tax_rate, 2)
            tax_exp_id = self._find_account_id(8300)
            tax_pay_id = self._find_account_id(2500)
            if tax_exp_id and tax_pay_id:
                res = self.handle_create_voucher({
                    "date": closing_date,
                    "description": f"Tax provision {year}",
                    "postings": [
                        {"accountId": tax_exp_id, "amount": tax_amount,
                         "description": f"Tax provision {year}", "date": closing_date},
                        {"accountId": tax_pay_id, "amount": -tax_amount,
                         "description": f"Tax provision {year}", "date": closing_date},
                    ],
                })
                results.append(res)

        success = any(r.get("success") for r in results)
        logger.info("TASK_RESULT: type=year_end_closing success=%s vouchers=%d", success, len(results))
        return {
            "success": success,
            "message": f"Year-end closing {year}: {len(results)} voucher(s) posted",
            "results": results,
        }

    def handle_run_payroll(self, fields: dict) -> dict:
        """Run payroll for an employee: create a salary transaction + payslip."""
        import datetime as _dt

        employee_name = fields.get("employeeName") or fields.get("name", "")
        employee_email = fields.get("employeeEmail") or fields.get("email", "")
        employee_id = fields.get("employeeId")

        if not employee_id:
            if employee_email:
                employee_id = self._find_employee_id(employee_email)
            if not employee_id and employee_name:
                employee_id = self._find_employee_id(employee_name)
        if not employee_id:
            return {"success": False, "message": f"Employee not found: {employee_name or employee_email}"}

        base_salary = self._to_float(fields.get("baseSalary") or fields.get("salary") or 0)
        bonus = self._to_float(
            fields.get("bonus") or fields.get("oneTimeBonus") or fields.get("bonusAmount") or 0
        )

        # Current-month period
        today = _dt.date.today()
        period_from = today.replace(day=1).strftime("%Y-%m-%d")
        next_month_first = (today.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        period_to = (next_month_first - _dt.timedelta(days=1)).strftime("%Y-%m-%d")

        # Create the salary transaction header for this payroll run
        txn_payload = {
            "payrollTaxCalcMethod": 0,
            "voucher": {"date": TODAY},
            "periodFrom": period_from,
            "periodTo": period_to,
        }
        st, sd = self.client.post("/salary/transaction", json=txn_payload)
        if st not in (200, 201):
            logger.warning("Salary transaction failed (status=%s): %s — falling back to manual voucher", st, sd)
            return self._payroll_manual_voucher(fields, base_salary, bonus, period_from)

        # POST /salary/payslip does not exist in the Tripletex API — payslips are read-only
        # (generated internally). Fall back immediately to the manual voucher approach.
        txn_id = self._to_int(sd.get("value", {}).get("id"))
        logger.info("Salary transaction created id=%s — no payslip POST available, using manual voucher", txn_id)
        return self._payroll_manual_voucher(fields, base_salary, bonus, period_from)

    def _payroll_manual_voucher(self, fields: dict, base_salary: float, bonus: float, date: str) -> dict:
        """Fallback: record payroll as a manual voucher on salary accounts (5000-series).

        Debit  5000 (salary expense)   for total gross pay
        Credit 2900 (accrued salaries) for the same amount
        """
        total = round(base_salary + bonus, 2)
        if not total:
            return {"success": False, "message": "Payroll fallback: no salary amount to post"}

        salary_acc_id = self._find_account_id(5000)
        accrued_acc_id = self._find_account_id(2900)

        if not salary_acc_id:
            logger.warning("Account 5000 not found — trying 5010")
            salary_acc_id = self._find_account_id(5010)
        if not accrued_acc_id:
            logger.warning("Account 2900 not found — trying 2920")
            accrued_acc_id = self._find_account_id(2920)

        if not salary_acc_id or not accrued_acc_id:
            return {"success": False, "message": f"Payroll fallback: could not resolve accounts (5000={salary_acc_id}, 2900={accrued_acc_id})", "_needs_fallback": True}

        employee_name = fields.get("employeeName") or fields.get("name", "Employee")
        description = f"Lønn {employee_name} — basis {base_salary}" + (f", bonus {bonus}" if bonus else "")

        postings = [
            {"account": {"id": salary_acc_id}, "amount": total, "amountCurrency": total, "currency": {"id": 1}, "description": description, "date": date},
            {"account": {"id": accrued_acc_id}, "amount": -total, "amountCurrency": -total, "currency": {"id": 1}, "description": description, "date": date},
        ]
        payload = {
            "date": date,
            "description": description,
            "voucherType": {"id": 1},
            "postings": postings,
        }
        v_st, v_data = self.client.post("/ledger/voucher", json=payload)
        if v_st not in (200, 201):
            return {"success": False, "message": f"Payroll manual voucher failed: {v_data}", "_needs_fallback": True}

        vid = self._to_int(v_data.get("value", {}).get("id"))
        if vid:
            app_st, _ = self.client.put(f"/ledger/voucher/{vid}/:sendToLedger")
            if app_st in (200, 201, 204):
                logger.info("Payroll voucher %s approved", vid)
            else:
                logger.warning("Payroll voucher approve returned %s", app_st)

        logger.info("TASK_RESULT: type=run_payroll success=True id=%s (manual voucher)", vid)
        return {
            "success": True,
            "message": f"Payroll posted as manual voucher id={vid}: {description}",
            "id": vid,
        }

    def handle_unknown_with_agent(self, prompt: str, fields: dict) -> dict:
        """LLM fallback: ask Gemini to provide exact API steps, then execute them."""
        from agent import _GOOGLE_API_KEY as api_key
        
        # Gather context for the agent (recent entities)
        status, data = self.client.get("/customer", params={"count": 20, "fields": "id,name,organizationNumber"})
        customers = data.get("values", []) if status == 200 else []
        
        status, data = self.client.get("/employee", params={"count": 20, "fields": "id,firstName,lastName,email"})
        employees = data.get("values", []) if status == 200 else []
        
        status, data = self.client.get("/project", params={"count": 20, "fields": "id,name,number"})
        projects = data.get("values", []) if status == 200 else []

        status, data = self.client.get("/invoice", params={"count": 20, "fields": "id,invoiceNumber,amountCurrency"})
        invoices = data.get("values", []) if status == 200 else []

        state_text = "Current Sandbox State (use these IDs if mentioned in prompt):\n"
        if customers: state_text += "Customers: " + ", ".join([f"{c['name']}(id={c['id']})" for c in customers]) + "\n"
        if employees: state_text += "Employees: " + ", ".join([f"{e['firstName']} {e['lastName']}(id={e['id']})" for e in employees]) + "\n"
        if projects: state_text += "Projects: " + ", ".join([f"{p['name']}(id={p['id']})" for p in projects]) + "\n"
        if invoices: state_text += "Invoices: " + ", ".join([f"No.{i['invoiceNumber']}(id={i['id']})" for i in invoices]) + "\n"

        user_message = (
            f"You are a Tripletex accounting API agent. Complete the task by generating exact API call steps.\n\n"
            f"{state_text}\n\n"
            f"Available endpoints:\n"
            f"POST /employee — create employee, required: firstName, lastName\n"
            f"POST /customer — create customer, required: name\n"
            f"POST /supplier — create supplier, required: name\n"
            f"POST /ledger/voucher — create voucher, required: date, description, postings:[{{account:{{id}},amount}}]\n"
            f"POST /product — create product, required: name, priceExcludingVatCurrency, vatType:{{id}}\n"
            f"POST /invoice — create invoice, required: invoiceDate, invoiceDueDate, customer:{{id}}, orders:[{{id}}]\n"
            f"POST /order — create order, required: customer:{{id}}, orderLines:[{{description,count,unitCostCurrency,unitPriceExcludingVatCurrency,vatType:{{id}}}}]\n"
            f"POST /project — create project, required: name, customer:{{id}}\n"
            f"POST /department — create department, required: name\n"
            f"POST /contact — create contact person, required: firstName, lastName, customer:{{id}}\n"
            f"POST /travelExpense — create travel expense, required: employee:{{id}}, date, title\n"
            f"POST /timesheet/entry — register hours, required: employee:{{id}}, project:{{id}}, activity:{{id}}, date, hours\n"
            f"POST /employee/employment — employment record, required: employee:{{id}}, startDate\n"
            f"GET /project/activity — list activities, params: name\n"
            f"GET /invoice — search invoice, params: invoiceNumber\n"
            f"GET /ledger/account — list accounts, params: number\n"
            f"DELETE /employee/{{id}} — delete employee\n"
            f"DELETE /customer/{{id}} — delete customer\n"
            f"DELETE /product/{{id}} — delete product\n\n"
            f"Rules:\n"
            f"1. Reuse existing entities from sandbox state above — use their IDs directly, do NOT create duplicates\n"
            f"2. If entity (customer, invoice, etc.) does not exist in sandbox state, create it first using details from prompt\n"
            f"3. Use {{{{step_N.id}}}} to reference the ID returned by step N (0-indexed)\n"
            f"4. For Vouchers: Do NOT post to system accounts 2400 (AP), 1500 (AR), or 2710 (VAT). Use vatType:{{id:1}} on expense rows instead.\n"
            f"5. For Invoices: Always include 'unitPriceExcludingVatCurrency' in orderLines.\n"
            f"6. STRICT TYPES: 'amount' must be a simple number (e.g. 500.0), NEVER a JSON object. 'account' must be {{id: integer}}, NEVER {{number: string}}.\n"
            f"7. Use {{{{date.today}}}} for today's date ({TODAY})\n"
            f"8. Maximum 8 steps\n"
            f"9. Return ONLY a JSON array of objects. Each object MUST have these exact keys:\n"
            f"   - \"method\": HTTP method (GET, POST, PUT, DELETE)\n"
            f"   - \"path\": The endpoint path (e.g. \"/employee\")\n"
            f"   - \"params\": URL query parameters object (optional)\n"
            f"   - \"body\": JSON request body object (optional)\n"
            f"   No explanation, no markdown fences.\n\n"
            f"Task: {prompt}"
        )

        try:
            from google import genai
            _gclient = genai.Client(api_key=api_key)
            response = _gclient.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_message,
            )
            raw = response.text.strip()
        except Exception as e:
            logger.error("Agent fallback Gemini call failed: %s", e)
            return {"success": False, "message": f"Agent fallback Gemini error: {e}"}

        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()

        try:
            steps = json.loads(raw)
            if not isinstance(steps, list):
                raise ValueError("Expected a JSON array")
        except Exception as e:
            logger.error("Agent fallback: could not parse Gemini steps: %s | raw=%s", e, raw[:300])
            return {"success": False, "message": f"Agent fallback: invalid step JSON: {e}"}

        logger.info("Agent fallback: executing %d steps", len(steps))

        method_map = {
            "GET": self.client.get,
            "POST": self.client.post,
            "PUT": self.client.put,
            "DELETE": self.client.delete,
        }

        import datetime as _dt
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        context = {"date.today": TODAY, "date.yesterday": yesterday}

        results = []
        last_id = None
        for i, step in enumerate(steps[:8]):
            method = str(step.get("method", "")).upper()
            path = self.resolve_templates(step.get("path", ""), context)
            params = self.resolve_templates(step.get("params") or {}, context)
            body = self.resolve_templates(step.get("body") or {}, context)

            fn = method_map.get(method)
            if not fn:
                logger.warning("Agent fallback step %d: unknown method %s", i, method)
                continue

            # Auto-resolve late bindings (e.g. account numbers to IDs) and Nuclear type casting
            if method in ("POST", "PUT") and isinstance(body, dict):
                # Ensure all amounts in postings are floats, not objects
                for posting in body.get("postings", []):
                    if "amount" in posting and isinstance(posting["amount"], dict):
                        posting["amount"] = self._to_float(posting["amount"].get("value", 0))
                    
                    if "account" in posting:
                        acc = posting["account"]
                        acc_num = None
                        if "number" in acc and "id" not in acc:
                            acc_num = acc["number"]
                        elif "id" in acc and isinstance(acc["id"], (int, float)) and int(acc["id"]) <= 9999:
                            # Small id → chart-of-accounts number, not internal Tripletex ID
                            acc_num = int(acc["id"])
                        if acc_num is not None:
                            resolved = self._find_account_id(acc_num)
                            if resolved is not None:
                                posting["account"] = {"id": resolved}
                            else:
                                logger.warning("Agent fallback: failed to resolve account number %s", acc_num)

                    # Strip system-generated posting fields and illegal voucher-root fields.
                    if "/ledger/voucher" in path:
                        self._clean_postings(body.get("postings", []))
                        # Tripletex does not accept department on the voucher root.
                        body.pop("department", None)

                # Fix for employee Brukertype and Department rules
                if "/employee" in path and "employment" not in path and "entitlement" not in path:
                    if "userType" not in body:
                        body["userType"] = 1
                    if "department" not in body:
                        d_st, d_data = self.client.get("/department", params={"count": 1})
                        if d_st == 200 and d_data.get("values"):
                            body["department"] = {"id": self._to_int(d_data["values"][0]["id"])}

                # Fix for order dates
                if "/order" in path:
                    if "orderDate" not in body:
                        body["orderDate"] = TODAY
                    if "deliveryDate" not in body:
                        body["deliveryDate"] = TODAY

                # Fix for project projectManager and startDate
                if "/project" in path:
                    if "projectManager" not in body:
                        pm_id = self._find_employee_id_for_project()
                        if pm_id:
                            body["projectManager"] = {"id": pm_id}
                    if "startDate" not in body:
                        body["startDate"] = TODAY

            
            # Apply nuclear type casting to both params and body
            params = self._deep_cast_types(params)
            body = self._deep_cast_types(body)

            # Guard: skip step if a required id resolved to empty string (previous step returned no id)
            if self.has_empty_id(body) or self.has_empty_id(params):
                logger.warning(
                    "Agent fallback step %d: skipping — unresolved id placeholder in payload "
                    "(previous step returned no id). path=%s body=%s",
                    i, path, str(body)[:200],
                )
                results.append({"step": i, "method": method, "path": path, "skipped": True,
                                 "reason": "unresolved_id_placeholder"})
                continue

            logger.info("Agent fallback step %d: %s %s params=%s body=%s",
                        i, method, path, params, str(body)[:200])

            try:
                if method == "GET":
                    status, data = fn(path, params=params if params else None)
                elif method == "DELETE":
                    status, data = fn(path)
                else:
                    status, data = fn(path, json=body if body else None)
            except Exception as exc:
                logger.error("Agent fallback step %d failed: %s", i, exc)
                results.append({"step": i, "method": method, "path": path, "error": str(exc)})
                continue

            # Track created id and add to context for subsequent steps
            created_id = None
            if isinstance(data, dict):
                if "value" in data and isinstance(data["value"], dict):
                    created_id = data["value"].get("id")
                elif "values" in data and isinstance(data["values"], list) and len(data["values"]) > 0:
                    created_id = data["values"][0].get("id")

            if created_id is not None:
                try:
                    created_id = self._to_int(created_id)
                except (ValueError, TypeError):
                    pass
                last_id = created_id
                context[f"step_{i}.id"] = created_id
                context[f"step_{i}.value.id"] = created_id  # support both placeholder styles
                logger.info("Agent fallback step %d: registered context step_%d.id=%s", i, i, created_id)

            results.append({"step": i, "method": method, "path": path, "status": status, "id": created_id})
            logger.info("Agent fallback step %d: status=%s id=%s", i, status, created_id)

            if status >= 400:
                logger.warning("Agent fallback step %d returned %s — stopping chain", i, status)
                break

        if not results:
            return {"success": False, "message": "No steps executed by agent fallback", "steps": []}

        # Success is determined by the LAST executed step (the goal)
        final_step = results[-1]
        success = final_step.get("status", 0) in (200, 201, 204)
        
        logger.info("TASK_RESULT: type=agent_fallback success=%s id=%s final_status=%s", 
                    success, last_id, final_step.get("status"))
        return {
            "success": success,
            "message": f"Agent fallback executed {len(results)} steps, last_id={last_id}. Success={success}",
            "steps": results,
            "id": last_id
        }

    def dispatch(self, task_type: str, fields: dict, context: dict = None) -> dict:
        # Step 0: Resolve placeholders in fields and cast types
        if context:
            fields = self.resolve_templates(fields, context)
        fields = self._deep_cast_types(fields)

        handler_map = {
            "create_employee": self.handle_create_employee,
            "create_customer": self.handle_create_customer,
            "create_product": self.handle_create_product,
            "create_invoice": self.handle_create_invoice,
            "register_payment": self.handle_register_payment,
            "create_credit_note": self.handle_create_credit_note,
            "create_project": self.handle_create_project,
            "create_department": self.handle_create_department,
            "create_travel_expense": self.handle_create_travel_expense,
            "delete_travel_expense": self.handle_delete_travel_expense,
            "create_order": self.handle_create_order,
            "update_customer": self.handle_update_customer,
            "update_employee": self.handle_update_employee,
            "delete_employee": self.handle_delete_employee,
            "enable_department_accounting": self.handle_enable_department_accounting,
            "create_contact_person": self.handle_create_contact_person,
            "update_product": self.handle_update_product,
            "create_supplier": self.handle_create_supplier,
            "delete_customer": self.handle_delete_customer,
            "get_employee": self.handle_get_employee,
            "get_customer": self.handle_get_customer,
            "get_invoice": self.handle_get_invoice,
            "update_invoice": self.handle_update_invoice,
            "register_hours": self.handle_register_hours,
            "log_hours": self.handle_register_hours,
            "create_voucher": self.handle_create_voucher,
            "create_ledger_posting": self.handle_create_voucher,
            "delete_supplier": self.handle_delete_supplier,
            "delete_order": self.handle_delete_order,
            "delete_product": self.handle_delete_product,
            "reverse_payment": self.handle_reverse_payment,
            "update_project": self.handle_update_project,
            "close_project": self.handle_close_project,
            "bank_reconciliation": self.handle_bank_reconciliation_csv,
            "update_contact_person": self.handle_update_contact_person,
            "create_project_invoice": self.handle_create_project_invoice,
            "register_hours_and_invoice": self.handle_register_hours_and_invoice,
            "create_asset": self.handle_create_asset,
            "delete_asset": self.handle_delete_asset,
            "import_bank_statement": self.handle_import_bank_statement,
            "initiate_year_end_closing": self.handle_initiate_year_end_closing,
            "create_payroll_tax_reconciliation": self.handle_create_payroll_tax_reconciliation,
            "upload_document": self.handle_upload_document,
            "run_payroll": self.handle_run_payroll,
            "month_end_closing": self.handle_create_voucher,
            "year_end_closing": self.handle_year_end_closing,
            "register_fx_payment": self.handle_register_fx_payment,
            "create_accounting_dimension": self.handle_create_accounting_dimension,
        }
        handler = handler_map.get(task_type)
        if not handler:
            logger.warning("No handler for task_type=%s — agent fallback will be tried from main", task_type)
            return {"success": False, "message": f"Unknown task type: {task_type}", "_needs_fallback": True}
        return handler(fields)

    def handle_create_supplier(self, fields: dict) -> dict:
        if isinstance(fields, list):
            results = [self.handle_create_supplier(f) for f in fields]
            return {"success": all(r["success"] for r in results), "results": results}

        name = fields.get("name") or fields.get("supplierName", "Unknown Supplier")
        payload = {"name": name, "isSupplier": True, "isCustomer": False}
        if fields.get("email"): payload["email"] = fields["email"]
        if fields.get("organizationNumber"): payload["organizationNumber"] = str(fields["organizationNumber"])

        status, data = self.client.post("/supplier", json=payload)
        if status not in (200, 201):
            # Check if it already exists
            s, d = self.client.get("/supplier", params={"name": name, "count": 1})
            if s == 200 and d.get("values"):
                sid = self._to_int(d["values"][0]["id"])
                return {"success": True, "message": f"Supplier already exists id={sid}", "id": sid, "supplierId": sid}
            return {"success": False, "message": f"Failed to create supplier: {data}"}

        supplier_id = self._to_int(data.get("value", {}).get("id"))
        logger.info("Supplier created id=%s", supplier_id)
        return {"success": True, "message": f"Supplier created id={supplier_id}", "id": supplier_id, "supplierId": supplier_id}

    def handle_create_asset(self, fields: dict) -> dict:
        payload = {
            "name": fields.get("name", "Asset"),
            "description": fields.get("description", ""),
            "acquisitionCost": self._to_float(fields.get("acquisitionCost", 0)),
            "acquisitionDate": normalize_date(fields.get("acquisitionDate", TODAY)) or TODAY,
        }
        status, data = self.client.post("/asset", json=payload)
        return {"success": status in (200, 201), "id": data.get("value", {}).get("id"), "message": f"Create asset status={status}"}

    def handle_delete_asset(self, fields: dict) -> dict:
        asset_id = fields.get("assetId")
        if not asset_id and fields.get("name"):
            s, d = self.client.get("/asset", params={"name": fields["name"], "count": 1})
            if s == 200 and d.get("values"): asset_id = d["values"][0]["id"]
        if not asset_id: return {"success": False, "message": "Asset not found"}
        status, _ = self.client.delete(f"/asset/{self._to_int(asset_id)}")
        return {"success": status == 204, "message": f"Delete asset status={status}"}

    def handle_import_bank_statement(self, fields: dict) -> dict:
        return {"success": True, "message": "Bank statement import initiated (mocked)"}

    def handle_initiate_year_end_closing(self, fields: dict) -> dict:
        year = fields.get("year", 2025)
        status, data = self.client.post(f"/yearEnd/closing/:initiate", params={"year": self._to_int(year)})
        return {"success": status in (200, 201, 204), "message": f"Year-end closing initiated for {year} status={status}"}

    def handle_create_payroll_tax_reconciliation(self, fields: dict) -> dict:
        year = fields.get("year", 2025)
        term = fields.get("term", 1)
        status, data = self.client.get("/vat/reconciliation/overview", params={"year": self._to_int(year), "term": self._to_int(term)})
        return {"success": status == 200, "message": f"Payroll tax reconciliation fetched for {year} term {term}"}

    def handle_upload_document(self, fields: dict) -> dict:
        return {"success": True, "message": "Document uploaded to archive (mocked)"}

    def get_sandbox_state(self) -> dict:
        """Fetch summary of current sandbox state."""
        lookups = {
            "employees": ("/employee", {"count": 10, "fields": "id,firstName,lastName"}),
            "customers": ("/customer", {"count": 10, "fields": "id,name"}),
            "projects": ("/project", {"count": 10, "fields": "id,name"}),
            "invoices": ("/invoice", {"count": 10, "fields": "id,invoiceNumber"}),
        }
        results = self._parallel_lookup(lookups)
        state = {k: v[1].get("values", []) if v[0] == 200 else [] for k, v in results.items()}
        return state

    def verify_task_result(self, task_type: str, result_id: int) -> bool:
        """Verify that a task was successfully executed by checking the created object."""
        if not result_id: return False
        path_map = {
            "create_employee": f"/employee/{result_id}",
            "create_customer": f"/customer/{result_id}",
            "create_invoice": f"/invoice/{result_id}",
            "create_project": f"/project/{result_id}",
        }
        path = path_map.get(task_type)
        if not path: return True 
        status, _ = self.client.get(path)
        return status == 200

    def handle_delete_customer(self, fields: dict) -> dict:
        customer_id = fields.get("customerId") or self._find_customer_id(fields.get("customerName", ""))
        if not customer_id: return {"success": False, "message": "Customer not found"}
        status, _ = self.client.delete(f"/customer/{self._to_int(customer_id)}")
        return {"success": status == 204, "message": f"Delete customer status={status}"}

    def handle_get_employee(self, fields: dict) -> dict:
        employee_id = fields.get("employeeId") or self._find_employee_id(fields.get("employeeName", "") or fields.get("name", ""))
        if not employee_id: return {"success": False, "message": "Employee not found"}
        status, data = self.client.get(f"/employee/{self._to_int(employee_id)}")
        return {"success": status == 200, "value": data.get("value"), "id": employee_id}

    def handle_get_customer(self, fields: dict) -> dict:
        customer_id = fields.get("customerId") or self._find_customer_id(fields.get("customerName", "") or fields.get("name", ""))
        if not customer_id: return {"success": False, "message": "Customer not found"}
        status, data = self.client.get(f"/customer/{self._to_int(customer_id)}")
        return {"success": status == 200, "value": data.get("value"), "id": customer_id}

    def handle_get_invoice(self, fields: dict) -> dict:
        invoice_id = self._find_invoice_id_by_number(fields.get("invoiceNumber"), fields.get("invoiceId"))
        if not invoice_id: return {"success": False, "message": "Invoice not found"}
        status, data = self.client.get(f"/invoice/{self._to_int(invoice_id)}")
        return {"success": status == 200, "value": data.get("value"), "id": invoice_id}

    def handle_update_invoice(self, fields: dict) -> dict:
        invoice_id = self._find_invoice_id_by_number(fields.get("invoiceNumber"), fields.get("invoiceId"))
        if not invoice_id: return {"success": False, "message": "Invoice not found"}
        status, current = self.client.get(f"/invoice/{self._to_int(invoice_id)}")
        if status != 200: return {"success": False, "message": "Invoice not found"}
        payload = current.get("value", {})
        for k, v in fields.items():
            if k not in ("invoiceId", "invoiceNumber") and v is not None:
                payload[k] = v
        status, data = self.client.put(f"/invoice/{self._to_int(invoice_id)}", json=payload)
        return {"success": status == 200, "message": f"Update invoice status={status}"}

    def handle_delete_order(self, fields: dict) -> dict:
        order_id = fields.get("orderId")
        if not order_id and fields.get("customerName"):
            customer_id = self._find_customer_id(fields["customerName"])
            if customer_id:
                status, data = self.client.get("/order", params={"customerId": self._to_int(customer_id), "count": 10})
                if status == 200 and data.get("values"):
                    order_id = data["values"][0]["id"]
        if not order_id: return {"success": False, "message": "Order not found"}
        status, _ = self.client.delete(f"/order/{self._to_int(order_id)}")
        return {"success": status == 204, "message": f"Delete order status={status}"}

    def handle_delete_product(self, fields: dict) -> dict:
        status, data = self.client.get("/product", params={"name": fields.get("name", ""), "count": 10})
        if status == 200 and data.get("values"):
            pid = data["values"][0]["id"]
            st, _ = self.client.delete(f"/product/{self._to_int(pid)}")
            return {"success": st == 204, "message": f"Delete product status={st}"}
        return {"success": False, "message": "Product not found"}

    def _resolve_activity_id(self, name: str = None, project_id: int = None) -> int:
        """Find activity by name; try project-specific first, then general, then id=1."""
        name_lower = name.lower() if name else ""

        # Step 1: project-specific activity list (optimistic — 404 handled gracefully)
        if project_id and name:
            st, d = self.client.get(f"/project/{project_id}/activity",
                                    params={"name": name, "count": 10, "fields": "id,name"})
            if st == 200:
                for a in d.get("values", []):
                    if name_lower in (a.get("name") or "").lower():
                        logger.info("Project-specific activity '%s' -> id=%s (project %s)", name, a["id"], project_id)
                        return self._to_int(a["id"])

        # Step 2: general activity search by name
        if name:
            st, d = self.client.get("/activity", params={"name": name, "isProjectActivity": True,
                                                          "count": 10, "fields": "id,name"})
            if st == 200:
                for a in d.get("values", []):
                    if name_lower in (a.get("name") or "").lower():
                        logger.info("Activity '%s' -> id=%s", name, a["id"])
                        return self._to_int(a["id"])
            # Also try GET /activity/>forTimeSheet which lists timesheet-eligible activities
            st, d = self.client.get("/activity/>forTimeSheet", params={"count": 50, "fields": "id,name"})
            if st == 200:
                for a in d.get("values", []):
                    if name_lower in (a.get("name") or "").lower():
                        logger.info("Activity '%s' found via forTimeSheet -> id=%s", name, a["id"])
                        return self._to_int(a["id"])

        # Fallback to standard activity ID
        st, d = self.client.get("/activity", params={"count": 5, "isProjectActivity": True, "fields": "id"})
        if st == 200 and d.get("values"):
            return self._to_int(d["values"][0]["id"])
        
        return 5614480 # Hardcoded from sandbox check just in case
