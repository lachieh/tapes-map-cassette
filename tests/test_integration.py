import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psycopg
import pytest
from pgvector.psycopg import register_vector

from tapes_map import timeline as timeline_module
from tapes_map.manifest import load_manifest
from tapes_map.projection import build_map
from tapes_map.service import MapState, Timeline, make_server
from tapes_map.store import fetch_centroids, wait_for_db
from tapes_map.timeline import TimelineStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed"),
]

PROVISION = (Path(__file__).resolve().parent.parent / "provision.sql").read_text()
EMBEDDINGS_VIEW = "search_contract_v1.span_embeddings"
SESSIONS = "tapes_v1.sessions"
TURNS = "tapes_v1.span_turns"

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
    id                  UUID PRIMARY KEY,
    harness_id          TEXT NOT NULL,
    harness_session_id  TEXT NOT NULL,
    name                TEXT,
    cwd                 TEXT,
    parent_session_id   UUID,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT '2026-10-31T00:00:00Z',
    last_seen_at        TIMESTAMPTZ NOT NULL,
    ended_at            TIMESTAMPTZ,
    total_input_tokens  BIGINT NOT NULL DEFAULT 0,
    total_output_tokens BIGINT NOT NULL DEFAULT 0,
    total_cost_usd      NUMERIC(12,4) NOT NULL DEFAULT 0,
    turn_count          INT NOT NULL DEFAULT 0,
    derived_status      TEXT NOT NULL DEFAULT 'unknown',
    tool_error_count    INT NOT NULL DEFAULT 0,
    derived_title       TEXT,
    derived_model       TEXT NOT NULL DEFAULT '',
    duration_ns         BIGINT,
    display_name        TEXT
);
CREATE TABLE tapes_v1.span_turns (
    org_id              UUID NOT NULL DEFAULT '00000000-0000-0000-0000-00000000000a',
    trace_id            TEXT NOT NULL,
    session_id          UUID,
    user_prompt         TEXT NOT NULL DEFAULT '',
    synthetic           TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'ok',
    started_at          TIMESTAMPTZ NOT NULL,
    ended_at            TIMESTAMPTZ,
    duration_ns         BIGINT NOT NULL DEFAULT 0,
    total_input_tokens  BIGINT NOT NULL DEFAULT 0,
    total_output_tokens BIGINT NOT NULL DEFAULT 0,
    total_cost_usd      NUMERIC(12,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, trace_id)
);
CREATE INDEX ON tapes_v1.span_turns (session_id, started_at);
CREATE TABLE tapes_v1.spans (
    org_id         UUID NOT NULL DEFAULT '00000000-0000-0000-0000-00000000000a',
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT NOT NULL DEFAULT '',
    session_id     UUID,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'ok',
    call_kind      TEXT NOT NULL DEFAULT '',
    thread_id      TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    started_at     TIMESTAMPTZ NOT NULL,
    duration_ns    BIGINT NOT NULL DEFAULT 0,
    input          JSONB,
    output         JSONB,
    usage          JSONB,
    seq            BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, trace_id, span_id)
);
CREATE INDEX ON tapes_v1.spans (trace_id, span_id);
CREATE TABLE tapes_v1.span_links (
    org_id        UUID NOT NULL DEFAULT '00000000-0000-0000-0000-00000000000a',
    from_trace_id TEXT NOT NULL,
    from_span_id  TEXT NOT NULL,
    from_io       TEXT NOT NULL DEFAULT 'output',
    to_trace_id   TEXT NOT NULL,
    to_span_id    TEXT NOT NULL,
    to_io         TEXT NOT NULL DEFAULT 'input',
    kind          TEXT NOT NULL,
    session_id    UUID
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
        cursor.execute(
            "UPDATE tapes_v1.sessions SET turn_count = 3, total_cost_usd = 0.125, derived_status = 'completed', derived_model = 'opus', parent_session_id = %s WHERE id = %s",
            (ids[1], ids[0]),
        )
        # Session 0's first turn is synthetic and its second has no prompt; the third is its first prompt.
        cursor.executemany(
            "INSERT INTO tapes_v1.span_turns (trace_id, session_id, user_prompt, synthetic, started_at) VALUES (%s, %s, %s, %s, %s)",
            [
                ("p0", ids[0], "summary of earlier work", "post-compaction", datetime(2026, 10, 31, 1, tzinfo=UTC)),
                ("p1", ids[0], "", "", datetime(2026, 10, 31, 2, tzinfo=UTC)),
                ("p2", ids[0], "<system-reminder>\nhidden\n</system-reminder>\nTune the map clusters", "", datetime(2026, 10, 31, 3, tzinfo=UTC)),
                ("p3", ids[0], "Later prompt", "", datetime(2026, 10, 31, 4, tzinfo=UTC)),
            ],
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

    rows = fetch_centroids(map_url, EMBEDDINGS_VIEW, SESSIONS, TURNS)
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
    assert first["prompt"] == "Tune the map clusters"
    assert points[str(ids[1])]["prompt"] == ""
    assert (first["turn_count"], first["cost_usd"], first["status"], first["model"]) == (3, 0.125, "completed", "opus")
    assert first["parent_session_id"] == str(ids[1])
    assert first["started_at"] == "2026-10-31T00:00:00Z"
    assert first["ended_at"] is None
    assert {p["cluster"] for p in result["points"]} <= {c["id"] for c in result["clusters"]} | {-1}

    coords = np.array([[p["x"], p["y"]] for p in result["points"]])
    topic = np.array([int(uuid.UUID(p["session_id"]).int - 1) % 2 for p in result["points"]])
    gap = np.linalg.norm(coords[topic == 0].mean(axis=0) - coords[topic == 1].mean(axis=0))
    assert gap > 0.5


def test_a_missing_view_fails_with_the_postgres_error(provisioned):
    _, url, _ = provisioned
    with pytest.raises(psycopg.errors.UndefinedTable, match="search_contract_v2"):
        fetch_centroids(url, "search_contract_v2.span_embeddings", SESSIONS, TURNS)


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


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


A, B, C, D = (uuid.UUID(int=1000 + n) for n in range(4))
ORG = "00000000-0000-0000-0000-00000000000a"


@pytest.fixture(scope="module")
def timeline_db(provisioned):
    admin_url, map_url, _ = provisioned
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tapes_v1.sessions (id, harness_id, harness_session_id, cwd, parent_session_id, started_at, last_seen_at, ended_at,"
            " derived_title, derived_status, turn_count, total_cost_usd, total_input_tokens, total_output_tokens, tool_error_count)"
            " VALUES (%s, 'claude', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (A, "sess-a", "/src/a", None, at(15, 10), at(15, 12), at(15, 12), "Session A", "completed", 3, 0.5, 1000, 100, 1),
                (B, "sess-b", None, None, at(14, 0), at(14, 1), None, "Before the window", "completed", 1, 0, 0, 0, 0),
                (C, "sess-c", "/src/c", A, at(14, 23), at(15, 0, 30), None, None, "active", 2, 0.25, 10, 1, 0),
                (D, "sess-d", "/src/d", None, at(16, 1), at(16, 2), None, "After the window", "completed", 1, 0, 0, 0, 0),
            ],
        )
        cursor.executemany(
            "INSERT INTO tapes_v1.span_turns (trace_id, session_id, user_prompt, synthetic, status, started_at, ended_at, duration_ns,"
            " total_cost_usd, total_input_tokens, total_output_tokens) VALUES (%s, %s, %s, %s, 'ok', %s, %s, %s, %s, %s, %s)",
            [
                ("ta1", A, "", "post-compaction", at(15, 10), at(15, 10, 5), 300_000_000_000, 0, 0, 0),
                ("ta2", A, "[T3 Code: thread x] Deploy the <b>gateway</b> see https://example.com " + "y" * 200, "", at(15, 11), at(15, 11, 30), 1_800_000_000_000, 0.4, 900, 90),
                ("ta3", A, "And then?", "", at(15, 11, 59), None, 0, 0.1, 100, 10),
                ("tc1", C, "Outside the window", "", at(14, 23, 30), at(14, 23, 40), 0, 0, 0, 0),
                ("tc2", C, "Inside the window", "", at(15, 0, 10), at(15, 0, 20), 0, 0.25, 10, 1),
                ("tb1", B, "Before", "", at(14, 0, 10), None, 0, 0, 0, 0),
            ],
        )
        cursor.executemany(
            "INSERT INTO tapes_v1.spans (trace_id, span_id, parent_span_id, session_id, kind, name, status, call_kind, thread_id, model,"
            " started_at, duration_ns, usage, seq, input, output) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}', '{}')",
            [
                ("ta2", "s1", "", A, "agent", "turn", "ok", "", "main", "", at(15, 11), 5, None, 1),
                ("ta2", "s2", "s1", A, "llm", "messages", "ok", "main", "main", "opus", at(15, 11, 1), 4,
                 json.dumps({"prompt_tokens": 900, "completion_tokens": 90, "total_tokens": 990, "cost_usd": 0.4}), 2),
                ("ta2", "s3", "s1", A, "tool", "Bash", "error", "", "main", "", at(15, 11, 2), 3, None, 3),
                ("ta3", "s1", "", A, "agent", "turn", "ok", "", "main", "", at(15, 11, 59), 1, None, 1),
                ("tc2", "s1", "", C, "agent", "turn", "ok", "", "main", "", at(15, 0, 10), 1, None, 1),
            ],
        )
        cursor.executemany(
            "INSERT INTO tapes_v1.span_links (from_trace_id, from_span_id, to_trace_id, to_span_id, kind, session_id) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                ("ta2", "s2", "ta2", "s3", "tool_call", A),
                ("tc2", "s1", "ta2", "s1", "spawned_by", C),
                ("ta3", "s1", "tc2", "s1", "unrelated", A),
            ],
        )
    return map_url


