import os
from tripletex import TripletexClient
from handlers import TaskHandler

client = TripletexClient("https://kkpqfuj-amager.tripletex.dev/v2", "eyJ0b2tlbklkIjoyMTQ3NjM0ODkxLCJ0b2tlbiI6IjQzZjM3ZjVjLTAyZGEtNGUwZC1hMWYwLWNkMjUwMWE0ZDczMyJ9")
handler = TaskHandler(client)
pm_id = handler._find_employee_id_for_project()
print(f"PM ID: {pm_id}")
