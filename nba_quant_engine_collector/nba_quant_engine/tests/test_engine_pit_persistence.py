from contextlib import nullcontext

from engine.dataset import build_feature_rows
from engine.service import FEATURE_COLUMNS, persist_upcoming_predictions


def test_rest_uses_previous_scheduled_game_for_same_team():
    games = [
        {
            "game_provider_id": "future-1",
            "scheduled_tipoff_at": "2026-11-21T00:00:00Z",
            "game_status": "scheduled",
            "home_team_provider_id": "A",
            "away_team_provider_id": "B",
        },
        {
            "game_provider_id": "future-2",
            "scheduled_tipoff_at": "2026-11-23T00:00:00Z",
            "game_status": "scheduled",
            "home_team_provider_id": "A",
            "away_team_provider_id": "C",
        },
    ]
    rows = build_feature_rows(games, [], include_upcoming=True)
    assert rows[1]["home_rest_days"] == 2.0
    assert rows[1]["rest_days_diff"] is None


class _Result:
    def fetchone(self):
        return {"feature_snapshot_id": "snapshot-1"}


class _Connection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params):
        self.calls.append((str(query), params))
        return _Result() if "RETURNING feature_snapshot_id" in str(query) else None


class _Database:
    schema = "nba"

    def __init__(self):
        self.conn = _Connection()


def test_persistence_writes_one_snapshot_and_two_sided_predictions():
    db = _Database()
    features = {name: 0.25 for name in FEATURE_COLUMNS}
    prediction = {
        "game_provider_id": "game-1",
        "home_win_probability": 0.6,
        "away_win_probability": 0.4,
        "features": features,
        "feature_cutoff_at": "2026-11-17T15:00:00+00:00",
        "predicted_at": "2026-11-17T15:00:00+00:00",
        "source_max_available_at": "2026-11-17T14:00:00+00:00",
        "feature_source_game_ids": ["old-1", "old-2"],
        "source_timestamp_verified": True,
        "training_pit_verified": True,
        "elo_home_prob": 0.55,
        "market_home_prob": None,
    }

    assert persist_upcoming_predictions(db, [prediction]) == 1
    assert len(db.conn.calls) == 3
    snapshot_sql, snapshot_params = db.conn.calls[0]
    assert "nba_feature_snapshots" in snapshot_sql
    assert snapshot_params[1] == "0.2.0-pit"
    home_sql, home_params = db.conn.calls[1]
    away_sql, away_params = db.conn.calls[2]
    assert "nba_predictions" in home_sql and "nba_predictions" in away_sql
    assert home_params[4] == "home" and home_params[5] == 0.6
    assert away_params[4] == "away" and away_params[5] == 0.4
