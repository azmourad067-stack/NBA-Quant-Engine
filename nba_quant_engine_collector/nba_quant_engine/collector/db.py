from __future__ import annotations

import ipaddress
import re
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


EXPECTED_COLUMNS = {
    "nba_teams": {"team_provider_id", "abbreviation", "display_name", "conference", "division", "is_active", "source_provider", "source_updated_at"},
    "nba_games": {"game_provider_id", "season_end_year", "season_type", "game_date", "scheduled_tipoff_at", "game_status", "home_team_provider_id", "away_team_provider_id", "home_score", "away_score"},
    "nba_team_game_stats": {"game_provider_id", "team_provider_id", "is_home", "points", "field_goals_made", "field_goals_attempted", "three_pointers_made", "three_pointers_attempted", "free_throws_made", "free_throws_attempted", "offensive_rebounds", "defensive_rebounds", "assists", "steals", "blocks", "turnovers", "personal_fouls", "raw_stats"},
    "nba_players": {"player_provider_id", "display_name", "first_name", "last_name", "position", "jersey_number", "is_active", "source_provider"},
    "nba_player_game_stats": {"game_provider_id", "player_provider_id", "team_provider_id", "minutes", "minutes_raw", "points", "rebounds", "offensive_rebounds", "defensive_rebounds", "assists", "steals", "blocks", "turnovers", "field_goals_made", "field_goals_attempted", "three_pointers_made", "three_pointers_attempted", "free_throws_made", "free_throws_attempted", "plus_minus", "raw_stats"},
    "nba_odds_snapshots": {"game_provider_id", "sportsbook", "market_type", "selection", "line_value", "decimal_odds", "is_opening", "is_closing", "source_provider", "source_captured_at", "raw_odds", "american_odds", "price_phase", "snapshot_time_quality"},
    "nba_ingestion_runs": {"ingestion_run_id", "source_provider", "endpoint_name", "request_params", "run_status", "started_at", "completed_at", "records_received", "records_inserted", "records_updated", "credits_estimated", "error_message", "metadata"},
    "nba_raw_payloads": {"raw_payload_id", "ingestion_run_id", "source_provider", "endpoint_name", "request_params", "response_payload", "source_as_of_at", "fetched_at", "payload_sha256"},
}

CONFLICT_KEYS = {
    "nba_teams": ["team_provider_id"],
    "nba_games": ["game_provider_id"],
    "nba_team_game_stats": ["game_provider_id", "team_provider_id"],
    "nba_players": ["player_provider_id"],
    "nba_player_game_stats": ["game_provider_id", "player_provider_id"],
    "nba_odds_snapshots": ["game_provider_id", "sportsbook", "market_type", "selection", "line_value", "price_phase", "source_captured_at"],
}


def psycopg_compatible_dsn(database_url: str) -> str:
    """Normalize a Supabase URI for Psycopg/libpq without changing credentials.

    - Remove Prisma's pgbouncer=true flag (not a libpq URI parameter).
    - Keep host= as a DNS name.
    - Keep hostaddr= only when every supplied address is a valid IP literal.
      libpq requires hostaddr to contain IP addresses, never DNS hostnames.
    """
    parts = urlsplit(database_url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    filtered: list[tuple[str, str]] = []

    for key, value in query_items:
        normalized_key = key.lower()
        if normalized_key == "pgbouncer":
            continue
        if normalized_key == "hostaddr":
            addresses = [item.strip().strip("[]") for item in value.split(",")]
            try:
                if not addresses or any(not address for address in addresses):
                    continue
                for address in addresses:
                    ipaddress.ip_address(address)
            except ValueError:
                # A DNS name belongs in host=, not hostaddr=. Let libpq resolve host.
                continue
        filtered.append((key, value))

    if filtered == query_items:
        return database_url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment)
    )

