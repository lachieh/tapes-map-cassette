import tomllib
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "cassette.toml"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def points_path(manifest: dict[str, Any]) -> str:
    prefix = manifest.get("api", {}).get("prefix_path", "api").strip("/")
    name = manifest["cassette"]["name"]
    return f"/{prefix}/{name}/points" if prefix else f"/{name}/points"


_ERROR = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
}

_POINT = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "format": "uuid"},
        "x": {"type": "number"},
        "y": {"type": "number"},
        "span_count": {"type": "integer"},
        "harness_id": {"type": "string"},
        "cwd": {"type": "string"},
        "title": {"type": "string"},
        "prompt": {"type": "string", "description": "The first genuine user prompt, cleaned, at most 200 characters."},
        "started_at": {"type": "string", "format": "date-time"},
        "last_seen_at": {"type": "string", "format": "date-time"},
        "ended_at": {"type": ["string", "null"], "format": "date-time"},
        "status": {"type": "string"},
        "model": {"type": "string"},
        "parent_session_id": {"type": ["string", "null"], "format": "uuid"},
        "turn_count": {"type": "integer"},
        "duration_ns": {"type": "integer"},
        "cost_usd": {"type": "number"},
        "input_tokens": {"type": "integer"},
        "output_tokens": {"type": "integer"},
        "tool_error_count": {"type": "integer"},
    },
}
_POINT["required"] = list(_POINT["properties"])

_MAP = {
    "type": "object",
    "properties": {
        "computed_at": {"type": "string", "format": "date-time"},
        "model": {"type": "string"},
        "method": {"type": "string", "enum": ["umap"]},
        "dimensions": {"type": "integer", "const": 2},
        "session_count": {"type": "integer"},
        "points": {"type": "array", "items": {"$ref": "#/components/schemas/MapPoint"}},
    },
    "required": ["computed_at", "model", "method", "dimensions", "session_count", "points"],
}


def openapi_document(manifest: dict[str, Any]) -> dict[str, Any]:
    cassette = manifest["cassette"]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": cassette.get("display_name", cassette["name"]),
            "description": cassette.get("description", ""),
            "version": cassette["version"],
        },
        "x-tapes-cassette": manifest,
        "paths": {
            points_path(manifest): {
                "get": {
                    "operationId": "getMapPoints",
                    "summary": "2-D coordinates for every session",
                    "description": (
                        "Each session's span embeddings averaged, projected to 2-D with UMAP "
                        "(cosine), and scaled into [-1, 1]. Recomputed on an interval, not per request."
                    ),
                    "tags": ["map"],
                    "responses": {
                        "200": {
                            "description": "The latest projection",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SessionMap"}}},
                        },
                        "304": {"description": "The projection matches an ETag in If-None-Match"},
                        "503": {
                            "description": "No projection yet: the first refresh is running or failed",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
                        },
                    },
                }
            }
        },
        "components": {"schemas": {"SessionMap": _MAP, "MapPoint": _POINT, "ErrorResponse": _ERROR}},
    }
