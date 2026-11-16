from __future__ import annotations

import argparse
import json
import logging
import sys

from .api import CreditBudget, DunkAPIClient
from .config import Settings
from .db import SupabaseDatabase
from .importer import NBACollector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import NBA teams, games, box scores and odds from DunkAPI into Supabase schema nba.")
    parser.add_argument("--season-start", type=int, default=2027, help="First season end-year (2027 = 2026-27).")
    parser.add_argument("--season-end", type=int, default=2027, help="Last season end-year inclusive.")
    parser.add_argument("--season-type", choices=["regular", "postseason", "play_in", "preseason"], default="regular")
    parser.add_argument("--status", choices=["final", "scheduled", "all"], default="final", help="Game status filter; final is safest for the first historical import.")
    parser.add_argument("--max-games", type=int, default=10, help="Safety cap on games fetched in one run (default: 10).")
    parser.add_argument("--max-credits", type=int, default=None, help="Override MAX_CREDITS_PER_RUN for this run.")
    parser.add_argument("--skip-stats", action="store_true", help="Import teams and games but skip get_game box-score calls.")
    parser.add_argument("--skip-odds", action="store_true", help="Skip get_odds calls.")
    parser.add_argument("--env-file", default=".env", help="Environment file path (default: .env).")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.season_end < args.season_start:
        logging.error("--season-end must be >= --season-start")
        return 2
    if args.max_games < 1:
        logging.error("--max-games must be >= 1")
        return 2

    try:
        settings = Settings.from_env(args.env_file)
        budget = CreditBudget(args.max_credits or settings.max_credits_per_run)
        api = DunkAPIClient(
            settings.dunkapi_key,
            base_url=settings.dunkapi_base_url,
            timeout=settings.request_timeout_seconds,
            interval_seconds=settings.request_interval_seconds,
            budget=budget,
        )
        db = SupabaseDatabase(settings.supabase_db_url, schema=settings.database_schema)
    except Exception as exc:
        logging.error("Configuration/connection setup failed: %s", exc)
        return 2

    try:
        logging.info("Checking schema %s and existing NBA tables...", settings.database_schema)
        db.verify_schema()
        collector = NBACollector(api, db, budget)
        report = collector.collect(
            seasons=range(args.season_start, args.season_end + 1),
            season_type=args.season_type,
            max_games=args.max_games,
            include_stats=not args.skip_stats,
            include_odds=not args.skip_odds,
            status_filter=None if args.status == "all" else args.status,
        )
        logging.info("Import completed:\n%s", json.dumps(report, indent=2, default=str))
        return 0
    except Exception as exc:
        logging.exception("Import stopped: %s", exc)
        return 1
    finally:
        api.close()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
