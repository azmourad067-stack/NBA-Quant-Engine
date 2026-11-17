from __future__ import annotations

from typing import Any

import numpy as np
from psycopg import sql
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .dataset import FEATURE_COLUMNS, aggregate_verified_market_probabilities, build_feature_rows

MIN_TRAIN_GAMES = 100
MIN_TEST_GAMES = 25


def load_engine_dataset(db: Any) -> list[dict[str, Any]]:
    schema = sql.Identifier(db.schema)
    games = db.conn.execute(sql.SQL("""
        SELECT game_provider_id, game_date, scheduled_tipoff_at, game_status,
               home_team_provider_id, away_team_provider_id, home_score, away_score
        FROM {}.nba_games
        WHERE game_status IN ('final', 'scheduled')
        ORDER BY game_date, scheduled_tipoff_at
    """).format(schema)).fetchall()
    stats = db.conn.execute(sql.SQL("""
        SELECT game_provider_id, team_provider_id, field_goals_made,
               field_goals_attempted, three_pointers_made, offensive_rebounds,
               defensive_rebounds, assists, turnovers
        FROM {}.nba_team_game_stats
    """).format(schema)).fetchall()
    tipoffs = {
        str(g["game_provider_id"]): g.get("scheduled_tipoff_at") or g.get("game_date")
        for g in games
    }
    odds = db.conn.execute(sql.SQL("""
        SELECT o.game_provider_id, o.sportsbook, o.market_type, o.selection,
               o.decimal_odds, o.source_captured_at, o.snapshot_time_quality
        FROM {}.nba_odds_snapshots AS o
        JOIN {}.nba_games AS g ON g.game_provider_id = o.game_provider_id
        WHERE o.market_type = 'moneyline'
          AND o.snapshot_time_quality = 'verified_point_in_time'
          AND g.scheduled_tipoff_at IS NOT NULL
          AND o.source_captured_at < g.scheduled_tipoff_at
          AND o.decimal_odds > 1
    """).format(schema, schema)).fetchall()
    market = aggregate_verified_market_probabilities([dict(r) for r in odds], tipoffs)
    return build_feature_rows([dict(r) for r in games], [dict(r) for r in stats], market, include_upcoming=True)


def evaluate_dataset(rows: list[dict[str, Any]], min_train_games: int = MIN_TRAIN_GAMES) -> dict[str, Any]:
    usable = [r for r in rows if r.get("target_home_win") in (0, 1)]
    n = len(usable)
    if n < min_train_games + MIN_TEST_GAMES:
        return {
            "status": "insufficient_history", "games": n, "minimum_required": min_train_games + MIN_TEST_GAMES,
            "message": f"Historique insuffisant : {n} matchs terminés ; au moins {min_train_games + MIN_TEST_GAMES} requis.",
            "metrics": {}, "predictions": [],
        }
    split = max(min_train_games, int(n * 0.8))
    train, test = usable[:split], usable[split:]
    if len({r["target_home_win"] for r in train}) < 2:
        return {"status": "insufficient_classes", "games": n, "metrics": {}, "predictions": []}

    x_train = np.asarray([[r.get(c) for c in FEATURE_COLUMNS] for r in train], dtype=float)
    y_train = np.asarray([r["target_home_win"] for r in train], dtype=int)
    x_test = np.asarray([[r.get(c) for c in FEATURE_COLUMNS] for r in test], dtype=float)
    y_test = np.asarray([r["target_home_win"] for r in test], dtype=int)
    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("scale", StandardScaler()),
        ("logit", LogisticRegression(max_iter=2000, random_state=42)),
    ])
    model.fit(x_train, y_train)
    probs = model.predict_proba(x_test)[:, 1]
    predictions = [
        {"game_provider_id": row["game_provider_id"], "played_at": row["played_at"],
         "home_win_probability": float(prob), "actual_home_win": int(row["target_home_win"])}
        for row, prob in zip(test, probs)
    ]
    metrics = {
        "accuracy": float(accuracy_score(y_test, probs >= 0.5)),
        "brier_score": float(brier_score_loss(y_test, probs)),
        "log_loss": float(log_loss(y_test, probs, labels=[0, 1])),
        "roc_auc": float(roc_auc_score(y_test, probs)) if len(set(y_test)) == 2 else None,
        "elo_brier_score": float(brier_score_loss(y_test, [r["home_elo_prob"] for r in test])),
    }
    market_rows = [r for r in test if r.get("market_home_prob") is not None]
    if market_rows:
        metrics["market_brier_score"] = float(brier_score_loss(
            [r["target_home_win"] for r in market_rows],
            [r["market_home_prob"] for r in market_rows],
        ))
        metrics["market_test_games"] = len(market_rows)
    return {
        "status": "evaluated", "games": n, "train_games": len(train), "test_games": len(test),
        "split": "chronological_80_20", "metrics": metrics, "predictions": predictions,
        "feature_columns": FEATURE_COLUMNS,
    }



def predict_upcoming_games(rows: list[dict[str, Any]], min_train_games: int = MIN_TRAIN_GAMES) -> dict[str, Any]:
    completed = [r for r in rows if r.get("target_home_win") in (0, 1)]
    upcoming = [r for r in rows if r.get("target_home_win") is None]
    if len(completed) < min_train_games:
        return {"status": "insufficient_history", "games": len(completed), "predictions": []}
    if len({r["target_home_win"] for r in completed}) < 2:
        return {"status": "insufficient_classes", "games": len(completed), "predictions": []}
    if not upcoming:
        return {"status": "no_upcoming_games", "games": 0, "predictions": []}

    x_train = np.asarray([[r.get(c) for c in FEATURE_COLUMNS] for r in completed], dtype=float)
    y_train = np.asarray([r["target_home_win"] for r in completed], dtype=int)
    x_future = np.asarray([[r.get(c) for c in FEATURE_COLUMNS] for r in upcoming], dtype=float)
    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("scale", StandardScaler()),
        ("logit", LogisticRegression(max_iter=2000, random_state=42)),
    ])
    model.fit(x_train, y_train)
    probabilities = model.predict_proba(x_future)[:, 1]
    predictions = []
    for row, probability in zip(upcoming, probabilities):
        predictions.append({
            "game_provider_id": row["game_provider_id"],
            "played_at": row["played_at"],
            "home_team_provider_id": row["home_team_provider_id"],
            "away_team_provider_id": row["away_team_provider_id"],
            "home_win_probability": float(probability),
            "away_win_probability": float(1.0 - probability),
            "market_home_prob": row.get("market_home_prob"),
            "elo_home_prob": row["home_elo_prob"],
        })
    return {"status": "predicted", "games": len(predictions), "predictions": predictions}


def build_engine_report(db: Any, min_train_games: int = MIN_TRAIN_GAMES) -> dict[str, Any]:
    rows = load_engine_dataset(db)
    report = evaluate_dataset(rows, min_train_games=min_train_games)
    report["upcoming"] = predict_upcoming_games(rows, min_train_games=min_train_games)
    completed_rows = [r for r in rows if r.get("target_home_win") in (0, 1)]
    upcoming_rows = [r for r in rows if r.get("target_home_win") is None]
    report["dataset"] = {
        "games": len(completed_rows),
        "upcoming_games": len(upcoming_rows),
        "market_feature_coverage": sum(r.get("market_home_prob") is not None for r in completed_rows),
        "feature_count": len(FEATURE_COLUMNS),
        "time_leakage_policy": "verified_point_in_time AND source_captured_at < scheduled_tipoff_at",
    }
    return report
