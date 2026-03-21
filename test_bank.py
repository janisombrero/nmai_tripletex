import requests
auth = ('0', 'eyJ0b2tlbklkIjoyMTQ3NjM0ODkxLCJ0b2tlbiI6IjQzZjM3ZjVjLTAyZGEtNGUwZC1hMWYwLWNkMjUwMWE0ZDczMyJ9')
base_url = 'https://kkpqfuj-amager.tripletex.dev/v2'

# 1. Clear bankAccountNumber on all bank accounts
r = requests.get(f'{base_url}/ledger/account?isBankAccount=true', auth=auth)
for acc in r.json().get('values', []):
    acc['bankAccountNumber'] = ''
    requests.put(f'{base_url}/ledger/account/{acc["id"]}', json=acc, auth=auth)

# 2. Try to create invoice
payload = {
    "invoiceDate": "2026-03-20",
    "invoiceDueDate": "2026-03-27",
    "customer": {"id": 108289275},
    "orders": [{"id": 401971775}]
}
r2 = requests.post(f'{base_url}/invoice', json=payload, auth=auth)
print(r2.status_code, r2.text)
