"""AUTH-015 — REAL PostgreSQL end-to-end acceptance (opt-in).

Runs the full credential persistence chain against a REAL PostgreSQL
server:

    startup + schema init
      → admin CRUD (encrypted at rest, redacted responses)
      → restart recovery (fresh app / repository / adapter)
      → OAuth refresh-token rotation persisted (AUTH-014)
      → persistence failure fails closed, then recovers
        (AUTH-014-FIX-01)
      → full restart acceptance cycle

The OAuth token ENDPOINT is simulated with the existing fake HTTP doubles
(no real Google calls); every durable step — repository, encryption,
SQL, restarts — runs against the real database.

Isolation (AUTH-015-FIX-01): the suite NEVER touches the database named
in ``GEMINI_GATEWAY_TEST_DATABASE_URL``.  It derives a DEDICATED test
database (``<dbname>_auth015_e2e``, created automatically when absent)
and all statements — including ``DROP TABLE``/``TRUNCATE`` — execute
only there.  The operator-configured database is only used as the
connection target for creating the test database.

Opt-in: set ``GEMINI_GATEWAY_TEST_DATABASE_URL`` (standard PostgreSQL
DSN).  Skipped — and reported as such — when no server is available.
"""

from __future__ import annotations

import asyncio
import base64
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.credential import CredentialStore
from core.credential_encryption import CredentialEncryptor
from core.credential_postgres import PostgreSQLCredentialRepository

from tests.providers._gemini_cli_fakes import FakeHttp

REAL_DSN = os.environ.get("GEMINI_GATEWAY_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not REAL_DSN,
    reason=(
        "no real PostgreSQL server available; set GEMINI_GATEWAY_TEST_DATABASE_URL "
        "to opt in (this run is reported as an environment limitation, not a PASS)"
    ),
)

TEST_DB_SUFFIX = "_auth015_e2e"

# Dedicated test database DSN, derived by the autouse fixture (None until
# the first test runs; all statements go here, never to REAL_DSN's db).
E2E_DSN: str | None = None

ADMIN = {"Authorization": "Bearer e2e-admin-token"}
ENCRYPTION_KEY = base64.b64encode(os.urandom(32)).decode("ascii")
ENCRYPTION_KEY_BYTES = base64.b64decode(ENCRYPTION_KEY)


def _conninfo_params(dsn: str) -> dict:
    from psycopg.conninfo import conninfo_to_dict

    return conninfo_to_dict(dsn)


