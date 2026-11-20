from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import numpy as np
import umap

from tapes_map.text import clean, truncate

RANDOM_STATE = 42
DIMENSIONS = 2
POINT_PROMPT_CHARS = 200


@dataclass(frozen=True)
class SessionCentroid:
    session_id: UUID
    model: str
    span_count: int
    centroid: np.ndarray
    harness_id: str | None
    cwd: str | None
    display_name: str | None
    derived_title: str | None
    name: str | None
    harness_session_id: str | None
    last_seen_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    turn_count: int | None = None
    total_cost_usd: Decimal | float | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    tool_error_count: int | None = None
    derived_status: str | None = None
    duration_ns: int | None = None
    derived_model: str | None = None
    parent_session_id: UUID | None = None
    # The session's first genuine user prompt, as stored.
    prompt: str | None = None


def project(centroids: np.ndarray) -> np.ndarray:
    """2-D coordinates in [-1, 1] that keep cosine neighbours of the rows close."""
    count = len(centroids)
    # UMAP needs at least two neighbours per point.
    if count < 3:
        return np.zeros((count, DIMENSIONS))

    norms = np.linalg.norm(centroids, axis=1, keepdims=True)
    unit = centroids / np.where(norms == 0, 1, norms)
    coords = umap.UMAP(
        n_components=DIMENSIONS,
        metric="cosine",
        n_neighbors=min(15, count - 1),
        min_dist=0.1,
        random_state=RANDOM_STATE,
        # A fixed random_state forces single-threaded UMAP; saying so silences its warning.
        n_jobs=1,
        # Pins the layout pipeline the defaults were measured with; umap-learn plans to change the default.
        compatibility_layout=True,
    ).fit_transform(unit)
    coords = coords - coords.mean(axis=0)
    extent = np.abs(coords).max()
    return coords / extent if extent > 0 else coords


def dominant_model(rows: list[SessionCentroid]) -> str:
    """The model that embedded the most spans; ties go to the first name alphabetically."""
    spans = Counter()
    for row in rows:
        spans[row.model] += row.span_count
    return min(spans, key=lambda model: (-spans[model], model))


def session_title(
    session_id: UUID, display_name: str | None, derived_title: str | None, name: str | None, harness_session_id: str | None
) -> str:
    for candidate in (display_name, derived_title, name):
        if candidate and candidate.strip():
            return candidate
    if harness_session_id and harness_session_id.strip():
        return harness_session_id[:12]
    return str(session_id)


def title(row: SessionCentroid) -> str:
    return session_title(row.session_id, row.display_name, row.derived_title, row.name, row.harness_session_id)


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def optional_rfc3339(moment: datetime | None) -> str | None:
    return rfc3339(moment) if moment is not None else None


def money(value: Decimal | float | None) -> float:
    return float(value) if value is not None else 0.0


def build_map(rows: list[SessionCentroid], computed_at: datetime) -> dict[str, Any]:
    model = dominant_model(rows) if rows else ""
    # UMAP's layout depends on input order, so a fixed order keeps refits reproducible.
    chosen = sorted((row for row in rows if row.model == model), key=lambda row: row.session_id)
    coords = project(np.stack([row.centroid for row in chosen]) if chosen else np.empty((0, 2)))
    return {
        "computed_at": rfc3339(computed_at),
        "model": model,
        "method": "umap",
        "dimensions": DIMENSIONS,
        "session_count": len(chosen),
        "points": [
            {
                "session_id": str(row.session_id),
                "x": float(x),
                "y": float(y),
                "span_count": row.span_count,
                "harness_id": row.harness_id or "",
                "cwd": row.cwd or "",
                "title": title(row),
                "prompt": truncate(clean(row.prompt), POINT_PROMPT_CHARS),
                "started_at": optional_rfc3339(row.started_at),
                "last_seen_at": rfc3339(row.last_seen_at),
                "ended_at": optional_rfc3339(row.ended_at),
                "status": row.derived_status or "",
                "model": row.derived_model or "",
                "parent_session_id": str(row.parent_session_id) if row.parent_session_id else None,
                "turn_count": row.turn_count or 0,
                "duration_ns": row.duration_ns or 0,
                "cost_usd": money(row.total_cost_usd),
                "input_tokens": row.total_input_tokens or 0,
                "output_tokens": row.total_output_tokens or 0,
                "tool_error_count": row.tool_error_count or 0,
            }
            for row, (x, y) in zip(chosen, coords, strict=True)
        ],
    }
