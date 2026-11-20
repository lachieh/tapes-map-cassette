import tomllib
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "cassette.toml"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def api_base(manifest: dict[str, Any]) -> str:
    prefix = manifest.get("api", {}).get("prefix_path", "api").strip("/")
    name = manifest["cassette"]["name"]
    return f"/{prefix}/{name}" if prefix else f"/{name}"


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(description: str, schema: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _ref(schema)}}}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties)}


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER = {"type": "number"}
_TIME = {"type": "string", "format": "date-time"}
_UUID = {"type": "string", "format": "uuid"}
_OPTIONAL_TIME = {"type": ["string", "null"], "format": "date-time"}
_OPTIONAL_UUID = {"type": ["string", "null"], "format": "uuid"}

_ERROR = _object({"error": _STRING})

_POINT = _object(
    {
        "session_id": _UUID,
        "x": _NUMBER,
        "y": _NUMBER,
        "cluster": {"type": "integer", "description": "Index into clusters by id; -1 for points in no cluster."},
        "span_count": _INTEGER,
        "harness_id": _STRING,
        "cwd": _STRING,
        "title": _STRING,
        "prompt": {"type": "string", "description": "The first genuine user prompt, cleaned, at most 200 characters."},
        "started_at": _TIME,
        "last_seen_at": _TIME,
        "ended_at": _OPTIONAL_TIME,
        "status": _STRING,
        "model": _STRING,
        "parent_session_id": _OPTIONAL_UUID,
        "turn_count": _INTEGER,
        "duration_ns": _INTEGER,
        "cost_usd": _NUMBER,
        "input_tokens": _INTEGER,
        "output_tokens": _INTEGER,
        "tool_error_count": _INTEGER,
    }
)

_CLUSTER = _object(
    {
        "id": _INTEGER,
        "label": _STRING,
        "terms": {"type": "array", "items": _STRING},
        "size": _INTEGER,
        "x": _NUMBER,
        "y": _NUMBER,
    }
)

_MAP = _object(
    {
        "computed_at": _TIME,
        "model": _STRING,
        "method": {"type": "string", "enum": ["umap"]},
        "dimensions": {"type": "integer", "const": 2},
        "session_count": _INTEGER,
        "clusters": {"type": "array", "items": _ref("MapCluster")},
        "points": {"type": "array", "items": _ref("MapPoint")},
    }
)

_TIMELINE_TURN = _object(
    {
        "trace_id": _STRING,
        "started_at": _TIME,
        "ended_at": _OPTIONAL_TIME,
        "duration_ns": _INTEGER,
        "status": _STRING,
        "synthetic": {"type": "boolean"},
        "cost_usd": _NUMBER,
        "input_tokens": _INTEGER,
        "output_tokens": _INTEGER,
        "span_count": _INTEGER,
        "error_span_count": _INTEGER,
        "prompt": _STRING,
    }
)

_TIMELINE_SESSION = _object(
    {
        "session_id": _UUID,
        "parent_session_id": _OPTIONAL_UUID,
        "harness_id": _STRING,
        "cwd": _STRING,
        "title": _STRING,
        "started_at": _TIME,
        "last_seen_at": _TIME,
        "ended_at": _OPTIONAL_TIME,
        "status": _STRING,
        "turn_count": _INTEGER,
        "cost_usd": _NUMBER,
        "input_tokens": _INTEGER,
        "output_tokens": _INTEGER,
        "tool_error_count": _INTEGER,
        "turns": {"type": "array", "items": _ref("TimelineTurn")},
    }
)

_TIMELINE = _object(
    {
        "since": _TIME,
        "until": _TIME,
        "truncated": {"type": "boolean"},
        "sessions": {"type": "array", "items": _ref("TimelineSession")},
    }
)

_OPTIONAL_INTEGER = {"type": ["integer", "null"]}

