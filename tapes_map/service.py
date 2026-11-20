import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tapes_map.manifest import openapi_document, points_path
from tapes_map.projection import SessionCentroid, build_map

log = logging.getLogger(__name__)

POINTS_CACHE_CONTROL = "private, max-age=300, stale-while-revalidate=3600"


def etag_matches(if_none_match: str, etag: str) -> bool:
    """Weak comparison (RFC 9110 13.1.2): proxies that compress, such as Cloudflare and Envoy, weaken ETags."""
    tags = [tag.strip() for tag in if_none_match.split(",")]
    return any(tag == "*" or tag.removeprefix("W/") == etag for tag in tags)


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
            "ETag": f'"{hashlib.sha256(body).hexdigest()[:16]}"',
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
            return 503, json.dumps({"error": error}).encode(), {"Cache-Control": "no-store"}
        if if_none_match is not None and etag_matches(if_none_match, headers["ETag"]):
            return 304, b"", {"ETag": headers["ETag"], "Cache-Control": headers["Cache-Control"]}
        return 200, body, headers

    def run(self, interval: float, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh()
            stop.wait(interval)


def make_handler(manifest: dict[str, Any], state: MapState) -> type[BaseHTTPRequestHandler]:
    openapi = json.dumps(openapi_document(manifest)).encode()
    ping = json.dumps({"status": "ok", "cassette": manifest["cassette"]["name"]}).encode()
    api = manifest.get("api", {})
    health_path = api.get("health", "/ping")
    openapi_path = api.get("openapi", "/openapi")
    map_path = points_path(manifest)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == health_path:
                self._send(200, ping)
            elif path == openapi_path:
                self._send(200, openapi)
            elif path == map_path:
                status, body, headers = state.points(self.headers.get("If-None-Match"))
                self._send(status, body, headers=headers)
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain")
            elif path == "/readyz":
                if state.db_reached:
                    self._send(200, b"ok", "text/plain")
                else:
                    self._send(503, b"not ready: database not reached yet", "text/plain")
            else:
                self._send(404, json.dumps({"error": "not found"}).encode())

        def _send(
            self, status: int, body: bytes, content_type: str = "application/json", headers: dict[str, str] | None = None
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


def make_server(listen: str, manifest: dict[str, Any], state: MapState) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host.strip("[]"), int(port)), make_handler(manifest, state))
    server.daemon_threads = True
    return server
