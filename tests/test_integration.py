import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psycopg
import pytest
from pgvector.psycopg import register_vector

from tapes_map.projection import build_map
from tapes_map.store import fetch_centroids, wait_for_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed"),
]

PROVISION = (Path(__file__).resolve().parent.parent / "provision.sql").read_text()
EMBEDDINGS_VIEW = "search_contract_v1.span_embeddings"
SESSIONS = "tapes_v1.sessions"

# search-cassette's table, as its EnsureSchema creates it, and the contract
# view's sessions shape reduced to the columns the map reads.
SCHEMA = """
CREATE EXTENSION vector;
CREATE ROLE cassette_search LOGIN PASSWORD 'search';
CREATE ROLE cassette_map LOGIN PASSWORD 'map';
CREATE SCHEMA search AUTHORIZATION cassette_search;
CREATE TABLE search.span_embeddings (
    trace_id     TEXT NOT NULL,
    span_id      TEXT NOT NULL,
    chunk_idx    INT NOT NULL DEFAULT 0,
    session_id   UUID,
    model        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding    vector(768) NOT NULL,
    PRIMARY KEY (trace_id, span_id, chunk_idx)
);
ALTER TABLE search.span_embeddings OWNER TO cassette_search;
CREATE SCHEMA tapes_v1;
CREATE TABLE tapes_v1.sessions (
    id                 UUID PRIMARY KEY,
    harness_id         TEXT NOT NULL,
    harness_session_id TEXT NOT NULL,
    name               TEXT,
    cwd                TEXT,
    last_seen_at       TIMESTAMPTZ NOT NULL,
    derived_title      TEXT,
    display_name       TEXT
);
"""


@pytest.fixture(scope="module")
def postgres():
    container = subprocess.run(
        ["docker", "run", "-d", "--rm", "-e", "POSTGRES_PASSWORD=postgres", "-p", "127.0.0.1::5432", "pgvector/pgvector:pg17"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        port = subprocess.run(["docker", "port", container, "5432/tcp"], check=True, capture_output=True, text=True).stdout.split(":")[-1].strip()
        url = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
        wait_for_db(url, retry=True, max_delay=1)
        yield f"127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def seed(admin: psycopg.Connection, sessions: int, spans_per_session: int) -> list[uuid.UUID]:
    rng = np.random.default_rng(0)
    topics = rng.normal(size=(2, 768))
    ids = [uuid.UUID(int=n + 1) for n in range(sessions)]
    with admin.cursor() as cursor:
        for n, session_id in enumerate(ids):
            cursor.execute(
                "INSERT INTO tapes_v1.sessions (id, harness_id, harness_session_id, cwd, last_seen_at, derived_title) VALUES (%s, 'claude', %s, '/src', %s, %s)",
                (session_id, f"harness-{n:04d}-session", datetime(2026, 11, 1, tzinfo=UTC), f"topic {n % 2}" if n else None),
            )
            for span in range(spans_per_session):
                cursor.execute(
                    "INSERT INTO search.span_embeddings (trace_id, span_id, chunk_idx, session_id, model, content_hash, embedding) VALUES (%s, %s, 0, %s, 'embeddinggemma', 'h', %s)",
                    (f"t{n}", f"s{span}", session_id, (topics[n % 2] + rng.normal(scale=0.3, size=768)).astype(np.float32)),
                )
        cursor.execute(
            "INSERT INTO search.span_embeddings (trace_id, span_id, chunk_idx, session_id, model, content_hash, embedding) VALUES ('t0', 's0', 1, %s, 'embeddinggemma', 'h', %s)",
            (ids[0], topics[0].astype(np.float32)),
        )
    return ids


@pytest.fixture(scope="module")
def provisioned(postgres):
    admin_url = f"postgresql://postgres:postgres@{postgres}/postgres"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(PROVISION)
        admin.execute(SCHEMA)
        admin.execute(PROVISION)
        admin.execute(PROVISION)
        register_vector(admin)
        ids = seed(admin, sessions=40, spans_per_session=3)
    return admin_url, f"postgresql://cassette_map:map@{postgres}/postgres", ids


def test_the_view_is_definer_rights_and_owned_by_search(provisioned):
    admin_url, _, _ = provisioned
    with psycopg.connect(admin_url) as admin:
        view = admin.execute(
            "SELECT relowner::regrole::text, reloptions FROM pg_class WHERE oid = 'search_contract_v1.span_embeddings'::regclass"
        ).fetchone()
    assert view == ("cassette_search", None)


def test_one_refresh_with_exactly_the_deployments_grants(provisioned):
    _, map_url, ids = provisioned
    with psycopg.connect(map_url) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT 1 FROM search.span_embeddings LIMIT 1")

    rows = fetch_centroids(map_url, EMBEDDINGS_VIEW, SESSIONS)
    result = build_map(rows, datetime.now(UTC))

    assert result["model"] == "embeddinggemma"
    assert result["session_count"] == 40
    points = {p["session_id"]: p for p in result["points"]}
    assert set(points) == {str(i) for i in ids}
    first = points[str(ids[0])]
    assert first["span_count"] == 3
    assert first["title"] == "harness-0000"
    assert points[str(ids[1])]["title"] == "topic 1"
    assert first["last_seen_at"] == "2026-11-01T00:00:00Z"

    coords = np.array([[p["x"], p["y"], p["z"]] for p in result["points"]])
    topic = np.array([int(uuid.UUID(p["session_id"]).int - 1) % 2 for p in result["points"]])
    gap = np.linalg.norm(coords[topic == 0].mean(axis=0) - coords[topic == 1].mean(axis=0))
    assert gap > 0.5


def test_a_missing_view_fails_with_the_postgres_error(provisioned):
    _, url, _ = provisioned
    with pytest.raises(psycopg.errors.UndefinedTable, match="search_contract_v2"):
        fetch_centroids(url, "search_contract_v2.span_embeddings", SESSIONS)


def test_provisioning_survives_a_search_layout_change(provisioned):
    admin_url, _, _ = provisioned
    with psycopg.connect(admin_url) as admin:
        admin.execute("ALTER TABLE search.span_embeddings RENAME COLUMN model TO embedding_model")
        admin.execute(PROVISION)
        owner = admin.execute(
            "SELECT viewowner FROM pg_views WHERE schemaname = 'search_contract_v1' AND viewname = 'span_embeddings'"
        ).fetchone()
        role = admin.execute("SELECT current_user").fetchone()
        admin.rollback()
    assert owner == ("cassette_search",)
    assert role == ("postgres",)
