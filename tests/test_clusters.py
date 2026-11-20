import math
from datetime import UTC, datetime
from uuid import UUID

import numpy as np
import pytest

from tapes_map.clusters import NOISE, class_tfidf, cluster, describe, min_cluster_size
from tapes_map.projection import SessionCentroid, build_map


def blobs(centres, per_blob=40, scale=0.02, seed=0):
    rng = np.random.default_rng(seed)
    return np.vstack([np.array(centre) + rng.normal(scale=scale, size=(per_blob, 2)) for centre in centres])


def test_min_cluster_size_grows_with_the_map():
    assert min_cluster_size(100) == 10
    assert min_cluster_size(3070) == 20
    assert min_cluster_size(15000) == 100


def test_separate_blobs_become_clusters_numbered_in_row_order():
    coords = blobs([(0.5, 0.5), (-0.5, -0.5), (0.5, -0.5)])
    labels = cluster(coords)
    assert labels.tolist() == [0] * 40 + [1] * 40 + [2] * 40


def test_a_point_far_from_every_cluster_is_noise():
    coords = np.vstack([blobs([(0.5, 0.5), (-0.5, -0.5)]), [[4.0, -4.0]]])
    assert cluster(coords)[-1] == NOISE


def test_clustering_is_deterministic():
    coords = blobs([(0.3, 0.1), (-0.2, -0.4), (0.6, -0.5), (-0.6, 0.6)], scale=0.08, seed=3)
    assert np.array_equal(cluster(coords), cluster(coords))


def test_too_few_points_are_all_noise():
    assert cluster(np.zeros((5, 2))).tolist() == [NOISE] * 5
    assert cluster(np.empty((0, 2))).tolist() == []


def test_class_tfidf_matches_the_formula_and_ranks_distinctive_terms_first():
    ranked = class_tfidf({0: ["deploy", "deploy", "shared"], 1: ["bread", "bread", "flour", "shared"]})
    # A = (3 + 4) / 2 tokens per class; "deploy" is 2 of 3 tokens in class 0 and appears twice overall.
    assert ranked[0][0] == ("deploy", pytest.approx(2 / 3 * math.log(1 + 3.5 / 2)))
    assert [term for term, _ in ranked[0]] == ["deploy", "shared"]
    assert [term for term, _ in ranked[1]] == ["bread", "flour", "shared"]


def test_class_tfidf_breaks_ties_alphabetically():
    ranked = class_tfidf({0: ["zeta", "alpha"], 1: ["other"]})
    assert [term for term, _ in ranked[0]] == ["alpha", "zeta"]


def test_describe_labels_each_cluster_with_its_top_terms_and_median():
    coords = blobs([(0.5, 0.5), (-0.5, -0.5)])
    labels = cluster(coords)
    documents = [["kubernetes", "cilium", "gateway", "deploy"]] * 40 + [["sourdough", "bread", "flour", "deploy"]] * 40
    clusters = describe(coords, labels, documents)
    assert [c["id"] for c in clusters] == [0, 1]
    assert clusters[0]["label"] == "cilium · gateway · kubernetes"
    assert clusters[0]["terms"] == ["cilium", "gateway", "kubernetes", "deploy"]
    assert clusters[1]["label"] == "bread · flour · sourdough"
    assert clusters[0]["size"] == 40
    assert (clusters[0]["x"], clusters[0]["y"]) == pytest.approx(np.median(coords[:40], axis=0).tolist())
    assert set(clusters[0]) == {"id", "label", "terms", "size", "x", "y"}


def test_build_map_assigns_clusters_and_labels_them_from_titles_and_prompts():
    rng = np.random.default_rng(1)
    topics = rng.normal(size=(2, 32))
    rows = []
    for n in range(80):
        topic = n % 2
        rows.append(
            SessionCentroid(
                UUID(int=n + 1), "m", 1, (topics[topic] + rng.normal(scale=0.05, size=32)).astype(np.float32),
                "claude", "/src", None, ["Cilium gateway", "Sourdough starter"][topic], None, None,
                datetime(2026, 11, 1, tzinfo=UTC),
                prompt=["Please debug the kubernetes ingress", "Bake bread with rye flour"][topic],
            )
        )
    result = build_map(rows, datetime(2026, 11, 2, tzinfo=UTC))
    assert len(result["clusters"]) == 2
    by_cluster = {}
    for point in result["points"]:
        by_cluster.setdefault(point["cluster"], set()).add(point["title"])
    assert sorted(map(sorted, by_cluster.values())) == [["Cilium gateway"], ["Sourdough starter"]]
    labels = {c["id"]: c["label"] for c in result["clusters"]}
    kube = next(p["cluster"] for p in result["points"] if p["title"] == "Cilium gateway")
    assert set(labels[kube].split(" · ")) <= {"cilium", "gateway", "debug", "kubernetes", "ingress"}
    assert sum(c["size"] for c in result["clusters"]) == 80
