import os, json, time, requests

key = os.environ["FANTASYPROS_API_KEY"]
BASE = "https://api.fantasypros.com/public/v2/json/nfl/2025/projections"
KELCE_FPID = 11594

def call(label, params):
    r = requests.get(BASE, headers={"x-api-key": key}, params=params, timeout=30)
    print(f"\n=== {label} ===")
    print("status:", r.status_code, "| params:", params)
    if not r.ok:
        print("body:", r.text[:300])
        return None
    data = r.json()
    players = data.get("players", [])
    print("player_count returned:", len(players))
    print("count (total available):", data.get("count"), "| limit:", data.get("limit"))
    print("players:", [p["name"] for p in players])
    return data

# targeted per-player queries, 1.5s apart to avoid the rate limit we hit last time
for param_name in ["player_id", "id", "fpid"]:
    call(f"targeted query via '{param_name}'", {"position": "TE", "week": 1, param_name: KELCE_FPID})
    time.sleep(1.5)

# does limit=50 actually return more than 10?
call("requesting limit=50", {"position": "TE", "week": 1, "limit": 50})
time.sleep(1.5)

# retry offset (got rate-limited last time)
call("requesting offset=10", {"position": "TE", "week": 1, "offset": 10})
time.sleep(1.5)

call("requesting page=2", {"position": "TE", "week": 1, "page": 2})
