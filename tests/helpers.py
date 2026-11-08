"""Explicitly fictional, stable simulation test inputs."""


def prepared_fixture(roster_size=10):
    teams = []
    for tid in ("SYN-H", "SYN-A"):
        players = []
        for i in range(roster_size):
            pid = f"{tid}-{i}"
            players.append({
                "id": pid, "player_id": pid, "team_id": tid,
                "name": f"Fictional player {pid}", "position": ("PG", "SG", "SF", "PF", "C")[i % 5],
                "archetypes": [], "minutes_cap": 48, "expected_minutes": 240 / roster_size,
                "active_probability": 1, "usage": .48,
                "three_share": .38, "two_pct": .53, "three_pct": .36, "ft_pct": .78,
                "turnover": .12, "foul_draw": .1, "assist_weight": .09,
                "rebound_weight": .16, "steal_weight": .025, "block_weight": .018,
            })
        teams.append({"id": tid, "name": f"Fictional {tid}", "pace": 99,
                      "offense": 114, "defense": 114, "players": players})
    return {"teams": teams, "warnings": []}
