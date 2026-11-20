from datetime import UTC, datetime, timedelta

import psycopg

from tapes_map.timeline import TimelineStore

OLD = datetime(2026, 11, 1, tzinfo=UTC)


def turn(trace_id, ended=OLD):
    return (trace_id, "s", "p", None, "ok", OLD, ended, 0, 0, 0, 0)


class FakeCursor:
    def __init__(self, rows=None, cancel=False):
        self.rows = rows or []
        self.cancel = cancel
        self.count_calls = []

    def execute(self, query, params=None):
        if params and "traces" in params:
            self.count_calls.append(list(params["traces"]))
            if self.cancel:
                raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    def __iter__(self):
        return iter(self.rows)


def store():
    return TimelineStore("postgres://unused")


def test_counts_of_finished_turns_are_kept_and_not_recounted():
    s = store()
    first = FakeCursor(rows=[("a", 5, 1)])
    assert s._span_counts(first, [turn("a"), turn("b")]) == {"a": (5, 1), "b": (0, 0)}
    again = FakeCursor(rows=[])
    assert s._span_counts(again, [turn("a")]) == {"a": (5, 1)}
    assert again.count_calls == []


def test_recent_or_open_turns_are_recounted():
    s = store()
    recent = datetime.now(UTC) - timedelta(minutes=1)
    s._span_counts(FakeCursor(rows=[("live", 2, 0), ("new", 3, 0)]), [turn("live", ended=None), turn("new", ended=recent)])
    again = FakeCursor(rows=[("live", 4, 0), ("new", 3, 0)])
    assert s._span_counts(again, [turn("live", ended=None), turn("new", ended=recent)]) == {"live": (4, 0), "new": (3, 0)}
    assert again.count_calls == [["live", "new"]]


def test_a_count_past_its_budget_leaves_counts_out():
    s = store()
    s._span_counts(FakeCursor(rows=[("a", 5, 1)]), [turn("a")])
    assert s._span_counts(FakeCursor(cancel=True), [turn("a"), turn("b")]) == {"a": (5, 1)}
