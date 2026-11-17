from engine.dataset import aggregate_verified_market_probabilities, build_feature_rows


def test_current_game_stats_never_enter_its_own_features():
    games = [
        {"game_provider_id": "g1", "scheduled_tipoff_at": "2026-01-01T20:00:00Z", "game_status": "final", "home_team_provider_id": "A", "away_team_provider_id": "B", "home_score": 110, "away_score": 100},
        {"game_provider_id": "g2", "scheduled_tipoff_at": "2026-01-03T20:00:00Z", "game_status": "final", "home_team_provider_id": "A", "away_team_provider_id": "C", "home_score": 90, "away_score": 105},
    ]
    stats = [
        {"game_provider_id": "g1", "team_provider_id": "A", "field_goals_made": 50, "field_goals_attempted": 80, "three_pointers_made": 15, "offensive_rebounds": 10, "defensive_rebounds": 30, "assists": 25, "turnovers": 8},
        {"game_provider_id": "g1", "team_provider_id": "B", "field_goals_made": 40, "field_goals_attempted": 80},
        {"game_provider_id": "g2", "team_provider_id": "A", "field_goals_made": 1, "field_goals_attempted": 1},
    ]
    rows = build_feature_rows(games, stats)
    assert rows[0]["home_win_rate_last5"] is None
    assert rows[1]["home_win_rate_last5"] == 1.0
    assert rows[1]["home_points_for_last5"] == 110.0
    assert rows[1]["home_fg_pct_last5"] == 50 / 80


def test_market_odds_after_tipoff_or_unverified_are_excluded():
    odds = [
        {"game_provider_id": "g1", "sportsbook": "book", "market_type": "moneyline", "selection": "home", "decimal_odds": 2.0, "source_captured_at": "2026-01-01T19:00:00Z", "snapshot_time_quality": "verified_point_in_time"},
        {"game_provider_id": "g1", "sportsbook": "book", "market_type": "moneyline", "selection": "away", "decimal_odds": 2.0, "source_captured_at": "2026-01-01T19:00:00Z", "snapshot_time_quality": "verified_point_in_time"},
        {"game_provider_id": "g1", "sportsbook": "late", "market_type": "moneyline", "selection": "home", "decimal_odds": 1.2, "source_captured_at": "2026-01-01T21:00:00Z", "snapshot_time_quality": "verified_point_in_time"},
        {"game_provider_id": "g1", "sportsbook": "late", "market_type": "moneyline", "selection": "away", "decimal_odds": 5.0, "source_captured_at": "2026-01-01T19:00:00Z", "snapshot_time_quality": "unverified"},
    ]
    result = aggregate_verified_market_probabilities(odds, {"g1": "2026-01-01T20:00:00Z"})
    assert result == {"g1": 0.5}


def test_scheduled_game_is_emitted_without_using_its_result():
    games = [
        {"game_provider_id": "g1", "scheduled_tipoff_at": "2026-01-01T20:00:00Z", "game_status": "final", "home_team_provider_id": "A", "away_team_provider_id": "B", "home_score": 110, "away_score": 100},
        {"game_provider_id": "future", "scheduled_tipoff_at": "2026-01-03T20:00:00Z", "game_status": "scheduled", "home_team_provider_id": "A", "away_team_provider_id": "C", "home_score": None, "away_score": None},
    ]
    rows = build_feature_rows(games, [], include_upcoming=True)
    assert len(rows) == 2
    assert rows[1]["game_provider_id"] == "future"
    assert rows[1]["target_home_win"] is None
    assert rows[1]["home_win_rate_last5"] == 1.0
