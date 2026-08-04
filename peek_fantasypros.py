import os, json, requests
key = os.environ["FANTASYPROS_API_KEY"]
resp = requests.get(
    "https://api.fantasypros.com/public/v2/json/nfl/2025/projections",
    params={"position": "ALL", "week": 1, "scoring": "PPR"},
    headers={"x-api-key": key},
    timeout=30,
)
data = resp.json()
print("--- top-level info (no player list) ---")
print(json.dumps({k: v for k, v in data.items() if k != "players"}, indent=2))
print("--- first player, full object ---")
print(json.dumps(data["players"][0], indent=2))
