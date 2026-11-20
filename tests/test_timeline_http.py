import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from tapes_map import service
from tapes_map.manifest import load_manifest
from tapes_map.service import MapState, Timeline, make_server
from tapes_map.timeline import BadRequest, parse_window

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FakeTimeline:
    def __init__(self):
        self.windows = []
        self.turns = []
        self.fail = None

    def timeline(self, since, until):
        if self.fail:
            raise self.fail
        self.windows.append((since, until))
        return {"since": since.isoformat(), "until": until.isoformat(), "truncated": False, "sessions": [], "calls": len(self.windows)}

    def turn(self, trace_id):
        self.turns.append(trace_id)
        if trace_id == "missing":
            return None
        return {"trace_id": trace_id, "session_id": None, "truncated": False, "spans": [], "links": []}


@pytest.fixture
def timeline():
    source = FakeTimeline()
    server = make_server("127.0.0.1:0", load_manifest(), MapState(lambda: []), Timeline(source, clock=lambda: NOW))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/map/timeline", source
    server.shutdown()


def request(url, **headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_window_defaults_to_the_last_24_hours():
    assert parse_window({}, NOW) == (NOW - timedelta(hours=24), NOW)
    assert parse_window({"until": ["2026-11-10T00:00:00Z"]}, NOW) == (datetime(2026, 11, 9, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC))


def test_window_accepts_offsets_including_an_unescaped_plus():
    since, until = parse_window({"since": ["2026-11-10T10:00:00 10:00"], "until": ["2026-11-10T01:00:00+00:00"]}, NOW)
    assert since == datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    assert until == datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        {"since": ["yesterday"]},
        {"since": ["2026-11-10T00:00:00"]},
        {"since": ["2026-11-10T00:00:00Z"], "until": ["2026-11-10T00:00:00Z"]},
        {"since": ["2026-11-11T00:00:00Z"], "until": ["2026-11-10T00:00:00Z"]},
        {"since": ["2026-10-01T00:00:00Z"], "until": ["2026-11-01T00:00:01Z"]},
    ],
)
def test_bad_windows_are_rejected(query):
    with pytest.raises(BadRequest):
        parse_window(query, NOW)


def test_a_31_day_window_is_allowed():
    assert parse_window({"since": ["2026-10-01T00:00:00Z"], "until": ["2026-11-01T00:00:00Z"]}, NOW)


@pytest.mark.parametrize("query", ["since=nope", "since=2026-11-20T00:00:00Z&until=2026-11-19T00:00:00Z", "since=2026-01-01T00:00:00Z"])
def test_bad_windows_are_400_json(timeline, query):
    url, source = timeline
    status, headers, body = request(f"{url}?{query}")
    assert status == 400
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    assert set(json.loads(body)) == {"error"}
    assert source.windows == []


def test_a_window_touching_now_is_cached_briefly(timeline):
    url, _ = timeline
    status, headers, body = request(url)
    assert status == 200
    assert headers["Cache-Control"] == "private, max-age=15, stale-while-revalidate=60"
    assert headers["ETag"] == f'"{hashlib.sha256(body).hexdigest()[:16]}"'


def test_a_window_ending_five_minutes_ago_counts_as_live(timeline):
    url, _ = timeline
    until = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert request(f"{url}?until={until}")[1]["Cache-Control"] == "private, max-age=15, stale-while-revalidate=60"


def test_a_past_window_is_cached_for_an_hour(timeline):
    url, _ = timeline
    status, headers, _ = request(f"{url}?until=2026-11-19T00:00:00Z")
    assert status == 200
    assert headers["Cache-Control"] == "private, max-age=3600"


def test_repeats_within_the_ttl_reuse_the_response(timeline, monkeypatch):
    url, source = timeline
    first = request(url)
    second = request(url)
    assert first[2] == second[2]
    assert len(source.windows) == 1
    monkeypatch.setattr(service, "RESPONSE_TTL", 0)
    request(f"{url}?until=2026-11-19T00:00:00Z")
    request(f"{url}?until=2026-11-19T00:00:00Z")
    assert len(source.windows) == 3


def test_a_matching_etag_is_304(timeline):
    url, _ = timeline
    etag = request(url)[1]["ETag"]
    status, headers, body = request(url, **{"If-None-Match": f"W/{etag}"})
    assert (status, body) == (304, b"")
    assert headers["ETag"] == etag
    assert headers["Cache-Control"] == "private, max-age=15, stale-while-revalidate=60"


def test_a_database_failure_is_503_and_not_cached(timeline):
    url, source = timeline
    source.fail = RuntimeError("canceling statement due to statement timeout")
    status, headers, body = request(url)
    assert status == 503
    assert "statement timeout" in json.loads(body)["error"]
    assert headers["Cache-Control"] == "no-store"
    source.fail = None
    assert request(url)[0] == 200


def test_turn_spans(timeline):
    url, source = timeline
    status, headers, body = request(url + "/turns/trc_abc%2Bx")
    assert status == 200
    assert headers["Cache-Control"] == "private, max-age=60"
    assert json.loads(body)["trace_id"] == "trc_abc+x"
    etag = headers["ETag"]
    assert request(url + "/turns/trc_abc%2Bx", **{"If-None-Match": etag})[0] == 304
    assert source.turns == ["trc_abc+x"]


def test_a_trace_without_spans_is_404(timeline):
    url, _ = timeline
    status, headers, body = request(url + "/turns/missing")
    assert status == 404
    assert headers["Cache-Control"] == "no-store"
    assert "missing" in json.loads(body)["error"]


@pytest.mark.parametrize("path", ["/turns/", "/turns/a/b", "/other"])
def test_other_timeline_paths_are_404(timeline, path):
    url, source = timeline
    assert request(url + path)[0] == 404
    assert source.turns == []
