from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


COLLECTION_ADVISORY_LOCK_KEY = 731904271


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


def supabase_connection_kwargs(database_url: str) -> dict[str, str]:
    """Parse a Supabase PostgreSQL URI into safe Psycopg keyword arguments.

    Supabase pooler hosts are DNS names. A DNS name belongs in host, while
    libpq hostaddr accepts IP literals only. Discard hostaddr from the URI
    and explicitly override PGHOSTADDR at connect time.
    """
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("SUPABASE_DB_URL is empty.")

    parts = urlsplit(database_url.strip())
    if parts.scheme.lower() not in {"postgres", "postgresql"}:
        raise ValueError(
            "SUPABASE_DB_URL must begin with postgresql:// or postgres://."
        )

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("SUPABASE_DB_URL contains an invalid host or port.") from exc

    if not hostname:
        raise ValueError("SUPABASE_DB_URL does not contain a database host.")

    # pgbouncer is not a libpq URI option. hostaddr is intentionally
    # discarded even when it looks like an IP: use the Supabase DNS host.
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"pgbouncer", "hostaddr"}
    ]
    safe_uri = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment)
    )

    try:
        kwargs = conninfo_to_dict(safe_uri)
    except Exception as exc:
        # Never include the DSN in the error: it contains credentials.
        raise ValueError(
            "SUPABASE_DB_URL could not be parsed as a PostgreSQL URI."
        ) from exc

    kwargs.pop("hostaddr", None)
    kwargs.pop("pgbouncer", None)
    kwargs["host"] = hostname
    if port is not None:
        kwargs["port"] = str(port)
    return kwargs