_TURN_SPAN = _object(
    {
        "span_id": _STRING,
        "parent_span_id": {"type": ["string", "null"]},
        "kind": _STRING,
        "name": _STRING,
        "status": _STRING,
        "call_kind": _STRING,
        "thread_id": _STRING,
        "model": _STRING,
        "started_at": _TIME,
        "duration_ns": _INTEGER,
        "input_tokens": _OPTIONAL_INTEGER,
        "output_tokens": _OPTIONAL_INTEGER,
        "cost_usd": {"type": ["number", "null"]},
    }
)

_SPAN_LINK = _object(
    {"kind": _STRING, "from_trace_id": _STRING, "from_span_id": _STRING, "to_trace_id": _STRING, "to_span_id": _STRING}
)

_TURN = _object(
    {
        "trace_id": _STRING,
        "session_id": _OPTIONAL_UUID,
        "truncated": {"type": "boolean"},
        "spans": {"type": "array", "items": _ref("TurnSpan")},
        "links": {"type": "array", "items": _ref("SpanLink")},
    }
)

def openapi_document(manifest: dict[str, Any]) -> dict[str, Any]:
    cassette = manifest["cassette"]
    base = api_base(manifest)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": cassette.get("display_name", cassette["name"]),
            "description": cassette.get("description", ""),
            "version": cassette["version"],
        },
        "x-tapes-cassette": manifest,
        "paths": {
            base + "/points": {
                "get": {
                    "operationId": "getMapPoints",
                    "summary": "2-D coordinates and clusters for every session",
                    "description": (
                        "Each session's span embeddings averaged, projected to 2-D with UMAP "
                        "(cosine), and scaled into [-1, 1], then clustered with HDBSCAN and labelled "
                        "by c-TF-IDF over titles and first prompts. Recomputed on an interval, not per request."
                    ),
                    "tags": ["map"],
                    "responses": {
                        "200": _json("The latest projection", "SessionMap"),
                        "304": {"description": "The projection matches an ETag in If-None-Match"},
                        "503": _json("No projection yet: the first refresh is running or failed", "ErrorResponse"),
                    },
                }
            },
            base + "/timeline": {
                "get": {
                    "operationId": "getTimeline",
                    "summary": "Sessions active in a time window, with their turns in it",
                    "description": (
                        "Sessions whose activity overlaps [since, until], most recently active first, at most 400. "
                        "Each carries its turns that started in the window, with span and error-span counts. "
                        "The window defaults to the 24 hours before now and may span at most 31 days."
                    ),
                    "tags": ["timeline"],
                    "parameters": [
                        {"name": "since", "in": "query", "required": False, "schema": _TIME, "description": "Window start; defaults to until minus 24 hours."},
                        {"name": "until", "in": "query", "required": False, "schema": _TIME, "description": "Window end; defaults to now."},
                    ],
                    "responses": {
                        "200": _json("The sessions in the window", "Timeline"),
                        "304": {"description": "The response matches an ETag in If-None-Match"},
                        "400": _json("since or until is not an RFC 3339 timestamp, since is not before until, or the window exceeds 31 days", "ErrorResponse"),
                        "503": _json("The database query failed", "ErrorResponse"),
                    },
                }
            },
            base + "/timeline/turns/{trace_id}": {
                "get": {
                    "operationId": "getTimelineTurn",
                    "summary": "One turn's spans and the links touching it",
                    "tags": ["timeline"],
                    "parameters": [{"name": "trace_id", "in": "path", "required": True, "schema": _STRING}],
                    "responses": {
                        "200": _json("The turn's spans in start order", "Turn"),
                        "304": {"description": "The response matches an ETag in If-None-Match"},
                        "404": _json("The trace has no spans", "ErrorResponse"),
                        "503": _json("The database query failed", "ErrorResponse"),
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "SessionMap": _MAP,
                "MapPoint": _POINT,
                "MapCluster": _CLUSTER,
                "Timeline": _TIMELINE,
                "TimelineSession": _TIMELINE_SESSION,
                "TimelineTurn": _TIMELINE_TURN,
                "Turn": _TURN,
                "TurnSpan": _TURN_SPAN,
                "SpanLink": _SPAN_LINK,
                "ErrorResponse": _ERROR,
            }
        },
    }
