import os, json, requests

key = os.environ["FANTASYPROS_API_KEY"]
BASE = "https://api.fantasypros.com/public/v2/json/nfl/2025/projections"

def call(label, params):
    r = requests.get(BASE, headers={"x-api-key": key}, params=params, timeout=30)
    print(f"\n=== {label} ===")
    print("status:", r.status_code, "| params:", params)
    if not r.ok:
        print("body:", r.text[:300])
        return None
    return r.json()

def find_player(data, name_substr):
    if not data:
        return None
    for p in data.get("players", []):
        if name_substr.lower() in p.get("name", "").lower():
            return p
    return None

# --- (1) scoring-param check on a real pass-catcher ---
std = call("TE list, scoring=STD", {"position": "TE", "week": 1, "scoring": "STD"})
ppr = call("TE list, scoring=PPR", {"position": "TE", "week": 1, "scoring": "PPR"})

for label, data in [("STD call", std), ("PPR call", ppr)]:
    if data:
        names = [p["name"] for p in data.get("players", [])]
        print(f"{label} top-10 TEs returned:", names)
        print(f"{label} echoed scoring field:", data.get("scoring"))

kelce = find_player(std, "Kelce") or find_player(ppr, "Kelce")
if kelce:
    print("\nKelce found. Full stats object:")
    print(json.dumps(kelce["stats"], indent=2))
    print(f"points={kelce['stats'].get('points')} vs points_ppr={kelce['stats'].get('points_ppr')} "
          f"-> {'DIFFERENT (both formats real)' if kelce['stats'].get('points') != kelce['stats'].get('points_ppr') else 'IDENTICAL (suspicious for a pass-catcher)'}")
    kelce_fpid = kelce["fpid"]
else:
    print("\nKelce not in top-10 TE list either way (may be outside top 10 this season) — falling back to a known real fpid isn't possible without another lookup.")
    kelce_fpid = None

# --- (2) per-player querying: try candidate param names ---
if kelce_fpid:
    for param_name in ["player_id", "id", "fpid"]:
        call(f"targeted query via '{param_name}'", {"position": "TE", "week": 1, param_name: kelce_fpid})

# --- pagination / limit checks (relevant either way) ---
call("requesting limit=50", {"position": "TE", "week": 1, "limit": 50})
call("requesting offset=10", {"position": "TE", "week": 1, "offset": 10})
call("requesting page=2", {"position": "TE", "week": 1, "page": 2})
