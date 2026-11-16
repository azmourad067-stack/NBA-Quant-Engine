# NBA Quant Engine — DunkAPI collector

Python collector that imports DunkAPI NBA teams, schedules/results, full game box scores and sportsbook odds into the existing Supabase PostgreSQL schema `nba`.

## What it writes

- `nba_teams`, `nba_games`
- `nba_team_game_stats`, `nba_players`, `nba_player_game_stats`
- `nba_odds_snapshots`
- `nba_ingestion_runs`, `nba_raw_payloads` for import audit and raw-response retention

The collector validates that the existing tables and required columns are present. It does not create, drop, truncate or alter tables. Imports use idempotent upserts against the primary/unique keys already in the schema. Missing nullable provider fields do not erase previously imported non-null values.

## Credentials

1. Create a DunkAPI key at <https://dunkapi.com/>.
2. In Supabase, open the **Project** project and use **Connect** to copy a PostgreSQL connection URI. Use the Session Pooler URI if your network cannot reach the direct database host over IPv6.
3. Copy `.env.example` to `.env` and set `DUNKAPI_API_KEY` and `SUPABASE_DB_URL`.

`SUPABASE_DB_URL` is the PostgreSQL database connection string containing the database password. It is **not** the Supabase `service_role` API key. Never commit `.env`.

## Install

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

Edit `.env` before running.

## First controlled import

The default cap is 10 games and the default local credit budget is 350 credits. Start with a single season and a small sample:

```bash
python -m collector.cli --season-start 2027 --season-end 2027 --status final --max-games 10
```

`2027` denotes the 2026–27 season (DunkAPI uses the season-ending year). The default `--status final` selects completed games so the first historical import has box scores to collect. Use `--status scheduled` for the upcoming fixture/odds feed; box-score calls are skipped for scheduled games. `--status all` disables the status filter. The collector imports teams, games, per-game team/player box scores for completed games, and the opening/closing odds that the API actually supplies.

Useful options:

```bash
python -m collector.cli --season-start 2025 --season-end 2027 --status final --max-games 25
python -m collector.cli --season-start 2027 --season-end 2027 --status scheduled --max-games 25
python -m collector.cli --season-start 2027 --season-end 2027 --max-games 15 --skip-odds
python -m collector.cli --season-start 2027 --season-end 2027 --max-games 15 --skip-stats
python -m collector.cli --season-start 2027 --season-end 2027 --max-games 10 --max-credits 120
```

## Important data-integrity rules

- Team/game/player identifiers remain DunkAPI provider IDs; names are not used as keys.
- Game season numbers are stored as season-end years.
- Team stats not returned by `get_game` (for example pace, possessions and per-100 ratings) remain `NULL`; the collector does not estimate them from incomplete fields.
- Actual tip-off timestamps are not invented when the provider does not return them.
- Historical sportsbook prices fetched after tip-off are labelled `postgame_backfill`. Records with no trustworthy source capture time are labelled `unknown`. Only captured prices before scheduled tip-off can be labelled `verified_point_in_time`.
- The provider supplies opening and closing moneyline prices. For spread/total, the importer writes the closing prices and closing line fields supplied by the response; it does not manufacture opening prices by reusing closing prices.
- Odds are converted from American to decimal while preserving the original American price.
- API responses are stored in `nba_raw_payloads`; each call is logged in `nba_ingestion_runs`.
- Requests are rate-limited, and a local credit budget stops the run before the next estimated call would exceed the configured limit. DunkAPI may reduce its rate limit when the available credit balance is low.

## Tests

```bash
pytest -q
```

Tests cover the DunkAPI-to-schema mapping, American odds conversion, box-score field mapping, and point-in-time odds classification. They do not contact DunkAPI or Supabase.

## Coverage caveats

DunkAPI's published documentation describes historical games/box scores since 2003–04, while sportsbook odds history begins in 2013–14 and opening/closing lines are available only for subsets/periods. Injury data is captured daily only from the provider's collection launch. Review the live provider data atlas before planning a full-history load: <https://dunkapi.com/data>.
