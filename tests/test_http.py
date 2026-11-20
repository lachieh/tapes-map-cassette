import hashlib
import json
import threading
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import UUID

import numpy as np
import pytest

from tapes_map.manifest import MANIFEST_PATH, load_manifest
from tapes_map.projection import SessionCentroid
from tapes_map.service import MapState, make_server


def rows(count=20):
    rng = np.random.default_rng(0)
    return [
        SessionCentroid(UUID(int=n), "embeddinggemma", 2, rng.normal(size=16).astype(np.float32), "codex", "/w", None, f"session {n}", None, None, datetime(2026, 11, 2, tzinfo=UTC))
        for n in range(count)
    ]


class FakeSource:
    def __init__(self):
        self.result = None

    def __call__(self):
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None:
            raise RuntimeError('relation "search_contract_v1.span_embeddings" does not exist')
        return self.result


@pytest.fixture
def cassette():
    source = FakeSource()
    state = MapState(source)
    server = make_server("127.0.0.1:0", load_manifest(), state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state, source
    server.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.headers["Content-Type"], response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read()


def test_points_are_503_before_the_first_projection(cassette):
    base, _, _ = cassette
    status, content_type, body = get(base + "/api/map/points")
    assert (status, content_type) == (503, "application/json")
    assert "error" in json.loads(body)


def test_first_refresh_failure_is_503_with_the_database_error(cassette):
    base, state, _ = cassette
    state.refresh()
    status, _, body = get(base + "/api/map/points")
    assert status == 503
    assert "search_contract_v1.span_embeddings" in json.loads(body)["error"]


def test_points_after_a_refresh(cassette):
    base, state, source = cassette
    source.result = rows()
    state.refresh()
    status, content_type, body = get(base + "/api/map/points")
    assert (status, content_type) == (200, "application/json")
    result = json.loads(body)
    assert set(result) == {"computed_at", "model", "method", "dimensions", "session_count", "points"}
    assert result["computed_at"].endswith("Z")
    assert (result["model"], result["method"], result["dimensions"], result["session_count"]) == ("embeddinggemma", "umap", 2, 20)
    point = result["points"][0]
    schema = json.loads(get(base + "/openapi")[2])["components"]["schemas"]["MapPoint"]
    assert set(point) == set(schema["required"]) == set(schema["properties"])
    assert all(-1 <= p[axis] <= 1 for p in result["points"] for axis in "xy")
    assert point["title"].startswith("session ")


def test_a_failed_refresh_keeps_serving_the_previous_projection(cassette):
    base, state, source = cassette
    source.result = rows()
    state.refresh()
    before = get(base + "/api/map/points")
    source.result = RuntimeError("connection refused")
    state.refresh()
    assert get(base + "/api/map/points") == before


def request(url, method="GET", **headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers)) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


@pytest.fixture
def refreshed(cassette):
    base, state, source = cassette
    source.result = rows()
    state.refresh()
    return base + "/api/map/points"


def test_points_carry_cache_validators(refreshed):
    status, headers, body = request(refreshed)
    assert status == 200
    assert headers["ETag"] == f'"{hashlib.sha256(body).hexdigest()[:16]}"'
    assert headers["Cache-Control"] == "private, max-age=300, stale-while-revalidate=3600"
    computed_at = datetime.fromisoformat(json.loads(body)["computed_at"])
    assert parsedate_to_datetime(headers["Last-Modified"]) == computed_at.replace(microsecond=0)


@pytest.mark.parametrize("form", ["{etag}", "W/{etag}", '"0000000000000000", {etag}', 'W/"0000000000000000",W/{etag}', "*"])
def test_a_matching_if_none_match_is_304_without_a_body(refreshed, form):
    etag = request(refreshed)[1]["ETag"]
    status, headers, body = request(refreshed, **{"If-None-Match": form.format(etag=etag)})
    assert (status, body) == (304, b"")
    assert headers["ETag"] == etag
    assert headers["Cache-Control"] == "private, max-age=300, stale-while-revalidate=3600"


def test_a_stale_if_none_match_gets_the_new_projection(cassette, refreshed):
    _, state, source = cassette
    old = request(refreshed)[1]["ETag"]
    source.result = rows(21)
    state.refresh()
    status, headers, body = request(refreshed, **{"If-None-Match": f"W/{old}"})
    assert status == 200
    assert headers["ETag"] != old
    assert json.loads(body)["session_count"] == 21


def test_503_is_not_stored(cassette):
    status, headers, _ = request(cassette[0] + "/api/map/points", **{"If-None-Match": "*"})
    assert status == 503
    assert headers["Cache-Control"] == "no-store"
    assert "ETag" not in headers


def test_head_sends_the_get_headers_without_a_body(refreshed):
    _, get_headers, body = request(refreshed)
    status, headers, head_body = request(refreshed, method="HEAD")
    assert (status, head_body) == (200, b"")
    assert headers["ETag"] == get_headers["ETag"]
    assert headers["Content-Length"] == str(len(body))


def test_readyz_waits_for_the_database(cassette):
    base, state, source = cassette
    assert get(base + "/readyz")[0] == 503
    source.result = []
    state.refresh()
    assert get(base + "/readyz")[0] == 200
    assert get(base + "/healthz")[0] == 200


def test_ping(cassette):
    status, _, body = get(cassette[0] + "/ping")
    assert status == 200
    assert json.loads(body) == {"status": "ok", "cassette": "map"}


def test_openapi_embeds_cassette_toml_and_documents_only_prefixed_operations(cassette):
    status, _, body = get(cassette[0] + "/openapi")
    document = json.loads(body)
    assert status == 200
    with MANIFEST_PATH.open("rb") as file:
        assert document["x-tapes-cassette"] == tomllib.load(file)
    assert document["openapi"].startswith("3.")
    assert list(document["paths"]) == ["/api/map/points"]
    operations = [op for item in document["paths"].values() for op in item.values()]
    assert all(op["responses"] for op in operations)
    assert len({op["operationId"] for op in operations}) == len(operations)


def test_unknown_paths_are_404(cassette):
    assert get(cassette[0] + "/api/map/other")[0] == 404