class SupabaseDatabase:
    """Direct PostgreSQL writer. Uses the DB URI, not a Supabase API key."""

    def __init__(self, database_url: str, schema: str = "nba") -> None:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", schema):
            raise ValueError("Invalid PostgreSQL schema name.")
        self.schema = schema
        self.conn = psycopg.connect(
            psycopg_compatible_dsn(database_url),
            row_factory=dict_row,
            connect_timeout=20,
            prepare_threshold=None,
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with self.conn.transaction():
            yield self.conn

    def verify_schema(self) -> None:
        rows = self.conn.execute(
            """select table_name, column_name
               from information_schema.columns
               where table_schema = %s""",
            (self.schema,),
        ).fetchall()
        actual: dict[str, set[str]] = {}
        for row in rows:
            actual.setdefault(row["table_name"], set()).add(row["column_name"])

        problems: list[str] = []
        for table, required in EXPECTED_COLUMNS.items():
            missing_table = table not in actual
            missing_columns = sorted(required - actual.get(table, set()))
            if missing_table:
                problems.append(f"missing table {self.schema}.{table}")
            elif missing_columns:
                problems.append(f"{self.schema}.{table} missing columns: {', '.join(missing_columns)}")
        if problems:
            raise RuntimeError("Database schema validation failed:\n- " + "\n- ".join(problems))

    def upsert_many(self, table: str, records: list[dict[str, Any]]) -> tuple[int, int]:
        if not records:
            return 0, 0
        if table not in CONFLICT_KEYS:
            raise ValueError(f"Upsert not allowed for table {table}.")

        keys = CONFLICT_KEYS[table]
        columns = list(records[0].keys())
        if any(list(record.keys()) != columns for record in records):
            raise ValueError(f"Records for {table} do not share the same ordered column set.")
        if not set(keys).issubset(columns):
            raise ValueError(f"Missing conflict key for {table}.")
        updates = [column for column in columns if column not in keys]
        if not updates:
            raise ValueError(f"No update columns supplied for {table}.")

        query = sql.SQL("insert into {}.{} as existing ({}) values ({}) on conflict ({}) do update set {} returning (xmax = 0) as inserted").format(
            sql.Identifier(self.schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            sql.SQL(", ").join(sql.Identifier(c) for c in keys),
            sql.SQL(", ").join(
                sql.SQL("{} = coalesce(excluded.{}, existing.{})").format(sql.Identifier(c), sql.Identifier(c), sql.Identifier(c)) for c in updates
            ),
        )

        inserted = updated = 0
        with self.conn.transaction():
            for record in records:
                values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in record.values()]
                result = self.conn.execute(query, values).fetchone()
                if result and result["inserted"]:
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated

    def start_ingestion(self, endpoint: str, params: dict[str, Any]) -> int:
        row = self.conn.execute(
            sql.SQL("""insert into {}.nba_ingestion_runs
                (source_provider, endpoint_name, request_params, run_status)
                values ('dunkapi', %s, %s, 'started')
                returning ingestion_run_id""").format(sql.Identifier(self.schema)),
            (endpoint, Jsonb(params)),
        ).fetchone()
        self.conn.commit()
        return int(row["ingestion_run_id"])

    def save_payload(self, run_id: int, endpoint: str, params: dict[str, Any], payload: dict[str, Any], digest: str) -> None:
        self.conn.execute(
            sql.SQL("""insert into {}.nba_raw_payloads
                (ingestion_run_id, source_provider, endpoint_name, request_params,
                 response_payload, source_as_of_at, payload_sha256)
                values (%s, 'dunkapi', %s, %s, %s, null, %s)""").format(sql.Identifier(self.schema)),
            (run_id, endpoint, Jsonb(params), Jsonb(payload), digest),
        )

    def finish_ingestion(
        self,
        run_id: int,
        records_received: int,
        records_inserted: int,
        records_updated: int,
        credits: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            sql.SQL("""update {}.nba_ingestion_runs
                set run_status = 'succeeded', completed_at = now(),
                    records_received = %s, records_inserted = %s,
                    records_updated = %s, credits_estimated = %s, metadata = %s
                where ingestion_run_id = %s""").format(sql.Identifier(self.schema)),
            (records_received, records_inserted, records_updated, credits, Jsonb(metadata or {}), run_id),
        )
        self.conn.commit()

    def fail_ingestion(self, run_id: int, error: str, metadata: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            sql.SQL("""update {}.nba_ingestion_runs
                set run_status = 'failed', completed_at = now(),
                    error_message = %s, metadata = %s
                where ingestion_run_id = %s""").format(sql.Identifier(self.schema)),
            (error[:4000], Jsonb(metadata or {}), run_id),
        )
        self.conn.commit()
