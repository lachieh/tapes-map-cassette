import logging
import time
from dataclasses import replace

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

from tapes_map.projection import SessionCentroid

log = logging.getLogger(__name__)

TURNS_VIEW = "tapes_v1.span_turns"

_CENTROIDS = """
WITH centroids AS (
    SELECT session_id, model,
           count(DISTINCT (trace_id, span_id)) AS span_count,
           avg(embedding) AS centroid
    FROM {embeddings}
    WHERE session_id IS NOT NULL
    GROUP BY session_id, model
)
SELECT c.session_id, c.model, c.span_count, c.centroid,
       s.harness_id, s.cwd, s.display_name, s.derived_title, s.name,
       s.harness_session_id, s.last_seen_at, s.started_at, s.ended_at,
       s.turn_count, s.total_cost_usd, s.total_input_tokens, s.total_output_tokens,
       s.tool_error_count, s.derived_status, s.duration_ns, s.derived_model,
       s.parent_session_id
FROM centroids c
JOIN {sessions} s ON s.id = c.session_id
"""

# Each session's first genuine prompt. The (session_id, started_at) index on span_turns
# returns rows already in DISTINCT ON order. tapes stores user_prompt cut to 280 characters.
_FIRST_PROMPTS = """
SELECT DISTINCT ON (session_id) session_id, user_prompt
FROM {turns}
WHERE session_id = ANY(%s) AND coalesce(synthetic, '') = '' AND user_prompt <> ''
ORDER BY session_id, started_at, trace_id
"""


def relation(name: str) -> sql.Composed:
    return sql.SQL(".").join(sql.Identifier(part) for part in name.split("."))


def connect(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url, autocommit=True)
    try:
        register_vector(conn)
    except Exception:
        conn.close()
        raise
    return conn


def fetch_centroids(url: str, embeddings_view: str, sessions_table: str, turns_table: str) -> list[SessionCentroid]:
    query = sql.SQL(_CENTROIDS).format(embeddings=relation(embeddings_view), sessions=relation(sessions_table))
    with connect(url) as conn, conn.cursor(binary=True) as cursor:
        cursor.execute(query)
        rows = [
            SessionCentroid(session_id, model, span_count, centroid.to_numpy(), *session)
            for session_id, model, span_count, centroid, *session in cursor
        ]
        ids = sorted({row.session_id for row in rows})
        cursor.execute(sql.SQL(_FIRST_PROMPTS).format(turns=relation(turns_table)), (ids,))
        prompts = dict(cursor.fetchall())
    return [replace(row, prompt=prompts.get(row.session_id)) for row in rows]


def wait_for_db(url: str, retry: bool, max_delay: float = 30.0) -> None:
    delay = 1.0
    while True:
        try:
            psycopg.connect(url, connect_timeout=10).close()
            return
        except psycopg.OperationalError as exc:
            if not retry:
                raise
            log.warning("database unreachable, retrying in %.0fs: %s", delay, exc)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
