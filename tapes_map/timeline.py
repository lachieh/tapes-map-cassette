"""Sessions and turns in a time window, and one turn's spans, read from Postgres per request.

Every query is bounded by a LIMIT and filters on an indexed column of the table under the
tapes_v1 view: sessions is small enough to scan, span_turns is read through its
(session_id, started_at) index, and spans and span_links through their trace_id indexes.
"""

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql

from tapes_map.projection import money, optional_rfc3339, rfc3339, session_title
from tapes_map.store import TURNS_VIEW, relation
from tapes_map.text import clean, truncate

DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=31)
SESSION_CAP = 400
TURN_CAP = 2000
SPAN_CAP = 5000
LINK_CAP = 5000
TURN_PROMPT_CHARS = 160
STATEMENT_TIMEOUT_MS = 15000
MAX_CONNECTIONS = 4

SESSIONS_VIEW = "tapes_v1.sessions"
SPANS_VIEW = "tapes_v1.spans"
LINKS_VIEW = "tapes_v1.span_links"


class BadRequest(ValueError):
    pass


_OFFSET_AFTER_SPACE = re.compile(r" (\d{2}:?\d{2})$")


def parse_time(name: str, raw: str) -> datetime:
    # An unencoded "+10:00" offset arrives as " 10:00" after query-string decoding.
    value = _OFFSET_AFTER_SPACE.sub(r"+\1", raw.strip())
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise BadRequest(f"{name} is not an RFC 3339 timestamp: {raw!r}") from None
    if moment.tzinfo is None:
        raise BadRequest(f"{name} needs a time zone offset or Z: {raw!r}")
    return moment.astimezone(UTC)


def parse_window(query: dict[str, list[str]], now: datetime) -> tuple[datetime, datetime]:
    """since and until from the query string: until defaults to now, since to 24 hours before until."""
    until = parse_time("until", query["until"][0]) if query.get("until") else now
    since = parse_time("since", query["since"][0]) if query.get("since") else until - DEFAULT_WINDOW
    if since >= until:
        raise BadRequest("since must be before until")
    if until - since > MAX_WINDOW:
        raise BadRequest(f"the window is longer than {MAX_WINDOW.days} days")
    return since, until


def _number(usage: Any, *keys: str) -> int | float | None:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _tokens(usage: Any, *keys: str) -> int | None:
    value = _number(usage, *keys)
    return int(value) if value is not None else None


def _cost(usage: Any) -> float | None:
    value = _number(usage, "cost_usd", "total_cost_usd")
    return float(value) if value is not None else None


_SESSIONS = """
SELECT id, parent_session_id, harness_id, cwd, display_name, derived_title, name, harness_session_id,
       started_at, last_seen_at, ended_at, derived_status, turn_count, total_cost_usd,
       total_input_tokens, total_output_tokens, tool_error_count
FROM {sessions}
WHERE last_seen_at >= %(since)s AND started_at <= %(until)s
ORDER BY last_seen_at DESC, id DESC
LIMIT %(limit)s
"""

_TURNS = """
SELECT trace_id, session_id, user_prompt, synthetic, status, started_at, ended_at, duration_ns,
       total_cost_usd, total_input_tokens, total_output_tokens
FROM {turns}
WHERE session_id = ANY(%(sessions)s) AND started_at >= %(since)s AND started_at <= %(until)s
ORDER BY started_at DESC, trace_id DESC
LIMIT %(limit)s
"""

_SPAN_COUNTS = """
SELECT trace_id, count(*), count(*) FILTER (WHERE status = 'error')
FROM {spans}
WHERE trace_id = ANY(%(traces)s)
GROUP BY trace_id
"""

_TURN_SPANS = """
SELECT org_id, session_id, span_id, parent_span_id, kind, name, status, call_kind, thread_id, model,
       started_at, duration_ns, usage
FROM {spans}
WHERE trace_id = %(trace)s
ORDER BY started_at, seq, span_id
LIMIT %(limit)s
"""

# The OR becomes a BitmapOr over the (org_id, from_trace_id) and (org_id, to_trace_id) indexes.
_TURN_LINKS = """
SELECT kind, from_trace_id, from_span_id, to_trace_id, to_span_id
FROM {links}
WHERE org_id = ANY(%(orgs)s) AND (from_trace_id = %(trace)s OR to_trace_id = %(trace)s)
ORDER BY from_trace_id, from_span_id, to_trace_id, to_span_id, kind
LIMIT %(limit)s
"""


