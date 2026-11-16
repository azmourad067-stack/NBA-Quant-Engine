from datetime import datetime, timezone

import pytest

from collector.mappings import (
    american_to_decimal,
    game_record,
    normalize_game_status,
    odds_records,
    player_records,
    team_stats_records,
)


def example_game(status="final", starts_at="2024-03-17T00:30:00.000Z"):
    return {
        "game_id": "401585601",
        "season": 2024,
        "season_type": "regular",
        "date": "2024-03-16",
        "starts_at": starts_at,
        "status": status,
        "home": {"team_id": "13", "abbrev": "LAL", "name": "Los Angeles Lakers", "score": 121},
        "away": {"team_id": "9", "abbrev": "GS", "name": "Golden State Warriors", "score": 128},
    }


def test_american_odds_conversion():
    assert american_to_decimal(-110) == pytest.approx(1.909091, rel=1e-5)
    assert american_to_decimal(150) == pytest.approx(2.5)
    assert american_to_decimal(100) == pytest.approx(2.0)
    assert american_to_decimal(0) is None
    assert american_to_decimal(None) is None


def test_game_record_maps_provider_identifiers_and_final_score():
    row = game_record(example_game())
    assert row["game_provider_id"] == "401585601"
    assert row["home_team_provider_id"] == "13"
    assert row["away_team_provider_id"] == "9"
    assert row["game_status"] == "final"
    assert row["home_score"] == 121
    assert row["season_end_year"] == 2024


def test_final_game_without_score_is_not_written_as_final():
    game = example_game()
    game["home"]["score"] = None
    row = game_record(game)
    assert row["game_status"] == "unknown"


def test_normalize_game_status():
    assert normalize_game_status("Final") == "final"
    assert normalize_game_status("Scheduled") == "scheduled"
    assert normalize_game_status("In Progress") == "live"
    assert normalize_game_status("Postponed") == "postponed"


def test_team_stats_do_not_invent_pace_or_possessions():
    rows = team_stats_records("401585601", [{
        "team_id": "13", "home": True, "pts": 121, "fgm": 47,
        "fga": 92, "tpm": 12, "tpa": 34, "ftm": 15, "fta": 18,
        "oreb": 8, "dreb": 31, "reb": 39, "ast": 32, "stl": 3,
        "blk": 4, "tov": 10, "pf": 13,
    }])
    assert rows[0]["points"] == 121
    assert rows[0]["offensive_rebounds"] == 8
    assert rows[0]["defensive_rebounds"] == 31
    assert rows[0]["pace"] is None
    assert rows[0]["possessions"] is None


def test_player_rows_map_full_box_score_without_guessing_active_status():
    player_rows, stat_rows = player_records([{
        "player_id": "1966", "name": "LeBron James", "team_id": "13", "position": "F",
        "minutes": 38, "pts": 40, "reb": 8, "oreb": 3, "dreb": 5,
        "ast": 9, "stl": 1, "blk": 1, "tov": 4,
        "fgm": 15, "fga": 23, "tpm": 3, "tpa": 5,
        "ftm": 7, "fta": 8, "plus_minus": -6,
    }])
    assert player_rows[0]["display_name"] == "LeBron James"
    assert player_rows[0]["is_active"] is None
    assert stat_rows[0]["points"] == 40
    assert stat_rows[0]["minutes"] == 38


def test_odds_rows_include_moneyline_open_and_close_and_closing_spread_total():
    game = example_game(status="final")
    rows = odds_records(game, [{
        "provider": "BetMGM",
        "home_ml_open": -155,
        "home_ml_close": -125,
        "away_ml_open": 125,
        "away_ml_close": 105,
        "home_spread_open": None,
        "home_spread_close": -1.5,
        "spread_home_odds": -110,
        "spread_away_odds": -110,
        "total_open": 235.5,
        "total_close": 233.5,
        "over_odds": -110,
        "under_odds": -110,
        "captured_at": "2026-10-06T18:38:25.772Z",
    }])
    assert len(rows) == 8
    assert any(r["market_type"] == "moneyline" and r["price_phase"] == "opening" for r in rows)
    assert any(r["market_type"] == "spread" and r["selection"] == "away" and r["line_value"] == 1.5 for r in rows)
    assert any(r["market_type"] == "total" and r["selection"] == "over" and r["line_value"] == 233.5 for r in rows)
    assert all(r["snapshot_time_quality"] == "postgame_backfill" for r in rows)


def test_future_game_capture_before_tipoff_is_verified_pit():
    game = {
        "game_id": "G1", "status": "scheduled", "starts_at": "2026-12-01T00:00:00Z",
    }
    rows = odds_records(game, [{
        "provider": "Book", "home_ml_close": -110, "away_ml_close": -110,
        "captured_at": "2026-11-30T18:00:00Z",
    }], fetched_at=datetime(2026, 11, 30, 18, 5, tzinfo=timezone.utc))
    assert len(rows) == 2
    assert all(r["snapshot_time_quality"] == "verified_point_in_time" for r in rows)


def test_missing_capture_timestamp_is_never_marked_verified():
    game = {"game_id": "G2", "status": "scheduled", "starts_at": "2026-12-01T00:00:00Z"}
    rows = odds_records(game, [{"provider": "Book", "home_ml_close": -110, "away_ml_close": -110}])
    assert all(r["snapshot_time_quality"] == "unknown" for r in rows)


def test_in_progress_status_variant():
    assert normalize_game_status("in_progress") == "live"


def test_historical_capture_before_tipoff_remains_verified_even_after_game_final():
    game = {"game_id": "G3", "status": "final", "starts_at": "2026-12-01T00:00:00Z"}
    rows = odds_records(game, [{
        "provider": "Book", "home_ml_close": -110, "away_ml_close": -110,
        "captured_at": "2026-11-30T18:00:00Z",
    }])
    assert all(r["snapshot_time_quality"] == "verified_point_in_time" for r in rows)
