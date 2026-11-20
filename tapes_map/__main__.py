import logging
import os
import signal
import sys
import threading

from tapes_map import store
from tapes_map.config import load_settings
from tapes_map.manifest import load_manifest
from tapes_map.service import MapState, make_server

log = logging.getLogger("tapes_map")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    manifest = load_manifest()
    try:
        settings = load_settings(manifest.get("config", []), os.environ)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    url = os.environ.get("TAPES_DATABASE_URL", "").strip()
    if not url:
        log.error("TAPES_DATABASE_URL is required: the map reads session embeddings from Postgres")
        return 2
    listen = os.environ.get("CASSETTE_LISTEN", "").strip() or "0.0.0.0:9997"

    store.wait_for_db(url, retry=settings["wait_for_db"])

    state = MapState(lambda: store.fetch_centroids(url, settings["embeddings_view"], settings["sessions_table"], store.TURNS_VIEW))
    state.db_reached = True
    stop = threading.Event()
    threading.Thread(target=state.run, args=(settings["refresh.interval"], stop), daemon=True).start()

    server = make_server(listen, manifest, state)
    # Python as PID 1 in a container ignores SIGTERM unless a handler is installed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("map cassette listening on %s", listen)
    try:
        server.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
