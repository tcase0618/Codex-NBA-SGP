from test_board_api import board


def test_read_only_polymarket_routes_report_actual_provider_and_no_ticket_price(board, monkeypatch):
    client, game, _, _, _ = board
    report = {"provider": "Polymarket", "status": "PARTIAL_METADATA_ONLY", "offers": [
        {"american_odds": None, "indicative_american_odds": 150, "executable": False}],
        "coverage": {"events": 1}, "joint_quote": None}
    monkeypatch.setattr("nba_quant.api.load_polymarket_odds", lambda: report)
    seen = []
    def refresh(**kwargs):
        seen.append(kwargs["slate"]["games"][0]["id"])
        return report
    monkeypatch.setattr("nba_quant.api.refresh_polymarket_odds", refresh)
    for response in (client.get("/api/polymarket"), client.post("/api/polymarket/refresh")):
        assert response.status_code == 200
        assert response.json()["provider"] == "Polymarket"
        assert response.json()["actionable"] is False
        assert response.json()["markets"][0]["american_odds"] is None
        assert response.json()["markets"][0]["indicative_american_odds"] == 150
    assert seen == [game["id"]]
    assert client.post("/api/polymarket/refresh", headers={"Origin": "https://foreign.example"}).status_code == 403


def test_aggressive_mode_is_bound_to_saved_bundle_and_invalid_modes_rejected(board):
    client, game, _, _, _ = board
    response = client.post(f"/api/games/{game['id']}/drafts?selection_mode=aggressive")
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["selection_mode"] == "aggressive"
    assert all(f["status"] == "BLOCKED" for f in bundle["families"])
    saved = client.get("/api/tonight").json()["games"][0]
    assert saved["sgp"]["draft_selection_mode"] == "aggressive"
    assert client.post(f"/api/games/{game['id']}/drafts?selection_mode=always-win").status_code == 422
