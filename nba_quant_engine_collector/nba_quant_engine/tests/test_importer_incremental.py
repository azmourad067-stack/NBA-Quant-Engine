from __future__ import annotations

from collector.api import CreditBudget
from collector.importer import NBACollector


class FakeDatabase:
    def __init__(self, missing_ids: set[str]) -> None:
        self.missing_ids = missing_ids
        self.requested_ids: list[str] = []

    def games_missing_details(self, game_ids: list[str]) -> set[str]:
        self.requested_ids = game_ids
        return self.missing_ids


def test_phase_b_only_fetches_box_scores_missing_from_database() -> None:
    games = [
        {"game_provider_id": "game-1", "game_status": "final"},
        {"game_provider_id": "game-2", "game_status": "final"},
        {"game_provider_id": "game-3", "game_status": "scheduled"},
    ]
    db = FakeDatabase({"game-2"})
    collector = NBACollector(api=None, db=db, budget=CreditBudget(120))
    collector.sync_teams = lambda season: 0
    collector.sync_games = lambda *args, **kwargs: games
    fetched: list[str] = []
    collector.sync_game_details = lambda selected: fetched.extend(
        str(game["game_provider_id"]) for game in selected
    )

    report = collector.collect(
        seasons=range(2027, 2028),
        max_games=25,
        include_stats=True,
        include_odds=False,
        status_filter="final",
    )

    assert db.requested_ids == ["game-1", "game-2"]
    assert fetched == ["game-2"]
    assert report["completed_games_for_stats"] == 1
    assert report["games_skipped_existing_details"] == 1
    assert report["games_selected"] == 3