class TimelineStore:
    """Reads for the timeline endpoints. Safe to call from many request threads at once."""

    def __init__(
        self,
        url: str,
        sessions: str = SESSIONS_VIEW,
        turns: str = TURNS_VIEW,
        spans: str = SPANS_VIEW,
        links: str = LINKS_VIEW,
    ):
        self._url = url
        # Bounds how many requests hold a Postgres connection at once.
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        names = {"sessions": sessions, "turns": turns, "spans": spans, "links": links}
        self._sql = {
            key: sql.SQL(text).format(**{name: relation(value) for name, value in names.items()})
            for key, text in {
                "sessions": _SESSIONS,
                "turns": _TURNS,
                "span_counts": _SPAN_COUNTS,
                "turn_spans": _TURN_SPANS,
                "turn_links": _TURN_LINKS,
            }.items()
        }

    @contextmanager
    def _cursor(self) -> Iterator[psycopg.Cursor]:
        with self._slots, psycopg.connect(
            self._url, autocommit=True, connect_timeout=10, options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
        ) as conn, conn.cursor() as cursor:
            yield cursor

    def timeline(self, since: datetime, until: datetime) -> dict[str, Any]:
        window = {"since": since, "until": until}
        with self._cursor() as cursor:
            cursor.execute(self._sql["sessions"], window | {"limit": SESSION_CAP + 1})
            sessions = cursor.fetchall()
            truncated = len(sessions) > SESSION_CAP
            sessions = sessions[:SESSION_CAP]
            turns = []
            counts = {}
            if sessions:
                cursor.execute(self._sql["turns"], window | {"sessions": [row[0] for row in sessions], "limit": TURN_CAP + 1})
                turns = cursor.fetchall()
                truncated = truncated or len(turns) > TURN_CAP
                turns = turns[:TURN_CAP]
            if turns:
                cursor.execute(self._sql["span_counts"], {"traces": [row[0] for row in turns]})
                counts = {trace: (total, errors) for trace, total, errors in cursor}

        by_session: dict[Any, list[dict[str, Any]]] = {}
        for trace_id, session_id, prompt, synthetic, status, started, ended, duration, cost, inputs, outputs in reversed(turns):
            total, errors = counts.get(trace_id, (0, 0))
            by_session.setdefault(session_id, []).append(
                {
                    "trace_id": trace_id,
                    "started_at": rfc3339(started),
                    "ended_at": optional_rfc3339(ended),
                    "duration_ns": duration or 0,
                    "status": status or "",
                    "synthetic": bool(synthetic),
                    "cost_usd": money(cost),
                    "input_tokens": inputs or 0,
                    "output_tokens": outputs or 0,
                    "span_count": total,
                    "error_span_count": errors,
                    "prompt": truncate(clean(prompt), TURN_PROMPT_CHARS),
                }
            )
        return {
            "since": rfc3339(since),
            "until": rfc3339(until),
            "truncated": truncated,
            "sessions": [
                {
                    "session_id": str(session_id),
                    "parent_session_id": str(parent) if parent else None,
                    "harness_id": harness_id or "",
                    "cwd": cwd or "",
                    "title": session_title(session_id, display_name, derived_title, name, harness_session_id),
                    "started_at": rfc3339(started),
                    "last_seen_at": rfc3339(last_seen),
                    "ended_at": optional_rfc3339(ended),
                    "status": status or "",
                    "turn_count": turn_count or 0,
                    "cost_usd": money(cost),
                    "input_tokens": inputs or 0,
                    "output_tokens": outputs or 0,
                    "tool_error_count": tool_errors or 0,
                    "turns": by_session.get(session_id, []),
                }
                for (
                    session_id, parent, harness_id, cwd, display_name, derived_title, name, harness_session_id,
                    started, last_seen, ended, status, turn_count, cost, inputs, outputs, tool_errors,
                ) in sessions
            ],
        }

    def turn(self, trace_id: str) -> dict[str, Any] | None:
        """The trace's spans and every link with an end in it; None when the trace has no spans."""
        with self._cursor() as cursor:
            cursor.execute(self._sql["turn_spans"], {"trace": trace_id, "limit": SPAN_CAP + 1})
            spans = cursor.fetchall()
            if not spans:
                return None
            orgs = sorted({row[0] for row in spans})
            cursor.execute(self._sql["turn_links"], {"orgs": orgs, "trace": trace_id, "limit": LINK_CAP + 1})
            links = cursor.fetchall()
        truncated = len(spans) > SPAN_CAP or len(links) > LINK_CAP
        spans, links = spans[:SPAN_CAP], links[:LINK_CAP]
        session_id = next((row[1] for row in spans if row[1] is not None), None)
        return {
            "trace_id": trace_id,
            "session_id": str(session_id) if session_id else None,
            "truncated": truncated,
            "spans": [
                {
                    "span_id": span_id,
                    "parent_span_id": parent or None,
                    "kind": kind or "",
                    "name": name or "",
                    "status": status or "",
                    "call_kind": call_kind or "",
                    "thread_id": thread_id or "",
                    "model": model or "",
                    "started_at": rfc3339(started),
                    "duration_ns": duration or 0,
                    "input_tokens": _tokens(usage, "prompt_tokens", "input_tokens"),
                    "output_tokens": _tokens(usage, "completion_tokens", "output_tokens"),
                    "cost_usd": _cost(usage),
                }
                for _, _, span_id, parent, kind, name, status, call_kind, thread_id, model, started, duration, usage in spans
            ],
            "links": [
                {"kind": kind or "", "from_trace_id": from_trace, "from_span_id": from_span, "to_trace_id": to_trace, "to_span_id": to_span}
                for kind, from_trace, from_span, to_trace, to_span in links
            ],
        }
