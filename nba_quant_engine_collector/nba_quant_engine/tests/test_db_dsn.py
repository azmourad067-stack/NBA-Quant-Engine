from collector.db import psycopg_compatible_dsn


def test_removes_prisma_pgbouncer_parameter_and_keeps_sslmode():
    dsn = (
        "postgresql://postgres.project:password@pooler.example.com:6543/postgres"
        "?pgbouncer=true&sslmode=require&connect_timeout=20"
    )

    assert psycopg_compatible_dsn(dsn) == (
        "postgresql://postgres.project:password@pooler.example.com:6543/postgres"
        "?sslmode=require&connect_timeout=20"
    )


def test_removes_only_pgbouncer_parameter_case_insensitively():
    dsn = "postgresql://user:password@host:6543/postgres?PGBOUNCER=true&sslmode=require"

    assert psycopg_compatible_dsn(dsn).endswith("?sslmode=require")


def test_keeps_uri_unchanged_when_pgbouncer_parameter_is_absent():
    dsn = "postgresql://user:password@host:5432/postgres?sslmode=require"

    assert psycopg_compatible_dsn(dsn) == dsn


def test_handles_uri_without_query_parameters():
    dsn = "postgresql://user:password@host:5432/postgres"

    assert psycopg_compatible_dsn(dsn) == dsn
