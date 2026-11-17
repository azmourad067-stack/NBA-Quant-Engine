from __future__ import annotations

from collector.api import CreditBudget
from collector.importer import NBACollector


class FakeDatabase:
    def __init__(
        self,
        missing_ids: set[str],
        pagination_state: dict[int, dict[str, object]] | None = None,
    ) -> None:
        self.missing_ids = missing_ids
        self.requested_ids: list[str] = []
        self.pagination_state = pagination_state or {}

    def games_missing_details(self, game_ids: list[str]) -> set[str]:
        self.requested_ids = game_ids
        return self.missing_ids.intersection(game_ids)

    def get_game_pagination_state(
        self, seasons: list[int], season_type: str, status_filter: str | None
    ) -> dict[int, dict[str, object]]:
        return {
            season: self.pagination_state[season]
            for season in seasons
            if season in self.pagination_state
        }

    def final_games_missing_details(
        self, seasons: list[int], season_type: str = "regular", limit: int = 10000
    ) -> list[dict[str, object]]:
        return [
            {
                "game_provider_id": game_id,
                "season_end_year": seasons[0],
                "season_type": season_type,
                "game_date": "2024-01-01",
                "game_status": "final",
            }
            for game_id in sorted(self.missing_ids)
        ][:limit]

    def upsert_many(self, table: str, records: list[dict[str, object]]) -> tuple[int, int]:
        return len(records), 0


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


def test_phase_b_defers_box_scores_when_remaining_budget_is_insufficient() -> None:
    games = [
        {"game_provider_id": "game-1", "game_status": "final"},
        {"game_provider_id": "game-2", "game_status": "final"},
        {"game_provider_id": "game-3", "game_status": "final"},
    ]
    db = FakeDatabase({"game-1", "game-2", "game-3"})
    budget = CreditBudget(10)
    collector = NBACollector(api=None, db=db, budget=budget)
    collector.sync_teams = lambda season: budget.record(1)
    collector.sync_games = lambda *args, **kwargs: (
        budget.record(2) or games
    )
    fetched: list[str] = []

    def fake_sync_details(selected):
        fetched.extend(str(game["game_provider_id"]) for game in selected)
        budget.record(5 * len(selected))

    collector.sync_game_details = fake_sync_details

    report = collector.collect(
        seasons=range(2024, 2025),
        max_games=25,
        include_stats=True,
        include_odds=False,
        status_filter="final",
    )

    assert fetched == ["game-1"]
    assert report["games_deferred_due_to_budget"] == 2
    assert report["games_skipped_existing_details"] == 0
    assert report["credits_used_local_estimate"] == 8



def test_sync_games_resumes_persisted_offsets_and_moves_to_next_season() -> None:
    db = FakeDatabase(
        set(),
        pagination_state={
            2024: {"next_offset": 20, "complete": False},
            2025: {"next_offset": 0, "complete": False},
        },
    )
    budget = CreditBudget(20)
    collector = NBACollector(api=None, db=db, budget=budget)
    calls: list[tuple[int, int]] = []

    def make_game(game_id: str, season: int) -> dict[str, object]:
        return {
            "game_id": game_id,
            "season": season,
            "season_type": "regular",
            "date": f"{season - 1}-01-01",
            "status": "final",
            "home": {"team_id": "home", "score": 101},
            "away": {"team_id": "away", "score": 99},
        }

    pages = {
        (2024, 20): {
            "games": [make_game("game-2024-a", 2024)],
            "next_offset": 21,
        },
        (2024, 21): {"games": [], "next_offset": None},
        (2025, 0): {
            "games": [make_game("game-2025-a", 2025)],
            "next_offset": None,
        },
    }

    def fake_api_import(endpoint, params, writer, fallback_credits):
        assert endpoint == "list_games"
        key = (int(params["season"]), int(params["offset"]))
        calls.append(key)
        data = pages[key]
        writer(data, 1)
        budget.record(2)
        return data, 2

    collector._api_import = fake_api_import
    result = collector.sync_games(
        range(2024, 2026),
        season_type="regular",
        max_games=10,
        status_filter="final",
        reserve_credits=5,
    )

    assert calls == [(2024, 20), (2024, 21), (2025, 0)]
    assert [game["game_provider_id"] for game in result] == [
        "game-2024-a",
        "game-2025-a",
    ]
    assert collector.totals["games_pages"] == 3
    assert budget.used == 6


def test_sync_games_preserves_reserved_credits_for_box_scores() -> None:
    db = FakeDatabase(set())
    budget = CreditBudget(12)
    collector = NBACollector(api=None, db=db, budget=budget)
    calls: list[int] = []

    def make_game(index: int) -> dict[str, object]:
        return {
            "game_id": f"game-{index}",
            "season": 2024,
            "season_type": "regular",
            "date": "2024-01-01",
            "status": "final",
            "home": {"team_id": "home", "score": 101},
            "away": {"team_id": "away", "score": 99},
        }

    def fake_api_import(endpoint, params, writer, fallback_credits):
        offset = int(params["offset"])
        calls.append(offset)
        data = {
            "games": [make_game(offset + i) for i in range(100)],
            "next_offset": offset + 100,
        }
        writer(data, 1)
        budget.record(2)
        return data, 2

    collector._api_import = fake_api_import
    collector.sync_games(
        range(2024, 2025),
        season_type="regular",
        max_games=500,
        status_filter="final",
        reserve_credits=5,
    )

    assert calls == [0, 100, 200]
    assert budget.used == 6
    assert budget.remaining == 6


def test_boxscore_only_mode_skips_calendar_and_team_calls_and_uses_full_budget() -> None:
    missing_ids = {f"game-{index:02d}" for index in range(27)}
    db = FakeDatabase(missing_ids, pagination_state={
        2024: {"next_offset": 1220, "complete": True},
        2025: {"next_offset": 1230, "complete": True},
        2026: {"next_offset": 1230, "complete": True},
    })
    budget = CreditBudget(120)
    collector = NBACollector(api=None, db=db, budget=budget)

    def should_not_run(*args, **kwargs):
        raise AssertionError("box-score-only mode must not call list_teams or list_games")

    collector.sync_teams = should_not_run
    collector.sync_games = should_not_run
    fetched: list[str] = []

    def fake_sync_details(selected):
        fetched.extend(str(game["game_provider_id"]) for game in selected)
        budget.record(5 * len(selected))
        db.missing_ids.difference_update(fetched)

    collector.sync_game_details = fake_sync_details

    report = collector.collect(
        seasons=range(2024, 2027),
        season_type="regular",
        max_games=5000,
        include_stats=True,
        include_odds=False,
        status_filter="final",
        backfill_details_only=True,
    )

    assert len(fetched) == 24
    assert report["backfill_details_only"] is True
    assert report["games_selected"] == 0
    assert report["games_pages"] == 0
    assert report["games_backlog_before"] == 27
    assert report["completed_games_for_stats"] == 24
    assert report["final_games_pending_details"] == 3
    assert report["pagination_complete"] is True
    assert report["credits_used_local_estimate"] == 120
    assert budget.remaining == 0


def test_boxscore_only_mode_rejects_odds_calls() -> None:
    db = FakeDatabase({"game-1"})
    collector = NBACollector(api=None, db=db, budget=CreditBudget(120))

    try:
        collector.collect(
            seasons=range(2024, 2025),
            max_games=10,
            include_stats=True,
            include_odds=True,
            status_filter="final",
            backfill_details_only=True,
        )
    except ValueError as exc:
        assert "cannot request odds" in str(exc)
    else:
        raise AssertionError("box-score-only mode must reject odds requests")
