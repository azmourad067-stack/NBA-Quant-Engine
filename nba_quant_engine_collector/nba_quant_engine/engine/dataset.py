from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timezone
from statistics import median
from typing import Any, Iterable

FEATURE_COLUMNS = [
    "home_elo_prob", "home_rest_days", "away_rest_days", "rest_days_diff",
    "home_win_rate_last5", "away_win_rate_last5",
    "home_points_for_last5", "home_points_against_last5",
    "away_points_for_last5", "away_points_against_last5",
    "home_point_diff_last5", "away_point_diff_last5",
    "home_fg_pct_last5", "away_fg_pct_last5",
    "home_three_made_last5", "away_three_made_last5",
    "home_rebounds_last5", "away_rebounds_last5",
    "home_assists_last5", "away_assists_last5",
    "home_turnovers_last5", "away_turnovers_last5",
    "market_home_prob",
]


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _mean(values: Iterable[float | int | None]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _team_summary(history: deque[dict[str, Any]], current_time: datetime) -> dict[str, float | None]:
    rows = list(history)
    if not rows:
        return {key: None for key in (
            "rest_days", "win_rate", "points_for", "points_against", "point_diff",
            "fg_pct", "three_made", "rebounds", "assists", "turnovers"
        )}
    rest = max(0.0, (current_time - rows[-1]["played_at"]).total_seconds() / 86400.0)
    fg_m = sum(r["fgm"] for r in rows if r["fgm"] is not None)
    fg_a = sum(r["fga"] for r in rows if r["fga"] is not None)
    return {
        "rest_days": rest,
        "win_rate": _mean([r["win"] for r in rows]),
        "points_for": _mean([r["points_for"] for r in rows]),
        "points_against": _mean([r["points_against"] for r in rows]),
        "point_diff": _mean([r["points_for"] - r["points_against"] for r in rows]),
        "fg_pct": fg_m / fg_a if fg_a else None,
        "three_made": _mean([r["three_made"] for r in rows]),
        "rebounds": _mean([r["rebounds"] for r in rows]),
        "assists": _mean([r["assists"] for r in rows]),
        "turnovers": _mean([r["turnovers"] for r in rows]),
    }


def build_feature_rows(
    games: list[dict[str, Any]],
    team_stats: list[dict[str, Any]],
    market_home_prob: dict[str, float] | None = None,
    history_window: int = 5,
    elo_k: float = 20.0,
    home_advantage: float = 65.0,
    include_upcoming: bool = False,
) -> list[dict[str, Any]]:
    """Build chronological pre-game rows; current-game stats are added only afterwards."""
    if history_window < 1:
        raise ValueError("history_window must be >= 1")
    stats_by_game: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for stat in team_stats:
        stats_by_game[str(stat.get("game_provider_id"))][str(stat.get("team_provider_id"))] = stat

    ordered = []
    for game in games:
        status = str(game.get("game_status", "")).lower()
        is_final = status == "final" and game.get("home_score") is not None and game.get("away_score") is not None
        is_upcoming = include_upcoming and status == "scheduled"
        if not (is_final or is_upcoming):
            continue
        played_at = _as_datetime(game.get("scheduled_tipoff_at") or game.get("game_date"))
        if played_at is not None:
            ordered.append((played_at, game))
    ordered.sort(key=lambda item: (item[0], str(item[1].get("game_provider_id"))))

    histories: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=history_window))
    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    rows: list[dict[str, Any]] = []

    for played_at, game in ordered:
        game_id = str(game["game_provider_id"])
        home_id = str(game["home_team_provider_id"])
        away_id = str(game["away_team_provider_id"])
        h = _team_summary(histories[home_id], played_at)
        a = _team_summary(histories[away_id], played_at)
        elo_prob = 1.0 / (1.0 + 10.0 ** (-(elo[home_id] + home_advantage - elo[away_id]) / 400.0))
        row = {
            "game_provider_id": game_id, "played_at": played_at.isoformat(),
            "home_team_provider_id": home_id, "away_team_provider_id": away_id,
            "home_score": int(game["home_score"]) if game.get("home_score") is not None else None,
            "away_score": int(game["away_score"]) if game.get("away_score") is not None else None,
            "target_home_win": int(game["home_score"] > game["away_score"]) if game.get("home_score") is not None and game.get("away_score") is not None else None,
            "home_elo_prob": elo_prob,
            "home_rest_days": h["rest_days"], "away_rest_days": a["rest_days"],
            "rest_days_diff": h["rest_days"] - a["rest_days"] if h["rest_days"] is not None and a["rest_days"] is not None else None,
            "home_win_rate_last5": h["win_rate"], "away_win_rate_last5": a["win_rate"],
            "home_points_for_last5": h["points_for"], "home_points_against_last5": h["points_against"],
            "away_points_for_last5": a["points_for"], "away_points_against_last5": a["points_against"],
            "home_point_diff_last5": h["point_diff"], "away_point_diff_last5": a["point_diff"],
            "home_fg_pct_last5": h["fg_pct"], "away_fg_pct_last5": a["fg_pct"],
            "home_three_made_last5": h["three_made"], "away_three_made_last5": a["three_made"],
            "home_rebounds_last5": h["rebounds"], "away_rebounds_last5": a["rebounds"],
            "home_assists_last5": h["assists"], "away_assists_last5": a["assists"],
            "home_turnovers_last5": h["turnovers"], "away_turnovers_last5": a["turnovers"],
            "market_home_prob": (market_home_prob or {}).get(game_id),
        }
        rows.append(row)

        # Upcoming games are prediction targets, never history updates.
        if row["target_home_win"] is None:
            continue

        # Historical labels/stats update state only after this game's feature row is frozen.
        home_stats = stats_by_game.get(game_id, {}).get(home_id, {})
        away_stats = stats_by_game.get(game_id, {}).get(away_id, {})
        home_win = int(game["home_score"] > game["away_score"])
        for team_id, own, win in (
            (home_id, home_stats, home_win), (away_id, away_stats, 1 - home_win)
        ):
            off_reb, def_reb = own.get("offensive_rebounds"), own.get("defensive_rebounds")
            rebounds = None if off_reb is None and def_reb is None else (off_reb or 0) + (def_reb or 0)
            histories[team_id].append({
                "played_at": played_at, "win": win,
                "points_for": game["home_score"] if team_id == home_id else game["away_score"],
                "points_against": game["away_score"] if team_id == home_id else game["home_score"],
                "fgm": own.get("field_goals_made"), "fga": own.get("field_goals_attempted"),
                "three_made": own.get("three_pointers_made"), "rebounds": rebounds,
                "assists": own.get("assists"), "turnovers": own.get("turnovers"),
            })
        elo[home_id] += elo_k * (home_win - elo_prob)
        elo[away_id] += elo_k * ((1 - home_win) - (1 - elo_prob))
    return rows


