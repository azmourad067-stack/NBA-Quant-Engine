from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from .api import CreditBudget, DunkAPIClient
from .db import SupabaseDatabase
from .mappings import (
    game_record,
    odds_records,
    parse_datetime,
    payload_sha256,
    player_records,
    team_record,
    team_stats_records,
    utc_now,
)


class NBACollector:
    def __init__(
        self, api: DunkAPIClient, db: SupabaseDatabase, budget: CreditBudget,
        collection_id: str | None = None,
    ) -> None:
        self.collection_id = collection_id or str(uuid4())
        self.api = api
        self.db = db
        self.budget = budget
        self.totals = {"teams": 0, "games": 0, "games_pages": 0, "team_stats": 0, "players": 0, "player_stats": 0, "odds": 0}

    def _api_import(
        self,
        endpoint: str,
        params: dict[str, Any],
        writer,
        fallback_credits: int,
    ) -> tuple[dict[str, Any], int]:
        run_id = self.db.start_ingestion(endpoint, params, self.collection_id)
        try:
            data, headers = self.api.post(endpoint, params)
            inserted, updated, received, metadata = writer(data, run_id)
            self.db.save_payload(run_id, endpoint, params, data, payload_sha256(data))
            actual_credits = int(headers.get("x-credits-used", fallback_credits))
            self.db.finish_ingestion(
                run_id,
                records_received=received,
                records_inserted=inserted,
                records_updated=updated,
                credits=actual_credits,
                metadata={**metadata, "collection_id": self.collection_id},
            )
            return data, actual_credits
        except Exception as exc:
            self.db.fail_ingestion(
                run_id, str(exc),
                {"request_params": params, "collection_id": self.collection_id},
            )
            raise

    def sync_teams(self, season: int) -> int:
        def writer(data: dict[str, Any], run_id: int):
            teams = data.get("teams") or []
            records = [record for team in teams if (record := team_record(team))]
            inserted, updated = self.db.upsert_many("nba_teams", records)
            self.totals["teams"] += len(records)
            return inserted, updated, len(teams), {"season": season, "teams_received": len(teams)}

        self._api_import("list_teams", {"season": season, "league": "nba"}, writer, 1)
        return self.totals["teams"]

    def sync_games(
        self,
        seasons: range,
        season_type: str,
        max_games: int,
        status_filter: str | None = "final",
        page_size: int = 100,
        reserve_credits: int = 0,
    ) -> list[dict[str, Any]]:
        """Import game pages and resume each season from its last durable offset."""
        imported: list[dict[str, Any]] = []
        season_list = [int(season) for season in seasons]
        pagination_state = self.db.get_game_pagination_state(
            season_list, season_type, status_filter
        )

        for season in season_list:
            state = pagination_state.get(season, {})
            if state.get("complete"):
                continue
            offset = int(state.get("next_offset") or 0)

            while len(imported) < max_games:
                # Preserve credits for box-score enrichment. The cursor for each
                # successful page is persisted in nba_ingestion_runs metadata.
                if self.budget.remaining - 2 < reserve_credits:
                    return imported[:max_games]

                limit = min(page_size, max_games - len(imported))
                params = {
                    "season": season,
                    "season_type": season_type,
                    "limit": limit,
                    "offset": offset,
                    "league": "nba",
                }
                if status_filter is not None:
                    params["status"] = status_filter

                def writer(data: dict[str, Any], run_id: int):
                    page_games = data.get("games") or []
                    raw_next = data.get("next_offset")
                    try:
                        resolved_next = int(raw_next) if raw_next is not None else None
                    except (TypeError, ValueError):
                        resolved_next = None
                    if resolved_next is None and len(page_games) >= limit:
                        # Defensive fallback for provider responses that omit
                        # next_offset on a full page.
                        resolved_next = offset + len(page_games)
                    pagination_complete = (
                        not page_games
                        or resolved_next is None
                        or resolved_next <= offset
                    )

                    game_rows: list[dict[str, Any]] = []
                    team_rows: dict[str, dict[str, Any]] = {}
                    for game in page_games:
                        home = game.get("home") or {}
                        away = game.get("away") or {}
                        for team in (home, away):
                            record = team_record(team)
                            if record:
                                team_rows[record["team_provider_id"]] = record
                        game_rows.append(game_record(game, fallback_season=season))
                    team_inserted, team_updated = self.db.upsert_many(
                        "nba_teams", list(team_rows.values())
                    )
                    game_inserted, game_updated = self.db.upsert_many("nba_games", game_rows)
                    self.totals["games"] += len(game_rows)
                    self.totals["teams"] += len(team_rows)
                    self.totals["games_pages"] += 1
                    return (
                        team_inserted + game_inserted,
                        team_updated + game_updated,
                        len(page_games),
                        {
                            "season": season,
                            "season_type": season_type,
                            "status_filter": status_filter or "all",
                            "offset": offset,
                            "games_received": len(page_games),
                            "next_offset": None if pagination_complete else resolved_next,
                            "pagination_complete": pagination_complete,
                        },
                    )

                data, _ = self._api_import("list_games", params, writer, 2)
                page = data.get("games") or []
                for game in page:
                    try:
                        imported.append(game_record(game, fallback_season=season))
                    except ValueError:
                        # Malformed entries remain in the raw payload but are not inserted.
                        continue

                raw_next = data.get("next_offset")
                try:
                    next_offset = int(raw_next) if raw_next is not None else None
                except (TypeError, ValueError):
                    next_offset = None
                if next_offset is None and len(page) >= limit:
                    next_offset = offset + len(page)
                if not page or next_offset is None or next_offset <= offset:
                    break
                offset = next_offset

            if len(imported) >= max_games:
                break

        return imported[:max_games]

    def sync_game_details(self, games: list[dict[str, Any]]) -> None:
        for game in games:
            game_id = game["game_provider_id"]
            params = {"game_id": game_id, "league": "nba"}

            def writer(data: dict[str, Any], run_id: int):
                game_obj = data.get("game") or {}
                if game_obj:
                    # Detailed game payload is authoritative for final scores/status.
                    g_record = game_record(game_obj, fallback_season=game.get("season_end_year"))
                    home = game_obj.get("home") or {}
                    away = game_obj.get("away") or {}
                    team_rows = [record for team in (home, away) if (record := team_record(team))]
                    ti, tu = self.db.upsert_many("nba_teams", team_rows)
                    gi, gu = self.db.upsert_many("nba_games", [g_record])
                else:
                    ti = tu = gi = gu = 0

                team_rows_stats = team_stats_records(game_id, data.get("team_stats") or [])
                tsi, tsu = self.db.upsert_many("nba_team_game_stats", team_rows_stats)
                player_rows, stat_rows = player_records(data.get("players") or [])
                pi, pu = self.db.upsert_many("nba_players", player_rows)
                # Attach the game FK after normalizing the raw player lines.
                for row in stat_rows:
                    row["game_provider_id"] = game_id
                psi, psu = self.db.upsert_many("nba_player_game_stats", stat_rows)

                self.totals["team_stats"] += len(team_rows_stats)
                self.totals["players"] += len(player_rows)
                self.totals["player_stats"] += len(stat_rows)
                return (
                    ti + gi + tsi + pi + psi,
                    tu + gu + tsu + pu + psu,
                    len(team_rows_stats) + len(player_rows),
                    {
                        "game_provider_id": game_id,
                        "team_stats_received": len(team_rows_stats),
                        "players_received": len(player_rows),
                    },
                )

            self._api_import("get_game", params, writer, 5)

    def sync_odds(self, games: list[dict[str, Any]]) -> None:
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for game in games:
            by_date[game["game_date"].isoformat()].append(game)

        for game_date, date_games in sorted(by_date.items()):
            if len(date_games) > 1:
                params = {"date": game_date, "league": "nba"}
                fallback_credits = 10
            else:
                params = {"game_id": date_games[0]["game_provider_id"], "league": "nba"}
                fallback_credits = 5

            def writer(data: dict[str, Any], run_id: int):
                odds_games = data.get("games") or []
                if not odds_games and data.get("game_id"):
                    odds_games = [data]
                game_rows: list[dict[str, Any]] = []
                teams: dict[str, dict[str, Any]] = {}
                odds_rows: list[dict[str, Any]] = []
                for odds_game in odds_games:
                    home = odds_game.get("home") or {}
                    away = odds_game.get("away") or {}
                    for team in (home, away):
                        rec = team_record(team)
                        if rec:
                            teams[rec["team_provider_id"]] = rec
                    gid = str(odds_game.get("game_id") or "")
                    if gid:
                        try:
                            game_rows.append(game_record(odds_game))
                        except ValueError:
                            pass
                        odds_rows.extend(odds_records(odds_game, odds_game.get("books") or [], fetched_at=utc_now()))
                ti, tu = self.db.upsert_many("nba_teams", list(teams.values()))
                gi, gu = self.db.upsert_many("nba_games", game_rows)
                oi, ou = self.db.upsert_many("nba_odds_snapshots", odds_rows)
                self.totals["odds"] += len(odds_rows)
                return (
                    ti + gi + oi,
                    tu + gu + ou,
                    len(odds_rows),
                    {"date": game_date, "games_received": len(odds_games), "odds_rows_received": len(odds_rows)},
                )

            self._api_import("get_odds", params, writer, fallback_credits)

    def collect(
        self,
        seasons: range,
        season_type: str = "regular",
        max_games: int = 10,
        include_stats: bool = True,
        include_odds: bool = True,
        status_filter: str | None = "final",
    ) -> dict[str, Any]:
        season_list = list(seasons)
        if not season_list:
            raise ValueError("At least one season is required.")
        self.sync_teams(season_list[-1])
        games = self.sync_games(
            season_list,
            season_type,
            max_games,
            status_filter=status_filter,
            reserve_credits=25 if include_stats else 0,
        )

        completed_games = [game for game in games if game["game_status"] == "final"]
        current_page_missing_ids = self.db.games_missing_details(
            [str(game["game_provider_id"]) for game in completed_games]
        )
        games_skipped_existing_details = max(
            0, len(completed_games) - len(current_page_missing_ids)
        )

        # Select from the full database backlog, not only the most recently
        # listed page. This lets later budget-limited runs keep enriching games
        # imported during earlier runs.
        missing_final_games = self.db.final_games_missing_details(
            season_list, season_type=season_type
        )
        games_to_enrich: list[dict[str, Any]] = []
        games_deferred_due_to_budget = 0
        if include_stats:
            details_capacity = max(0, self.budget.remaining // 5)
            games_to_enrich = missing_final_games[:details_capacity]
            games_deferred_due_to_budget = max(
                0, len(missing_final_games) - len(games_to_enrich)
            )
            if games_to_enrich:
                self.sync_game_details(games_to_enrich)

        if include_odds:
            self.sync_odds(games)

        final_pagination_state = self.db.get_game_pagination_state(
            season_list, season_type, status_filter
        )
        seasons_pending_pagination = [
            season for season in season_list
            if not final_pagination_state.get(season, {}).get("complete", False)
        ]
        final_games_pending_details = max(
            0, len(missing_final_games) - len(games_to_enrich)
        )
        return {
            "collection_id": self.collection_id,
            "season_end_years": season_list,
            "season_type": season_type,
            "status_filter": status_filter or "all",
            "games_selected": len(games),
            "completed_games_for_stats": len(games_to_enrich),
            "games_skipped_existing_details": games_skipped_existing_details,
            "games_deferred_due_to_budget": games_deferred_due_to_budget,
            "final_games_pending_details": final_games_pending_details,
            "seasons_pending_pagination": seasons_pending_pagination,
            "pagination_complete": not seasons_pending_pagination,
            "credits_used_local_estimate": self.budget.used,
            "credits_remaining_in_run_budget": self.budget.remaining,
            **self.totals,
        }
