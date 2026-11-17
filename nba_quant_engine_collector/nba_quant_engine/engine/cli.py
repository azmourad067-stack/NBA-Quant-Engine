from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from collector.db import SupabaseDatabase
from .service import build_engine_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and evaluate the NBA pre-game prediction engine.")
    parser.add_argument("--min-train-games", type=int, default=100)
    args = parser.parse_args()
    load_dotenv()
    dsn = os.getenv("SUPABASE_DB_URL", "").strip()
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL is required. No DunkAPI key or collection is needed.")
    schema = os.getenv("NBA_DB_SCHEMA", "nba").strip() or "nba"
    db = SupabaseDatabase(dsn, schema=schema)
    try:
        db.verify_schema()
        report = build_engine_report(db, min_train_games=args.min_train_games)
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
