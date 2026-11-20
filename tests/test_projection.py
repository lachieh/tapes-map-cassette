from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from tapes_map.projection import SessionCentroid, build_map, project, title

SEEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def two_clusters(per_cluster=60, dims=768, seed=0):
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=(2, dims))
    rows = np.vstack([a + rng.normal(scale=0.3, size=(per_cluster, dims)), b + rng.normal(scale=0.3, size=(per_cluster, dims))])
    return rows.astype(np.float32), np.repeat([0, 1], per_cluster)


def session(n, model="embeddinggemma", **fields):
    defaults = dict(
        session_id=UUID(int=n),
        model=model,
        span_count=1,
        centroid=np.ones(4, dtype=np.float32) * (n + 1),
        harness_id="claude",
        cwd="/src",
        display_name=None,
        derived_title=None,
        name=None,
        harness_session_id=None,
        last_seen_at=SEEN,
    )
    return SessionCentroid(**(defaults | fields))


def test_well_separated_clusters_stay_separated_in_2d():
    vectors, labels = two_clusters()
    coords = project(vectors)
    a, b = coords[labels == 0], coords[labels == 1]
    gap = np.linalg.norm(a.mean(axis=0) - b.mean(axis=0))
    spread = max(np.linalg.norm(a - a.mean(axis=0), axis=1).max(), np.linalg.norm(b - b.mean(axis=0), axis=1).max())
    assert gap > 2 * spread


def test_coordinates_are_centred_and_within_unit_square():
    coords = project(two_clusters()[0])
    assert coords.shape == (120, 2)
    assert np.isclose(np.abs(coords).max(), 1.0)
    assert np.allclose(coords.mean(axis=0), 0, atol=1e-6)


def test_projection_is_reproducible_for_the_same_input():
    vectors = two_clusters()[0]
    assert np.array_equal(project(vectors), project(vectors))


def test_too_few_sessions_for_umap_sit_at_the_origin():
    assert project(np.ones((2, 8))).tolist() == [[0, 0], [0, 0]]
    assert project(np.empty((0, 8))).shape == (0, 2)


def test_map_uses_only_the_model_with_most_spans():
    rows = [session(1, span_count=5), session(2, span_count=5), session(3, model="other", span_count=7)]
    result = build_map(rows, SEEN)
    assert result["model"] == "embeddinggemma"
    assert result["session_count"] == 2
    assert {p["session_id"] for p in result["points"]} == {str(UUID(int=1)), str(UUID(int=2))}


def test_map_point_shape():
    result = build_map([session(1, span_count=3, cwd=None, harness_id=None)], SEEN)
    assert result == {
        "computed_at": "2026-11-01T12:00:00Z",
        "model": "embeddinggemma",
        "method": "umap",
        "dimensions": 2,
        "session_count": 1,
        "points": [
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "x": 0.0,
                "y": 0.0,
                "span_count": 3,
                "harness_id": "",
                "cwd": "",
                "title": "00000000-0000-0000-0000-000000000001",
                "last_seen_at": "2026-11-01T12:00:00Z",
            }
        ],
    }


def test_empty_map():
    assert build_map([], SEEN) == {"computed_at": "2026-11-01T12:00:00Z", "model": "", "method": "umap", "dimensions": 2, "session_count": 0, "points": []}


def test_title_takes_the_first_non_blank_name():
    assert title(session(1, display_name="  ", derived_title="Derived", name="n")) == "Derived"
    assert title(session(1, display_name="Shown", derived_title="Derived")) == "Shown"
    assert title(session(1, name="", harness_session_id="abcdefghijklmnop")) == "abcdefghijkl"
