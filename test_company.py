import requests
import json
auth = ('0', 'eyJ0b2tlbklkIjoyMTQ3NjM0ODkxLCJ0b2tlbiI6IjQzZjM3ZjVjLTAyZGEtNGUwZC1hMWYwLWNkMjUwMWE0ZDczMyJ9')
base_url = 'https://kkpqfuj-amager.tripletex.dev/v2'

r = requests.get(f'{base_url}/company/settings', auth=auth)
print(r.status_code, json.dumps(r.json(), indent=2))