@pytest.fixture
def served(timeline_db):
    server = make_server("127.0.0.1:0", load_manifest(), MapState(lambda: []), Timeline(TimelineStore(timeline_db)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/map/timeline"
    server.shutdown()


def fetch(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


WINDOW = "?since=2026-11-15T00:00:00Z&until=2026-11-16T00:00:00Z"


def test_timeline_returns_overlapping_sessions_with_their_turns_in_the_window(served):
    status, body = fetch(served + WINDOW)
    assert status == 200
    assert (body["since"], body["until"], body["truncated"]) == ("2026-11-15T00:00:00Z", "2026-11-16T00:00:00Z", False)
    assert [s["session_id"] for s in body["sessions"]] == [str(A), str(C)]
    a, c = body["sessions"]
    assert a == {
        "session_id": str(A),
        "parent_session_id": None,
        "harness_id": "claude",
        "cwd": "/src/a",
        "title": "Session A",
        "started_at": "2026-11-15T10:00:00Z",
        "last_seen_at": "2026-11-15T12:00:00Z",
        "ended_at": "2026-11-15T12:00:00Z",
        "status": "completed",
        "turn_count": 3,
        "cost_usd": 0.5,
        "input_tokens": 1000,
        "output_tokens": 100,
        "tool_error_count": 1,
        "turns": a["turns"],
    }
    assert [t["trace_id"] for t in a["turns"]] == ["ta1", "ta2", "ta3"]
    synthetic, deploy, last = a["turns"]
    assert synthetic["synthetic"] is True and synthetic["prompt"] == ""
    assert deploy == {
        "trace_id": "ta2",
        "started_at": "2026-11-15T11:00:00Z",
        "ended_at": "2026-11-15T11:30:00Z",
        "duration_ns": 1_800_000_000_000,
        "status": "ok",
        "synthetic": False,
        "cost_usd": 0.4,
        "input_tokens": 900,
        "output_tokens": 90,
        "span_count": 3,
        "error_span_count": 1,
        "prompt": deploy["prompt"],
    }
    assert deploy["prompt"].startswith("Deploy the see yyy")
    assert len(deploy["prompt"]) == 160
    assert (last["ended_at"], last["span_count"], last["error_span_count"]) == (None, 1, 0)
    assert c["parent_session_id"] == str(A)
    assert c["title"] == "sess-c"
    assert [t["trace_id"] for t in c["turns"]] == ["tc2"]


def test_timeline_window_edges(served):
    _, body = fetch(served + "?since=2026-11-15T11:30:00Z&until=2026-11-15T11:59:00Z")
    assert [s["session_id"] for s in body["sessions"]] == [str(A)]
    assert [t["trace_id"] for t in body["sessions"][0]["turns"]] == ["ta3"]
    _, body = fetch(served + "?since=2026-11-13T00:00:00Z&until=2026-11-13T23:00:00Z")
    assert body["sessions"] == []


def test_timeline_rejects_bad_windows(served):
    for query in ("?since=2026-11-15T00:00:00Z&until=2026-11-14T00:00:00Z", "?since=2026-09-01T00:00:00Z&until=2026-11-16T00:00:00Z", "?until=soon"):
        status, body = fetch(served + query)
        assert status == 400
        assert list(body) == ["error"]


def test_timeline_caps_sessions_at_the_most_recently_active(served, monkeypatch):
    monkeypatch.setattr(timeline_module, "SESSION_CAP", 1)
    _, body = fetch(served + "?since=2026-11-14T00:00:00Z&until=2026-11-17T00:00:00Z")
    assert body["truncated"] is True
    assert [s["session_id"] for s in body["sessions"]] == [str(D)]


def test_turn_spans_and_links(served):
    status, body = fetch(served + "/turns/ta2")
    assert status == 200
    assert (body["trace_id"], body["session_id"], body["truncated"]) == ("ta2", str(A), False)
    assert [s["span_id"] for s in body["spans"]] == ["s1", "s2", "s3"]
    assert body["spans"][0] == {
        "span_id": "s1",
        "parent_span_id": None,
        "kind": "agent",
        "name": "turn",
        "status": "ok",
        "call_kind": "",
        "thread_id": "main",
        "model": "",
        "started_at": "2026-11-15T11:00:00Z",
        "duration_ns": 5,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }
    llm = body["spans"][1]
    assert (llm["parent_span_id"], llm["model"], llm["input_tokens"], llm["output_tokens"], llm["cost_usd"]) == ("s1", "opus", 900, 90, 0.4)
    assert body["spans"][2]["status"] == "error"
    assert body["links"] == [
        {"kind": "tool_call", "from_trace_id": "ta2", "from_span_id": "s2", "to_trace_id": "ta2", "to_span_id": "s3"},
        {"kind": "spawned_by", "from_trace_id": "tc2", "from_span_id": "s1", "to_trace_id": "ta2", "to_span_id": "s1"},
    ]


def test_a_trace_without_spans_is_404(served):
    status, body = fetch(served + "/turns/nope")
    assert status == 404
    assert list(body) == ["error"]
