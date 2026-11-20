import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from datetime import timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote

from tapes_map.manifest import api_base, openapi_document
from tapes_map.projection import SessionCentroid, build_map
from tapes_map.timeline import BadRequest, parse_window

log = logging.getLogger(__name__)

POINTS_CACHE_CONTROL = "private, max-age=300, stale-while-revalidate=3600"
LIVE_TIMELINE_CACHE_CONTROL = "private, max-age=15, stale-while-revalidate=60"
PAST_TIMELINE_CACHE_CONTROL = "private, max-age=3600"
TURN_CACHE_CONTROL = "private, max-age=60"
NO_STORE = {"Cache-Control": "no-store"}
# A window ending this close to now can still gain turns, so clients revalidate it soon.
LIVE_EDGE = timedelta(minutes=5)
RESPONSE_TTL = 15.0
RESPONSE_CACHE_ENTRIES = 256


def etag_matches(if_none_match: str, etag: str) -> bool:
    """Weak comparison (RFC 9110 13.1.2): proxies that compress, such as Cloudflare and Envoy, weaken ETags."""
    tags = [tag.strip() for tag in if_none_match.split(",")]
    return any(tag == "*" or tag.removeprefix("W/") == etag for tag in tags)


def etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:16]}"'


def conditional(body: bytes, headers: dict[str, str], if_none_match: str | None) -> tuple[int, bytes, dict[str, str]]:
    """200 with the body, or 304 with only the validators when If-None-Match matches its ETag."""
    if if_none_match is not None and etag_matches(if_none_match, headers["ETag"]):
        return 304, b"", {"ETag": headers["ETag"], "Cache-Control": headers["Cache-Control"]}
    return 200, body, headers


def error_response(status: int, message: str) -> tuple[int, bytes, dict[str, str]]:
    return status, json.dumps({"error": message}).encode(), NO_STORE


class MapState:
    """The latest projection, replaced whole by each successful refresh."""

    def __init__(self, fetch: Callable[[], list[SessionCentroid]]):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._body: bytes | None = None
        self._headers: dict[str, str] = {}
        self._error = "the first projection has not finished"
        self.db_reached = False

    def refresh(self) -> None:
        started = time.monotonic()
        try:
            rows = self._fetch()
            self.db_reached = True
            fetched = time.monotonic()
            computed_at = datetime.now(UTC)
            result = build_map(rows, computed_at)
            body = json.dumps(result).encode()
        except Exception as exc:
            log.exception("refresh failed")
            with self._lock:
                self._error = f"refresh failed: {exc}"
            return
        headers = {
            "ETag": etag(body),
            "Last-Modified": format_datetime(computed_at, usegmt=True),
            "Cache-Control": POINTS_CACHE_CONTROL,
        }
        with self._lock:
            self._body = body
            self._headers = headers
        if skipped := len(rows) - result["session_count"]:
            log.warning("left out %d session embeddings from models other than %s", skipped, result["model"])
        log.info(
            "refreshed %d sessions: query %.1fs, projection %.1fs",
            result["session_count"],
            fetched - started,
            time.monotonic() - fetched,
        )

    def points(self, if_none_match: str | None = None) -> tuple[int, bytes, dict[str, str]]:
        with self._lock:
            body, headers, error = self._body, self._headers, self._error
        if body is None:
            return 503, json.dumps({"error": error}).encode(), NO_STORE
        return conditional(body, headers, if_none_match)

    def run(self, interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh()
            stop.wait(interval)


class TimelineSource(Protocol):
    def timeline(self, since: datetime, until: datetime) -> dict[str, Any]: ...

    def turn(self, trace_id: str) -> dict[str, Any] | None: ...


class Timeline:
    """The timeline endpoints: a database read per request, shared for RESPONSE_TTL seconds per query."""

    def __init__(self, source: TimelineSource, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._source = source
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, bytes, dict[str, str]]] = {}

    def _cached(self, key: str, load: Callable[[], tuple[dict[str, Any], str] | None]) -> tuple[bytes, dict[str, str]] | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2]
        loaded = load()
        if loaded is None:
            return None
        result, cache_control = loaded
        body = json.dumps(result).encode()
        headers = {"ETag": etag(body), "Cache-Control": cache_control}
        with self._lock:
            for stale in [k for k, (expires, _, _) in self._cache.items() if expires <= now]:
                del self._cache[stale]
            if len(self._cache) >= RESPONSE_CACHE_ENTRIES:
                self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]))
            self._cache[key] = (now + RESPONSE_TTL, body, headers)
        return body, headers

    def window(self, query: str, if_none_match: str | None) -> tuple[int, bytes, dict[str, str]]:
        def load() -> tuple[dict[str, Any], str]:
            now = self._clock()
            since, until = parse_window(parse_qs(query, keep_blank_values=True), now)
            live = until >= now - LIVE_EDGE
            return self._source.timeline(since, until), LIVE_TIMELINE_CACHE_CONTROL if live else PAST_TIMELINE_CACHE_CONTROL

        try:
            body, headers = self._cached("window?" + query, load)
        except BadRequest as exc:
            return error_response(400, str(exc))
        except Exception as exc:
            log.exception("timeline query failed")
            return error_response(503, f"timeline query failed: {exc}")
        return conditional(body, headers, if_none_match)

    def turn(self, trace_id: str, if_none_match: str | None) -> tuple[int, bytes, dict[str, str]]:
        def load() -> tuple[dict[str, Any], str] | None:
            result = self._source.turn(trace_id)
            return (result, TURN_CACHE_CONTROL) if result is not None else None

        try:
            found = self._cached("turn/" + trace_id, load)
        except Exception as exc:
            log.exception("turn query failed")
            return error_response(503, f"turn query failed: {exc}")
        if found is None:
            return error_response(404, f"no spans for trace {trace_id}")
        return conditional(*found, if_none_match)