def aggregate_verified_market_probabilities(
    odds_rows: list[dict[str, Any]], tipoffs: dict[str, datetime | str]
) -> dict[str, float]:
    """Use paired no-vig moneyline prices from verified snapshots strictly before tipoff."""
    per_game_book: dict[str, dict[str, dict[str, tuple[datetime, float]]]] = defaultdict(lambda: defaultdict(dict))
    for row in odds_rows:
        game_id = str(row.get("game_provider_id"))
        tipoff, captured = _as_datetime(tipoffs.get(game_id)), _as_datetime(row.get("source_captured_at"))
        if not tipoff or not captured or captured >= tipoff:
            continue
        if row.get("snapshot_time_quality") != "verified_point_in_time":
            continue
        if str(row.get("market_type", "")).lower() != "moneyline":
            continue
        selection, odds = str(row.get("selection", "")).lower(), row.get("decimal_odds")
        if selection not in {"home", "away"} or odds is None or float(odds) <= 1.0:
            continue
        book = str(row.get("sportsbook") or "unknown")
        previous = per_game_book[game_id][book].get(selection)
        if previous is None or captured > previous[0]:
            per_game_book[game_id][book][selection] = (captured, float(odds))

    result: dict[str, float] = {}
    for game_id, books in per_game_book.items():
        probabilities = []
        for sides in books.values():
            if "home" not in sides or "away" not in sides:
                continue
            home_imp, away_imp = 1 / sides["home"][1], 1 / sides["away"][1]
            probabilities.append(home_imp / (home_imp + away_imp))
        if probabilities:
            result[game_id] = float(median(probabilities))
    return result
