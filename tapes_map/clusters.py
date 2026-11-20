"""Clusters of nearby points on the 2-D map, labelled by the words that set each one apart."""

import math
from collections import Counter
from typing import Any

import numpy as np
from sklearn.cluster import HDBSCAN

NOISE = -1
LABEL_TERMS = 3
TOP_TERMS = 5


def min_cluster_size(count: int) -> int:
    return max(10, count // 150)


def cluster(coords: np.ndarray) -> np.ndarray:
    """An HDBSCAN cluster id per row, numbered by first appearance in row order; NOISE for outliers.

    HDBSCAN has no randomness, so the same rows in the same order give the same ids.
    """
    count = len(coords)
    size = min_cluster_size(count)
    if count < size:
        return np.full(count, NOISE)
    raw = HDBSCAN(min_cluster_size=size, min_samples=max(5, size // 2), copy=True).fit_predict(coords)
    renumber: dict[int, int] = {}
    for label in raw:
        if label != NOISE and label not in renumber:
            renumber[label] = len(renumber)
    return np.array([renumber.get(label, NOISE) for label in raw])


def class_tfidf(documents: dict[int, list[str]]) -> dict[int, list[tuple[str, float]]]:
    """Class-based TF-IDF: each class's terms ranked by their weight in it against every class.

    `documents` maps a class to the concatenated tokens of its members. A term's weight in a
    class is its frequency there over the class's token count, times log(1 + A / f), where A is
    the average tokens per class and f the term's frequency across all classes (BERTopic's form).
    Ties sort alphabetically.
    """
    counts = {label: Counter(words) for label, words in documents.items()}
    totals = Counter()
    for counter in counts.values():
        totals.update(counter)
    sizes = {label: sum(counter.values()) for label, counter in counts.items()}
    nonempty = [size for size in sizes.values() if size]
    average = sum(nonempty) / len(nonempty) if nonempty else 0
    ranked = {}
    for label, counter in counts.items():
        scores = [(term, count / sizes[label] * math.log(1 + average / totals[term])) for term, count in counter.items()]
        ranked[label] = sorted(scores, key=lambda item: (-item[1], item[0]))
    return ranked


def describe(coords: np.ndarray, labels: np.ndarray, documents: list[list[str]]) -> list[dict[str, Any]]:
    """One entry per cluster, in id order: label, top terms, size and member median.

    Noise members take part in the term frequencies, so words common to the whole map rank low,
    but noise gets no entry.
    """
    grouped: dict[int, list[str]] = {}
    for label, words in zip(labels.tolist(), documents, strict=True):
        grouped.setdefault(label, []).extend(words)
    ranked = class_tfidf(grouped)
    result = []
    for label in sorted(set(labels.tolist()) - {NOISE}):
        members = coords[labels == label]
        terms = [term for term, _ in ranked[label][:TOP_TERMS]]
        x, y = np.median(members, axis=0)
        result.append(
            {
                "id": label,
                "label": " · ".join(terms[:LABEL_TERMS]),
                "terms": terms,
                "size": len(members),
                "x": float(x),
                "y": float(y),
            }
        )
    return result