def make_handler(manifest: dict[str, Any], state: MapState, timeline: Timeline) -> type[BaseHTTPRequestHandler]:
    openapi = json.dumps(openapi_document(manifest)).encode()
    ping = json.dumps({"status": "ok", "cassette": manifest["cassette"]["name"]}).encode()
    api = manifest.get("api", {})
    health_path = api.get("health", "/ping")
    openapi_path = api.get("openapi", "/openapi")
    base = api_base(manifest)
    map_path = base + "/points"
    timeline_path = base + "/timeline"
    turn_prefix = base + "/timeline/turns/"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if_none_match = self.headers.get("If-None-Match")
            if path == health_path:
                self._send(200, ping)
            elif path == openapi_path:
                self._send(200, openapi)
            elif path == map_path:
                self._send(*state.points(if_none_match))
            elif path == timeline_path:
                self._send(*timeline.window(query, if_none_match))
            elif path.startswith(turn_prefix) and (trace_id := unquote(path.removeprefix(turn_prefix))) and "/" not in trace_id:
                self._send(*timeline.turn(trace_id, if_none_match))
            elif path == "/healthz":
                self._send(200, b"ok", content_type="text/plain")
            elif path == "/readyz":
                if state.db_reached:
                    self._send(200, b"ok", content_type="text/plain")
                else:
                    self._send(503, b"not ready: database not reached yet", content_type="text/plain")
            else:
                self._send(404, json.dumps({"error": "not found"}).encode())

        def _send(
            self, status: int, body: bytes, headers: dict[str, str] | None = None, content_type: str = "application/json"
        ) -> None:
            self.send_response(status)
            # A 304 has no body, so it must not describe one.
            if status != 304:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            log.debug(format, *args)

    return Handler


def make_server(listen: str, manifest: dict[str, Any], state: MapState, timeline: Timeline) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host.strip("[]"), int(port)), make_handler(manifest, state, timeline))
    server.daemon_threads = True
    return server
