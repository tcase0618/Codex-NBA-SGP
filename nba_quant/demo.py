"""Fictional identities and simulated history; never presented as NBA observations."""
from datetime import datetime, timedelta, timezone

from .model import prepare
from .schemas import Dataset, ForecastRequest
from .simulation import game


def make_demo():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    teams = [{"id": "demo-harbor", "name": "Harbor Comets"},
             {"id": "demo-metro", "name": "Metro Owls"}]
    players, rotations = [], []
    positions = ["PG", "SG", "SF", "PF", "C"] * 2
    roles = ["primary initiator", "movement shooter", "wing stopper", "stretch big", "rim protector"] * 2
    for team in teams:
        for i in range(10):
            pid = f"{team['id']}-{i + 1:02d}"
            players.append({"id": pid, "name": f"{team['name'].split()[0]} Player {i + 1:02d}",
                            "position": positions[i], "archetypes": [roles[i]]})
            rotations.append({"player_id": pid, "team_id": team["id"],
                "known_at": (now - timedelta(days=70)).isoformat(),
                "expected_minutes": [33, 32, 30, 29, 28, 22, 20, 18, 16, 12][i],
                "minutes_cap": 48, "active_probability": 1, "starter": i < 5, "status": "synthetic available"})
    payload = {"source": "NBA Quant synthetic fixture v1", "source_url": "fixture://nba-quant/demo-v1",
               "retrieved_at": now.isoformat(), "synthetic": True, "teams": teams,
               "players": players, "rotations": rotations, "games": []}
    # Generate a deterministic historical fixture from the same baseline priors.
    # This is NOT independent model validation or evidence of predictive skill.
    for i in range(30):
        tip = now - timedelta(days=62 - i * 2)
        home, away = teams[i % 2]["id"], teams[1 - i % 2]["id"]
        req = ForecastRequest(dataset_id="demo", home_id=home, away_id=away, tipoff=tip,
                              cutoff=tip - timedelta(hours=1), simulations=20)
        context = prepare(payload, req)
        result = game(context, 9100 + i)
        boxes = []
        for tid in (home, away):
            for p in players:
                if p["id"].startswith(tid + "-"):
                    b = {k: v for k, v in result["boxes"][p["id"]].items() if k != "reb"}
                    boxes.append({"player_id": p["id"], "team_id": tid, "pf": 0, **b})
        payload["games"].append({"id": f"demo-game-{i:03d}", "home_id": home, "away_id": away,
            "tipoff": tip.isoformat(), "completed_at": (tip + timedelta(hours=3)).isoformat(),
            "known_at": (tip + timedelta(hours=4)).isoformat(),
            "home_score": result["score"][0], "away_score": result["score"][1],
            "overtime_periods": result["overtime_periods"], "boxes": boxes})
    payload["rotations"].extend([{**r, "known_at": (now - timedelta(hours=1)).isoformat()} for r in rotations.copy()])
    return Dataset.model_validate(payload)
