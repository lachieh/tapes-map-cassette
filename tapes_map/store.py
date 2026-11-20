import logging
import time

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

from tapes_map.projection import SessionCentroid

log = logging.getLogger(__name__)

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
       s.harness_session_id, s.last_seen_at
FROM centroids c
JOIN {sessions} s ON s.id = c.session_id
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


def fetch_centroids(url: str, embeddings_view: str, sessions_table: str) -> list[SessionCentroid]:
    query = sql.SQL(_CENTROIDS).format(embeddings=relation(embeddings_view), sessions=relation(sessions_table))
    with connect(url) as conn, conn.cursor(binary=True) as cursor:
        cursor.execute(query)
        return [
            SessionCentroid(session_id, model, span_count, centroid.to_numpy(), *session)
            for session_id, model, span_count, centroid, *session in cursor
        ]


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
