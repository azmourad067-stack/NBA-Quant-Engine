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


def test_removes_dns_hostname_from_hostaddr_but_keeps_host_and_sslmode():
    dsn = (
        "postgresql://user:password@aws-1-eu-west-1.pooler.supabase.com:6543/postgres"
        "?hostaddr=aws-1-eu-west-1.pooler.supabase.com&sslmode=require"
    )

    assert psycopg_compatible_dsn(dsn) == (
        "postgresql://user:password@aws-1-eu-west-1.pooler.supabase.com:6543/postgres"
        "?sslmode=require"
    )


def test_removes_invalid_hostaddr_and_pgbouncer_together():
    dsn = (
        "postgresql://user:password@pooler.example.com:6543/postgres"
        "?pgbouncer=true&hostaddr=pooler.example.com&sslmode=require"
    )

    assert psycopg_compatible_dsn(dsn) == (
        "postgresql://user:password@pooler.example.com:6543/postgres"
        "?sslmode=require"
    )


def test_keeps_valid_ipv4_hostaddr():
    dsn = (
        "postgresql://user:password@pooler.example.com:6543/postgres"
        "?hostaddr=192.0.2.10&sslmode=require"
    )

    assert psycopg_compatible_dsn(dsn) == dsn


def test_keeps_valid_ipv6_hostaddr():
    dsn = (
        "postgresql://user:password@pooler.example.com:6543/postgres"
        "?hostaddr=%5B2001%3Adb8%3A%3A1%5D&sslmode=require"
    )

    assert psycopg_compatible_dsn(dsn) == dsn


def test_supabase_connection_explicitly_clears_hostaddr(monkeypatch):
    import collector.db as db_module

    captured = {}

    class FakeConnection:
        pass

    def fake_connect(conninfo, **kwargs):
        captured["conninfo"] = conninfo
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr(db_module.psycopg, "connect", fake_connect)

    db_module.SupabaseDatabase(
        "postgresql://user:password@pooler.example.com:6543/postgres"
        "?hostaddr=pooler.example.com&sslmode=require"
    )

    assert "hostaddr=pooler.example.com" not in captured["conninfo"]
    assert captured["kwargs"]["hostaddr"] == ""
    assert captured["kwargs"]["prepare_threshold"] is None
