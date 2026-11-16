from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any


VALID_SEASON_TYPES = {"regular", "postseason", "play_in", "preseason"}
VALID_GAME_STATUSES = {"scheduled", "live", "final", "postponed", "cancelled", "unknown"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(value: Any, fallback_ts: Any = None) -> date:
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            pass
    parsed = parse_datetime(fallback_ts)
    if parsed:
        return parsed.date()
    raise ValueError(f"Cannot determine game_date from date={value!r}, starts_at={fallback_ts!r}")


def normalize_season_type(value: Any) -> str:
    value = str(value or "regular").strip().lower().replace(" ", "_")
    aliases = {"play-in": "play_in", "playin": "play_in", "post_season": "postseason"}
    value = aliases.get(value, value)
    return value if value in VALID_SEASON_TYPES else "regular"


def normalize_game_status(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    if text in {"final", "finished", "complete", "completed"} or "final" in text:
        return "final"
    if text in {"scheduled", "pre-game", "pregame", "not started"}:
        return "scheduled"
    if any(token in text for token in ("progress", "live", "halftime", "quarter", "period")):
        return "live"
    if "postpon" in text:
        return "postponed"
    if any(token in text for token in ("cancel", "abandon")):
        return "cancelled"
    return "unknown"


def team_record(team: dict[str, Any]) -> dict[str, Any] | None:
    team_id = team.get("team_id") or team.get("id")
    if team_id is None:
        return None
    display_name = team.get("display_name") or team.get("name") or team.get("abbrev") or str(team_id)
    return {
        "team_provider_id": str(team_id),
        "abbreviation": str(team.get("abbrev") or team.get("abbreviation") or "UNK").upper(),
        "display_name": str(display_name),
        "conference": team.get("conference"),
        "division": team.get("division"),
        "is_active": team.get("is_active"),
        "source_provider": "dunkapi",
        "source_updated_at": None,
    }


def game_record(game: dict[str, Any], fallback_season: int | None = None) -> dict[str, Any]:
    home = game.get("home") or {}
    away = game.get("away") or {}
    game_id = game.get("game_id") or game.get("id")
    if game_id is None:
        raise ValueError("DunkAPI game record has no game_id.")
    season = game.get("season") or fallback_season
    if season is None:
        raise ValueError(f"Game {game_id} has no season and no fallback season was supplied.")
    home_id = home.get("team_id")
    away_id = away.get("team_id")
    if home_id is None or away_id is None:
        raise ValueError(f"Game {game_id} is missing a home/away team id.")

    status = normalize_game_status(game.get("status"))
    home_score = _int(home.get("score"))
    away_score = _int(away.get("score"))
    if status == "final" and (home_score is None or away_score is None):
        # The database CHECK requires a final game to have both scores.
        status = "unknown"

    starts_at = parse_datetime(game.get("starts_at") or game.get("scheduled_tipoff_at"))
    return {
        "game_provider_id": str(game_id),
        "season_end_year": int(season),
        "season_type": normalize_season_type(game.get("season_type")),
        "game_date": parse_date(game.get("date"), game.get("starts_at")),
        "scheduled_tipoff_at": starts_at,
        "actual_tipoff_at": None,  # DunkAPI's documented game objects do not expose actual tip-off.
        "game_status": status,
        "home_team_provider_id": str(home_id),
        "away_team_provider_id": str(away_id),
        "home_score": home_score,
        "away_score": away_score,
        "source_provider": "dunkapi",
        "source_updated_at": None,
    }


def team_stats_records(game_id: str, team_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in team_stats or []:
        team_id = row.get("team_id")
        if team_id is None:
            continue
        result.append({
            "game_provider_id": str(game_id),
            "team_provider_id": str(team_id),
            "is_home": bool(row.get("home")),
            "points": _int(row.get("pts")),
            "possessions": None,
            "pace": None,
            "offensive_rating": None,
            "defensive_rating": None,
            "net_rating": None,
            "field_goals_made": _int(row.get("fgm")),
            "field_goals_attempted": _int(row.get("fga")),
            "three_pointers_made": _int(row.get("tpm")),
            "three_pointers_attempted": _int(row.get("tpa")),
            "free_throws_made": _int(row.get("ftm")),
            "free_throws_attempted": _int(row.get("fta")),
            "offensive_rebounds": _int(row.get("oreb")),
            "defensive_rebounds": _int(row.get("dreb")),
            "assists": _int(row.get("ast")),
            "steals": _int(row.get("stl")),
            "blocks": _int(row.get("blk")),
            "turnovers": _int(row.get("tov")),
            "personal_fouls": _int(row.get("pf")),
            "source_available_at": None,
            "raw_stats": row,
        })
    return result


def player_records(players: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    player_rows: list[dict[str, Any]] = []
    stat_rows: list[dict[str, Any]] = []
    for row in players or []:
        player_id = row.get("player_id") or row.get("id")
        if player_id is None:
            continue
        name = str(row.get("name") or row.get("display_name") or player_id).strip()
        parts = name.split()
        first_name = parts[0] if parts else None
        last_name = " ".join(parts[1:]) if len(parts) > 1 else None
        player_rows.append({
            "player_provider_id": str(player_id),
            "display_name": name,
            "first_name": first_name,
            "last_name": last_name,
            "position": row.get("position"),
            "jersey_number": None,
            # Participating in an old game does not prove current active status.
            "is_active": None,
            "source_provider": "dunkapi",
            "source_updated_at": None,
        })
        minutes_value = row.get("minutes")
        minutes = _float_or_none(minutes_value)
        stat_rows.append({
            "player_provider_id": str(player_id),
            "team_provider_id": _str_or_none(row.get("team_id")),
            "minutes": minutes,
            "minutes_raw": None if minutes is not None else _str_or_none(minutes_value),
            "points": _int(row.get("pts")),
            "rebounds": _int(row.get("reb")),
            "offensive_rebounds": _int(row.get("oreb")),
            "defensive_rebounds": _int(row.get("dreb")),
            "assists": _int(row.get("ast")),
            "steals": _int(row.get("stl")),
            "blocks": _int(row.get("blk")),
            "turnovers": _int(row.get("tov")),
            "field_goals_made": _int(row.get("fgm")),
            "field_goals_attempted": _int(row.get("fga")),
            "three_pointers_made": _int(row.get("tpm")),
            "three_pointers_attempted": _int(row.get("tpa")),
            "free_throws_made": _int(row.get("ftm")),
            "free_throws_attempted": _int(row.get("fta")),
            "plus_minus": _int(row.get("plus_minus")),
            "source_available_at": None,
            "raw_stats": row,
        })
    return player_rows, stat_rows


def american_to_decimal(value: Any) -> float | None:
    odds = _float_or_none(value)
    if odds is None or odds == 0:
        return None
    if odds > 0:
        return round(1.0 + odds / 100.0, 6)
    return round(1.0 + 100.0 / abs(odds), 6)


def odds_records(
    game: dict[str, Any],
    books: list[dict[str, Any]],
    fetched_at: datetime | None = None,
) -> list[dict[str, Any]]:
    game_id = str(game.get("game_id") or game.get("id"))
    scheduled = parse_datetime(game.get("starts_at") or game.get("scheduled_tipoff_at"))
    status = normalize_game_status(game.get("status"))
    fetched_at = fetched_at or utc_now()
    rows: list[dict[str, Any]] = []

    for book in books or []:
        sportsbook = str(book.get("provider") or book.get("sportsbook") or "unknown")
        captured = parse_datetime(book.get("captured_at"))
        # Missing source capture timestamps are never promoted to verified PIT.
        timestamp = captured or fetched_at
        if captured is None:
            quality = "unknown"
        elif scheduled and captured < scheduled:
            quality = "verified_point_in_time"
        elif scheduled and captured >= scheduled:
            quality = "postgame_backfill"
        elif status == "final":
            quality = "postgame_backfill"
        else:
            quality = "unknown"

        def add(market: str, selection: str, line: Any, american: Any, phase: str) -> None:
            decimal = american_to_decimal(american)
            if decimal is None or decimal <= 1.0:
                return
            rows.append({
                "game_provider_id": game_id,
                "sportsbook": sportsbook,
                "market_type": market,
                "selection": selection,
                "line_value": _float_or_none(line),
                "decimal_odds": decimal,
                "is_opening": phase == "opening",
                "is_closing": phase == "closing",
                "source_provider": "dunkapi",
                "source_captured_at": timestamp,
                "raw_odds": {"book": book, "phase": phase, "selection": selection},
                "american_odds": _float_or_none(american),
                "price_phase": phase,
                "snapshot_time_quality": quality,
            })

        # Moneyline: the API provides both opening and closing American prices.
        add("moneyline", "home", None, book.get("home_ml_open"), "opening")
        add("moneyline", "away", None, book.get("away_ml_open"), "opening")
        add("moneyline", "home", None, book.get("home_ml_close"), "closing")
        add("moneyline", "away", None, book.get("away_ml_close"), "closing")

        # Spread: the published response supplies a closing line and closing prices.
        # Do not manufacture opening prices from closing prices.
        home_spread = _float_or_none(book.get("home_spread_close"))
        if home_spread is not None:
            add("spread", "home", home_spread, book.get("spread_home_odds"), "closing")
            add("spread", "away", -home_spread, book.get("spread_away_odds"), "closing")

        # Totals: closing prices are published; opening prices are not supplied.
        total_close = _float_or_none(book.get("total_close"))
        if total_close is not None:
            add("total", "over", total_close, book.get("over_odds"), "closing")
            add("total", "under", total_close, book.get("under_odds"), "closing")

    return rows


def payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)