def _make_dsn(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params["dbname"] = dbname
    return make_conninfo(**params)


def psql(sql: str, params: tuple | None = None) -> list[dict]:
    """Run one statement directly against the dedicated E2E database."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(E2E_DSN, row_factory=dict_row) as conn:
        cursor = conn.execute(sql, params)
        try:
            return cursor.fetchall()
        except psycopg.ProgrammingError:
            return []


def e2e_env(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", E2E_DSN)
    monkeypatch.setenv("GEMINI_GATEWAY_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("ADMIN_TOKEN", "e2e-admin-token")


@pytest.fixture(autouse=True)
def isolated_database():
    """AUTH-015-FIX-01: derive and create the DEDICATED E2E database, then
    reset its credentials table before every test (schema auto-init by
    app startup remains part of the acceptance).  The operator's own
    database is never written to or dropped."""
    global E2E_DSN
    if not REAL_DSN:
        yield
        return
    import psycopg

    params = _conninfo_params(REAL_DSN)
    base_db = params.get("dbname") or params.get("user") or "postgres"
    test_db = base_db + TEST_DB_SUFFIX
    # connect to the operator-provided database ONLY to ensure the test
    # database exists (CREATE DATABASE cannot run inside a transaction)
    with psycopg.connect(REAL_DSN, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (test_db,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{test_db}"')
    E2E_DSN = _make_dsn(REAL_DSN, test_db)

    # table reset inside the dedicated database only
    psql("DROP TABLE IF EXISTS credentials")
    yield


def make_app(monkeypatch, database_url: str | None = None):
    monkeypatch.setenv("GEMINI_GATEWAY_DATABASE_URL", database_url or E2E_DSN)
    monkeypatch.setenv("GEMINI_GATEWAY_ENCRYPTION_KEY", ENCRYPTION_KEY)
    monkeypatch.setenv("ADMIN_TOKEN", "e2e-admin-token")
    config = {
        "credential_repository": {"backend": "postgres"},
        "providers": {
            "gemini_cli": {
                "enabled": True,
                "resources": [
                    {
                        "id": "cli-1",
                        "refresh_token": "legacy-rt",
                        "client_id": "legacy-cid",
                        "client_secret": "legacy-cs",
                        "project_id": "proj",
                    }
                ],
            }
        },
    }
    return create_app(config, config_path="e2e-nonexistent.yaml")


def make_repo() -> PostgreSQLCredentialRepository:
    return PostgreSQLCredentialRepository(
        __import__("core.credential_postgres", fromlist=["psycopg_connection_factory"])
        .psycopg_connection_factory(E2E_DSN),
        encryptor=CredentialEncryptor(ENCRYPTION_KEY_BYTES),
    )


def rotate_response(token: str, rotated: str | None) -> dict:
    body = {"access_token": token, "expires_in": 3600}
    if rotated is not None:
        body["refresh_token"] = rotated
    return body


def inject_google_http(app, responses: list[dict]) -> FakeHttp:
    """Inject the fake Google token endpoint into the app's provider.

    Must be called BEFORE the provider creates its per-resource adapter
    (set_http_client clears the adapter/client caches)."""
    provider = app.state.scheduler.providers["gemini_cli"]
    http = FakeHttp()
    for body in responses:
        http.responses.append(http.ok(body))
    provider.set_http_client(http)
    return http


# -- 1. PostgreSQL startup ---------------------------------------------------------


def test_startup_initializes_schema_and_binds_durable_repository(monkeypatch):
    app = make_app(monkeypatch)
    repo = app.state.credential_store
    assert isinstance(repo, PostgreSQLCredentialRepository)

    columns = psql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'credentials' ORDER BY ordinal_position"
    )
    assert [c["column_name"] for c in columns] == [
        "id", "type", "payload_encrypted", "created_at", "updated_at",
    ]


def test_startup_failure_when_database_unreachable(monkeypatch):
    import psycopg

    with pytest.raises(psycopg.OperationalError):  # ConnectionTimeout subclass
        make_app(
            monkeypatch,
            database_url=(
                "postgresql://gateway:e2e-secret@127.0.0.1:1/none?connect_timeout=2"
            ),  # closed port, fast timeout
        )  # must raise, never fall back


def test_no_silent_fallback_to_memory_store(monkeypatch):
    app = make_app(monkeypatch)
    # the runtime store is the durable repository, not an empty memory store
    assert not isinstance(app.state.credential_store, CredentialStore)


# -- 2/3. Credential CRUD + encrypted persistence (restart-aware) ---------------------


def make_crud_app(monkeypatch):
    """Minimal app without legacy resources (AUTH-010 migration would add
    its own credential row and pollute the CRUD row-count assertions)."""
    e2e_env(monkeypatch)
    config = {
        "credential_repository": {"backend": "postgres"},
        "providers": {
            "fake": {
                "enabled": True,
                "resources": [{"id": "fake-01", "scenario": "success"}],
            }
        },
    }
    return create_app(config, config_path="e2e-nonexistent.yaml")


def test_crud_round_trip_survives_restart(monkeypatch):
    client_1 = TestClient(make_crud_app(monkeypatch))

    created = client_1.post(
        "/admin/credentials",
        headers=ADMIN,
        json={
            "id": "e2e-oauth-01",
            "type": "oauth",
            "payload": {
                "refresh_token": "rt-plaintext-secret",
                "client_id": "public-cid",
                "client_secret": "cs-plaintext-secret",
            },
        },
    )
    assert created.status_code == 201
    assert "rt-plaintext-secret" not in created.text  # redacted response

    detail = client_1.get("/admin/credentials/e2e-oauth-01", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.json()["payload"]["refresh_token"] == "***"

    patched = client_1.patch(
        "/admin/credentials/e2e-oauth-01",
        headers=ADMIN,
        json={"payload": {"refresh_token": "rt-updated", "client_id": "public-cid"}},
    )
    assert patched.status_code == 200
    assert "rt-updated" not in patched.text

    # encrypted at rest: envelope + kid, no plaintext in the REAL database
    rows = psql("SELECT id, type, payload_encrypted FROM credentials")
    assert len(rows) == 1
    envelope = rows[0]["payload_encrypted"]
    assert envelope["alg"] == "AES-256-GCM"
    assert envelope["kid"] == "default"
    assert set(envelope) == {"v", "alg", "kid", "nonce", "ciphertext"}
    rendered_row = str(rows[0])
    for secret in ("rt-plaintext-secret", "cs-plaintext-secret", "rt-updated"):
        assert secret not in rendered_row

    # RESTART: fresh app over the same database sees the patched credential
    client_2 = TestClient(make_crud_app(monkeypatch))
    after_restart = client_2.get("/admin/credentials/e2e-oauth-01", headers=ADMIN)
    assert after_restart.status_code == 200
    assert after_restart.json()["payload"]["refresh_token"] == "***"
    assert after_restart.json()["payload"]["client_id"] == "public-cid"

    # delete → really gone (even after another restart)
    assert (
        client_2.delete("/admin/credentials/e2e-oauth-01", headers=ADMIN).status_code
        == 204
    )
    client_3 = TestClient(make_crud_app(monkeypatch))
    assert (
        client_3.get("/admin/credentials/e2e-oauth-01", headers=ADMIN).status_code
        == 404
    )
    assert psql("SELECT count(*) AS n FROM credentials")[0]["n"] == 0


# -- 4. refresh token rotation E2E ------------------------------------------------------


def test_rotation_persists_to_real_postgres(monkeypatch):
    """Migration binds the resource → first refresh (rotated) persists to
    the REAL database → restart + provider/adapter rebuild → the NEXT
    refresh uses the rotated token, not the legacy one."""

    async def scenario():
        app = make_app(monkeypatch)
        resource = app.state.scheduler.pools["gemini_cli"].resources[0]
        assert resource.credential_id == "legacy-gemini_cli-cli-1"  # AUTH-010

        repo = make_repo()
        credential = repo.require(resource.credential_id)

        http = inject_google_http(
            app, [rotate_response("at-1", "rotated-rt")]
        )
        provider = app.state.scheduler.providers["gemini_cli"]
        adapter = await provider._auth_adapter_for(resource)
        runtime = await adapter.refresh(credential, resource)
        return app, repo, resource, credential, http, runtime

    app, repo, resource, credential, http, runtime = asyncio.run(scenario())

    assert runtime.headers == {"Authorization": "Bearer at-1"}
    # rotated token durably stored in REAL PostgreSQL (decrypted by repo)
    assert repo.require(resource.credential_id).payload["refresh_token"] == (
        "rotated-rt"
    )
    # raw row still encrypted
    row = psql(
        "SELECT payload_encrypted FROM credentials WHERE id = %s",
        (resource.credential_id,),
    )[0]
    assert "rotated-rt" not in str(row)

    # RESTART: fresh app / provider / adapter over the same database
    async def rebuild():
        app_2 = make_app(monkeypatch)
        resource_2 = app_2.state.scheduler.pools["gemini_cli"].resources[0]
        assert resource_2.credential_id == resource.credential_id
        repo_2 = make_repo()
        credential_2 = repo_2.require(resource_2.credential_id)
        http_2 = inject_google_http(app_2, [rotate_response("at-2", None)])
        provider_2 = app_2.state.scheduler.providers["gemini_cli"]
        adapter_2 = await provider_2._auth_adapter_for(resource_2)
        runtime_2 = await adapter_2.refresh(credential_2, resource_2)
        return http_2, runtime_2

    http_2, runtime_2 = asyncio.run(rebuild())

    # the NEXT refresh used the ROTATED token, not the legacy one
    (refresh_call,) = http_2.post_calls
    assert refresh_call["data"]["refresh_token"] == "rotated-rt"
    assert refresh_call["data"]["refresh_token"] != "legacy-rt"
    assert runtime_2.headers == {"Authorization": "Bearer at-2"}


def test_rotation_without_rotation_response_keeps_credential(monkeypatch):
    async def scenario():
        app = make_app(monkeypatch)
        resource = app.state.scheduler.pools["gemini_cli"].resources[0]
        repo = make_repo()
        credential = repo.require(resource.credential_id)
        inject_google_http(app, [rotate_response("at-1", None)])
        provider = app.state.scheduler.providers["gemini_cli"]
        adapter = await provider._auth_adapter_for(resource)
        await adapter.refresh(credential, resource)
        return repo, resource

    repo, resource = asyncio.run(scenario())
    assert repo.require(resource.credential_id).payload["refresh_token"] == (
        "legacy-rt"
    )


# -- 5. persistence failure (AUTH-014-FIX-01 semantics, real DB failure) -----------------


def test_rotation_persistence_failure_fails_closed_then_recovers(monkeypatch):
    """The credentials table is physically made unavailable (renamed) so
    the rotation persistence fails with a REAL database error; the runtime
    state must stay uncommitted, and the next call re-executes the refresh
    with the OLD refresh token (the uncommitted rotation is never reused).

    The whole scenario runs in ONE event loop (the adapter's asyncio.Lock
    binds to its first loop)."""
    import psycopg

    async def scenario():
        app = make_app(monkeypatch)
        resource = app.state.scheduler.pools["gemini_cli"].resources[0]
        repo = make_repo()
        credential = repo.require(resource.credential_id)
        http = inject_google_http(app, [rotate_response("at-1", None)])
        provider = app.state.scheduler.providers["gemini_cli"]
        adapter = await provider._auth_adapter_for(resource)
        await adapter.refresh(credential, resource)  # baseline, no rotation

        # rotation persistence FAILS: table physically unavailable
        psql("ALTER TABLE credentials RENAME TO credentials_moved")
        try:
            http.responses.append(http.ok(rotate_response("at-2", "rotated-2")))
            failed_exc = None
            try:
                await adapter.refresh(credential, resource)
            except psycopg.errors.UndefinedTable as exc:
                failed_exc = exc
            assert failed_exc is not None
            # the NEW runtime state was not committed (AUTH-014-FIX-01):
            # the baseline token from the earlier successful refresh
            # remains, the failed rotation's token never landed
            assert adapter.auth._access_token == "at-1"
            assert adapter.auth._rotated_refresh_token is None
        finally:
            psql("ALTER TABLE credentials_moved RENAME TO credentials")

        # recovery: next call re-executes the refresh with the OLD refresh
        # token (the failed rotation was never committed/reused)
        http.responses.append(http.ok(rotate_response("at-3", None)))
        runtime = await adapter.refresh(credential, resource)
        assert runtime.headers == {"Authorization": "Bearer at-3"}
        (baseline_call, failed_call, recovered_call) = http.post_calls
        assert failed_call["data"]["refresh_token"] == "legacy-rt"
        assert recovered_call["data"]["refresh_token"] == "legacy-rt"
        # and the repository still decrypts the original durable payload
        assert repo.require(resource.credential_id).payload["refresh_token"] == (
            "legacy-rt"
        )

    asyncio.run(scenario())


def _ok_json(token: str, rotated: str | None) -> dict:
    body = {"access_token": token, "expires_in": 3600}
    if rotated is not None:
        body["refresh_token"] = rotated
    return body


def json_rotate(token: str, rotated: str | None) -> str:
    import json

    return json.dumps(_ok_json(token, rotated))


# -- 6. full restart acceptance cycle ------------------------------------------------------


def test_full_restart_acceptance_cycle(monkeypatch):
    """Full restart acceptance: rotate under app instance #1 → drop that
    app instance (in-process simulated stop: no server-side caching exists)
    → start a fresh app over the same database → rebuild provider and
    adapter from the persisted (rotated) credential → refresh again using
    the rotated token.  The admin API read confirms the rotated payload is
    redacted in responses while the rotated token lives encrypted in the
    dedicated test database."""
    from fastapi.testclient import TestClient as TC

    async def first_phase():
        app = make_app(monkeypatch)
        resource = app.state.scheduler.pools["gemini_cli"].resources[0]
        repo = make_repo()
        credential = repo.require(resource.credential_id)
        inject_google_http(app, [rotate_response("at-1", "rotated-final")])
        provider = app.state.scheduler.providers["gemini_cli"]
        adapter = await provider._auth_adapter_for(resource)
        runtime = await adapter.refresh(credential, resource)
        return runtime

    runtime = asyncio.run(first_phase())
    assert runtime.headers == {"Authorization": "Bearer at-1"}

    # create via the admin API of a fresh app over the same database
    client_1 = TC(make_app(monkeypatch))
    assert client_1.get(
        "/admin/credentials/legacy-gemini_cli-cli-1", headers=ADMIN
    ).json()["payload"]["refresh_token"] == "***"  # redacted, rotated inside

    # (stop gateway: the app object is dropped; nothing is cached server-side)
    del client_1

    # restart + rebuild everything from the database
    async def second_phase():
        app_2 = make_app(monkeypatch)
        resource_2 = app_2.state.scheduler.pools["gemini_cli"].resources[0]
        repo_2 = make_repo()
        credential_2 = repo_2.require(resource_2.credential_id)
        assert credential_2.payload["refresh_token"] == "rotated-final"

        provider_2 = app_2.state.scheduler.providers["gemini_cli"]
        http_2 = inject_google_http(app_2, [rotate_response("at-final", None)])
        adapter_2 = await provider_2._auth_adapter_for(resource_2)
        runtime_2 = await adapter_2.refresh(credential_2, resource_2)
        return http_2, runtime_2

    http_2, runtime_2 = asyncio.run(second_phase())
    assert runtime_2.headers == {"Authorization": "Bearer at-final"}
    (refresh_call,) = http_2.post_calls
    assert refresh_call["data"]["refresh_token"] == "rotated-final"
