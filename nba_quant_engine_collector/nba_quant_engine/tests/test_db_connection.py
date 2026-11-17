from collector.db import SupabaseDatabase, supabase_connection_kwargs


POOLER_URI = (
    "postgresql://postgres.gwnpqsbtkubtumrfnspd:secret"
    "@aws-1-eu-west-1.pooler.supabase.com:5432/postgres"
    "?sslmode=require&pgbouncer=true"
    "&hostaddr=aws-1-eu-west-1.pooler.supabase.com"
)


def test_supabase_uri_drops_dns_hostaddr_and_pgbouncer():
    kwargs = supabase_connection_kwargs(POOLER_URI)

    assert kwargs["host"] == "aws-1-eu-west-1.pooler.supabase.com"
    assert kwargs["port"] == "5432"
    assert kwargs["dbname"] == "postgres"
    assert kwargs["user"] == "postgres.gwnpqsbtkubtumrfnspd"
    assert kwargs["sslmode"] == "require"
    assert "hostaddr" not in kwargs
    assert "pgbouncer" not in kwargs


def test_supabase_uri_preserves_other_query_parameters():
    uri = POOLER_URI.replace(
        "&hostaddr=aws-1-eu-west-1.pooler.supabase.com", ""
    ) + "&connect_timeout=17"

    kwargs = supabase_connection_kwargs(uri)

    assert kwargs["connect_timeout"] == "17"
    assert kwargs["sslmode"] == "require"


def test_invalid_uri_fails_without_echoing_credentials():
    try:
        supabase_connection_kwargs("not-a-postgresql-uri")
    except ValueError as exc:
        assert "secret" not in str(exc)
    else:
        raise AssertionError("Invalid URI should be rejected")


def test_database_constructor_explicitly_overrides_hostaddr(monkeypatch):
    captured = {}

    class FakeConnection:
        pass

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr("collector.db.psycopg.connect", fake_connect)

    db = SupabaseDatabase(POOLER_URI)

    assert isinstance(db.conn, FakeConnection)
    assert captured["host"] == "aws-1-eu-west-1.pooler.supabase.com"
    assert captured["hostaddr"] == ""
    assert captured["prepare_threshold"] is None
    assert captured["sslmode"] == "require"


def test_ingestion_finish_and_fail_sql_templates_accept_jsonb_default():
    class FakeConnection:
        def __init__(self):
            self.executed = []
            self.commits = 0

        def execute(self, query, params):
            self.executed.append((query, params))

        def commit(self):
            self.commits += 1

    db = SupabaseDatabase.__new__(SupabaseDatabase)
    db.schema = "nba"
    db.conn = FakeConnection()

    db.finish_ingestion(
        run_id=123,
        records_received=1,
        records_inserted=1,
        records_updated=0,
        credits=2,
        metadata={"collection_id": "test-collection"},
    )
    db.fail_ingestion(
        run_id=124,
        error="test error",
        metadata={"collection_id": "test-collection"},
    )

    assert len(db.conn.executed) == 2
    assert db.conn.commits == 2
