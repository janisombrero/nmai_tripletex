# NM i AI 2026 — Tripletex Agent Memory
<!-- AUTO-UPDATED — DO NOT EDIT MANUALLY -->
<!-- Last updated: 2026-03-22 -->

## Quick Start
```bash
# New Cloud Shell session:
gcloud auth login
gcloud config set project ainm26osl-792
cd ~/nmiai && git pull origin master
source ~/keys.sh
python3 competition_state.py  # check scores
bash redeploy.sh               # deploy latest
```

## Server Commands
```bash
# Local development (Windows PowerShell):
cd C:\Users\Jani\nmiai
$env:PATH += ";C:\Users\Jani\AppData\Roaming\Python\Python314\Scripts"
$env:GOOGLE_API_KEY="AIzaSyCYRCYdNmarDh4L_er0tkO-mLY8W4KAKCg"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Local tests:
python test_local.py
python test_local.py --test 6
python scoring_debugger.py
python scoring_debugger.py --task create_invoice

# Cloud Shell deploy:
cd ~/nmiai && git pull && source ~/keys.sh && bash redeploy.sh

# Cloud Shell logs:
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=nmiai" \
  --limit=100 --project=ainm26osl-792 \
  --format="value(textPayload)" --freshness=10m

# Update state from logs:
python3 competition_state.py logs scorer_logs.txt
```

## Deployment
- Cloud Run URL: https://nmiai-247841323009.europe-north1.run.app/solve
- GCP Project: ainm26osl-792
- GitHub: https://github.com/janisombrero/nmiai.git
- Model: gemini-2.5-flash (google.genai SDK)
- Keys file: ~/keys.sh (Cloud Shell only)

## Architecture
Windows PC (Claude Code) → git push → GitHub → git pull → Cloud Shell → Cloud Run
Scorer → POST /solve → Gemini parse → dispatch() → Tripletex API → response

## Critical Known Issues
### RESOLVED — Bank account bootstrap
- Fix: PUT /company with full object fetch to inject 'bankAccountNumber'
- Code: _ensure_bank_account() in TaskHandler called before invoice creation

### RESOLVED — Auth format
- Fix: Basic Auth with username "0" and session_token as password
- Code: auth=("0", token) in tripletex.py

### RESOLVED — Invoice field names
- invoiceDueDate not dueDate
- POST /invoice directly, not PUT /order/:invoice

### RESOLVED — OrderLine field names
- count not quantity
- unitCostCurrency not unitPrice

### RESOLVED — Employee startDate
- Goes to POST /employee/employment not top-level employee object
- `nationalIdentityNumber` format validation handles 422 errors by self-healing
- Extracts and uses `department`, `salary`, `occupationCode`, `employmentPercentage`

### RESOLVED — Payment Registration
- Falls back to `invoiceDateFrom` broad search if `customerName` fuzzy match fails.
- Checks `amount * 1.25` for 25% default VAT cases where order cost vs selling price is skewed.
- Improved customer fuzzy matching logic.

### RESOLVED — Timesheet 409 Conflicts
- Self-heals 409 conflicts in `/timesheet/entry` by locating the existing entry for the employee, project, activity, and date, then PUT updating it instead of failing.

### RESOLVED — Address fields
- physicalAddress.addressLine1 not top-level address1
- country: {"id": 161} for Norway

### RESOLVED — VAT type selection
- Must use "Utgående" (output) VAT types not "Inngående" (input)
- id=3 (25%), id=31 (15%), id=32 (12%), id=6 (0%)
- Dynamic lookup from GET /ledger/vatType filtering by "Utgående" in name

### RESOLVED — Bulk creates
- Agent returns fields as list when creating multiple items
- Handled by list guard in dispatch()

### RESOLVED — Per-request credentials
- Scorer sends tripletex_credentials in request body
- base_url and session_token extracted per request

## Task Coverage
<!-- AUTO-UPDATED -->
43 handlers in dispatch()
Confirmed perfect: none yet
Confirmed failing: create_employee
Total unique tasks seen: 4

## Scorer Patterns
- Fresh sandbox per run, starts completely empty
- Tier 1 tasks: simple creates (employee, customer, supplier, department, product, project)
- Tier 2 tasks: invoice, payment, order, hours+invoice
- Tier 3 tasks: complex multi-step (fixed price project, bank reconciliation, year-end)
- 7 languages: Norwegian bokmål (nb), Norwegian nynorsk (nn), English (en),
               Spanish (es), Portuguese (pt), German (de), French (fr)
- Most common complex task: register_hours_and_invoice

## All Prompts Seen From Scorer
<!-- AUTO-UPDATED -->
See competition_state.json prompts_seen array for full list

## File Structure
nmiai/
├── main.py              — FastAPI app, /solve and /health endpoints
├── agent.py             — Gemini 2.5 Flash prompt parser
├── handlers.py          — 47 task handlers + TaskHandler class
├── tripletex.py         — TripletexClient (Basic Auth, get/post/put/delete)
├── test_local.py        — 10-task local test suite
├── scoring_debugger.py  — Scoring simulation tool
├── competition_state.py — State manager (read/update)
├── competition_state.json — Persistent memory
├── predict_tasks.py     — Gemini task prediction tool
├── Dockerfile           — Python 3.11, port 8080
├── deploy.sh            — Initial Cloud Run deploy
├── redeploy.sh          — Pull + redeploy
├── requirements.txt     — google-genai, fastapi, uvicorn, httpx, pypdf
└── MEMORY.md            — This file (auto-updated)

## Recording Findings
To record a finding without a Claude session:
```bash
# Record a finding
python3 competition_state.py finding create_invoice "Bank account bootstrap via PUT /company works if you GET full object first then merge"

# After submit
python3 competition_state.py submit

# Update from logs
python3 competition_state.py logs scorer_logs.txt
```

## Starting a New Claude Code Session
Paste this at the start of every new Claude session:
"I am competing in NM i AI 2026 Tripletex task. Read MEMORY.md and competition_state.json for full context. Then help me fix the highest priority failing task."