class SupabaseDatabase:
    """Direct PostgreSQL writer. Uses the DB URI, not a Supabase API key."""

    def __init__(self, database_url: str, schema: str = "nba") -> None:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", schema):
            raise ValueError("Invalid PostgreSQL schema name.")
        self.schema = schema
        connection_kwargs = supabase_connection_kwargs(database_url)
        # Explicit empty hostaddr overrides PGHOSTADDR inherited from the
        # Streamlit runtime. libpq then resolves the DNS hostname in host=.
        self.conn = psycopg.connect(
            **connection_kwargs,
            hostaddr="",
            row_factory=dict_row,
            connect_timeout=20,
            prepare_threshold=None,
        )

    def close(self) -> None:
        self.conn.close()

    def try_acquire_collection_lock(self) -> bool:
        """Acquire one database-wide session lock for the whole NBA collection."""
        row = self.conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired",
            (COLLECTION_ADVISORY_LOCK_KEY,),
        ).fetchone()
        self.conn.commit()
        return bool(row and row["acquired"])

    def release_collection_lock(self) -> None:
        """Release the global collection lock; closing the connection also releases it."""
        row = self.conn.execute(
            "SELECT pg_advisory_unlock(%s) AS released",
            (COLLECTION_ADVISORY_LOCK_KEY,),
        ).fetchone()
        self.conn.commit()
        if not row or not row["released"]:
            raise RuntimeError("The NBA collection lock was not held by this database session.")

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

    def games_missing_details(self, game_ids: list[str]) -> set[str]:
        """Return game IDs without both team rows and at least one player stat row."""
        if not game_ids:
            return set()
        rows = self.conn.execute(
            sql.SQL("""select candidate.game_provider_id
                from unnest(%s::text[]) as candidate(game_provider_id)
                where (
                    select count(*) from {}.nba_team_game_stats s
                    where s.game_provider_id = candidate.game_provider_id
                ) < 2
                or not exists (
                    select 1 from {}.nba_player_game_stats s
                    where s.game_provider_id = candidate.game_provider_id
                )""").format(
                sql.Identifier(self.schema),
                sql.Identifier(self.schema),
            ),
            (game_ids,),
        ).fetchall()
        return {str(row["game_provider_id"]) for row in rows}

    def get_game_pagination_state(
        self,
        seasons: list[int],
        season_type: str,
        status_filter: str | None,
    ) -> dict[int, dict[str, Any]]:
        """Load the latest durable list_games cursor for each requested season."""
        if not seasons:
            return {}
        rows = self.conn.execute(
            sql.SQL("""select distinct on ((request_params->>'season')::integer)
                    (request_params->>'season')::integer as season,
                    coalesce((metadata->>'pagination_complete')::boolean, false)
                        as pagination_complete,
                    coalesce(
                        (metadata->>'next_offset')::integer,
                        (request_params->>'offset')::integer + coalesce(records_received, 0)
                    ) as next_offset
                from {}.nba_ingestion_runs
                where endpoint_name = 'list_games'
                  and run_status = 'succeeded'
                  and request_params->>'season' = any(%s::text[])
                  and coalesce(request_params->>'season_type', 'regular') = %s
                  and coalesce(request_params->>'status', 'all') = %s
                order by (request_params->>'season')::integer,
                         started_at desc, ingestion_run_id desc""").format(
                sql.Identifier(self.schema)
            ),
            ([str(int(season)) for season in seasons], season_type, status_filter or "all"),
        ).fetchall()

        return {
            int(row["season"]): {
                "next_offset": int(row["next_offset"] or 0),
                "complete": bool(row["pagination_complete"]),
            }
            for row in rows
        }

    def final_games_missing_details(
        self,
        seasons: list[int],
        season_type: str = "regular",
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """Return the oldest completed games in the requested seasons needing box scores."""
        if not seasons:
            return []
        safe_limit = max(1, min(int(limit), 10000))
        rows = self.conn.execute(
            sql.SQL("""select g.game_provider_id, g.season_end_year, g.season_type,
                           g.game_date, g.game_status
                from {}.nba_games g
                where g.season_end_year = any(%s::smallint[])
                  and g.season_type = %s
                  and lower(g.game_status) = 'final'
                  and (
                    (select count(*) from {}.nba_team_game_stats t
                     where t.game_provider_id = g.game_provider_id) < 2
                    or not exists (
                        select 1 from {}.nba_player_game_stats p
                        where p.game_provider_id = g.game_provider_id
                    )
                  )
                order by g.game_date asc, g.game_provider_id asc
                limit %s""").format(
                sql.Identifier(self.schema),
                sql.Identifier(self.schema),
                sql.Identifier(self.schema),
            ),
            ([int(season) for season in seasons], season_type, safe_limit),
        ).fetchall()
        return [dict(row) for row in rows]

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

    def start_ingestion(
        self, endpoint: str, params: dict[str, Any], collection_id: str | None = None
    ) -> int:
        run_metadata = {"collection_id": collection_id} if collection_id else {}
        row = self.conn.execute(
            sql.SQL("""insert into {}.nba_ingestion_runs
                (source_provider, endpoint_name, request_params, run_status, metadata)
                values ('dunkapi', %s, %s, 'started', %s)
                returning ingestion_run_id""").format(sql.Identifier(self.schema)),
            (endpoint, Jsonb(params), Jsonb(run_metadata)),
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
                    records_updated = %s, credits_estimated = %s, metadata = coalesce(metadata, '{{}}'::jsonb) || %s
                where ingestion_run_id = %s""").format(sql.Identifier(self.schema)),
            (records_received, records_inserted, records_updated, credits, Jsonb(metadata or {}), run_id),
        )
        self.conn.commit()

    def fail_ingestion(self, run_id: int, error: str, metadata: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            sql.SQL("""update {}.nba_ingestion_runs
                set run_status = 'failed', completed_at = now(),
                    error_message = %s,
                    metadata = coalesce(metadata, '{{}}'::jsonb) || %s
                where ingestion_run_id = %s""").format(sql.Identifier(self.schema)),
            (error[:4000], Jsonb(metadata or {}), run_id),
        )
        self.conn.commit()
