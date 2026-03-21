import os
from tripletex import TripletexClient

client = TripletexClient()
client.set_auth_from_env()

# Get a project
status, current = client.get("/project", params={"count": 1})
if status == 200 and current.get("values"):
    project = current["values"][0]
    pid = project["id"]
    version = project.get("version", 0)
    print(f"Project ID: {pid}, Version: {version}")
    
    # Try partial update with version
    status, res = client.put(f"/project/{pid}", json={"name": project["name"] + " test", "version": version})
    print(f"Partial update (with version) status: {status}")
    print(res)

    # Revert
    status, res = client.put(f"/project/{pid}", json={"name": project["name"], "version": version + 1})
    print(f"Revert status: {status}")
else:
    print("Could not get project:", status, current)
