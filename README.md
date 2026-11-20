# tapes-map-cassette

A [tapes](https://github.com/papercomputeco/tapes) cassette named `map`. It
serves 2-D coordinates for every tapes session, placed so that sessions about
similar things sit near each other.

For each session it averages the session's span embeddings, L2-normalizes the
average, projects all sessions to 2-D with UMAP (cosine metric, 15 neighbours,
`min_dist` 0.1, fixed random seed), then centres the result and scales it into
[-1, 1]. It does this at startup and then once per `refresh.interval` (default
1 hour), and keeps only the latest result in memory. It creates no tables.

UMAP was chosen over PCA after measuring both on real tapes data: UMAP kept 36%
of each session's true 10 nearest neighbours, and PCA kept 11%.

The map is 2-D because the UI draws it on a 2-D canvas, and a third axis bought
little. On 3,069 real sessions, 2-D kept 31.8% of the true 10 nearest neighbours
(3-D: 36.3%), a session's neighbours shared its repository 44.1% of the time
(3-D: 45.8%, against a ceiling of 46.5%), and
trustworthiness was 0.896 (3-D: 0.917).

## Before you deploy: it reads another cassette's data

The span embeddings belong to the search cassette
([papercomputeco/search-cassette](https://github.com/papercomputeco/search-cassette)),
which stores them in its private table `search.span_embeddings`. Search does not
publish a view over that table, and the map's role `cassette_map` has no grant on
it.

The map reads a view that **your deployment** creates instead:
`search_contract_v1.span_embeddings`. The view is owned by `cassette_search` and
runs with its owner's rights. `cassette_map` gets `SELECT` on the view and on
`tapes_v1.sessions`, and nothing else. [`provision.sql`](provision.sql) creates
the view and both grants. Run it as a superuser after the `cassette_search` and
`cassette_map` roles exist.


The script is idempotent and skips anything that does not exist yet, so you can
run it at every Postgres start. On a fresh install the first runs skip until the
search cassette has created its table and tapes has run its migrations. The map
returns 503 until a run succeeds. Do not set `security_invoker` on the view: the
view would then run with `cassette_map`'s rights, which do not include the
search table.

The map does not list this view under `depends.published` in its manifest,
because no cassette publishes it. Its name is deployment configuration
(`embeddings_view`), as the cassette contract recommends for a consumer that
must not depend on a particular publisher.

### The view couples the map to search's private table layout

The view selects `trace_id`, `span_id`, `chunk_idx`, `session_id`, `model`,
`embedded_at` and `embedding` from a table that search-cassette treats as
internal. Postgres then ties the two together in both directions:

| Change to `search.span_embeddings` | Effect |
| --- | --- |
| Add a column, or drop a column the view doesn't select | Works. |
| Rename a selected column | Search works and the existing view keeps working, but re-running `provision.sql` can no longer re-create it. The script logs a warning and continues. |
| Retype a selected column (such as a new embedding dimension), drop one, or drop the table | **Search's own change fails**: Postgres refuses to alter or drop what a view depends on. |

The last row matters most. search-cassette refuses to start against a table
with different dimensions and tells you to re-embed into a new table or drop
the old one; with this view in place, drop the view first
(`DROP VIEW search_contract_v1.span_embeddings`), make the search change, then
run `provision.sql` again. Check the table layout before upgrading search.

When the view is missing or unreadable, only the map is affected:
`/api/map/points` returns 503 with the Postgres error, or keeps serving the last
successful result and logs each failed refresh.

`provision.sql` downgrades a failure to create the view to a warning, so a
Postgres start hook that runs it cannot fail because of a search change. A hook
that fails can stop Postgres from starting at all.

This was tested against search-cassette v0.3.1: its image was run against an
empty Postgres, and the table it created matches the view's columns and the
table in `tests/test_integration.py`. If search ever publishes an equivalent
view, set `embeddings_view` to that view and drop the deployment's view.

## Operating notes

- **Layouts are recomputed from scratch.** Each refresh fits a new UMAP. For
  unchanged data the seed makes the layout identical, but new sessions move
  existing points. A new session first appears after the next refresh, so with
  the defaults it can take up to an hour.
- **Cost.** The aggregation reads every embedding on every refresh. On the
  author's cluster, `avg(embedding)` grouped by session took about 47 s for
  118,515 vectors. Over a local Postgres with 119,000 vectors it took 1.1 s.
  Coordinates are never computed per request. UMAP holds every session centroid
  in memory: 7,000 sessions × 768 float32 values is about 22 MB, plus UMAP's
  working set, numba and llvmlite. In the container, 7,000 sessions from 119,000
  vectors measured:
  - first refresh: 26 s, including numba's JIT compilation; later refreshes
    took 7.7 s;
  - cgroup `memory.peak`: 950 MB;
  - memory after each refresh: 760–810 MB, with no growth over repeated
    refreshes.

  Set a memory limit of at least **1.5 GiB** and request 1 GiB. The working set
  grows with the number of sessions.
- **Embedding model.** The model and its dimensions are whatever search is
  configured with. The map does not support projecting several models into one
  space. It uses the model with the most embedded spans, leaves the rest out,
  and logs how many session embeddings it left out. The model it used is
  returned as `model`.
- **Read-only root filesystem.** The image runs as UID 65532 and writes only to
  `/tmp`, where numba keeps its JIT cache (`NUMBA_CACHE_DIR=/tmp/numba`). Mount a
  writable `/tmp`, such as an `emptyDir`.

## Configuration

Every key is read from `CASSETTE_<KEY>`: the key upper-cased, with dots replaced
by underscores. These are the same names search-cassette uses.

| Key | Environment | Type | Default | Purpose |
| --- | --- | --- | --- | --- |
| `embeddings_view` | `CASSETTE_EMBEDDINGS_VIEW` | string | `search_contract_v1.span_embeddings` | Relation to read span embeddings from. |
| `sessions_table` | `CASSETTE_SESSIONS_TABLE` | string | `tapes_v1.sessions` | Relation to read session metadata from. |
| `refresh.interval` | `CASSETTE_REFRESH_INTERVAL` | duration | `1h` | Time between refreshes (Go syntax: `30m`, `1h30m`). |
| `wait_for_db` | `CASSETTE_WAIT_FOR_DB` | bool | `false` | At startup, retry an unreachable Postgres with backoff instead of exiting. |

The process also reads two settings that are not in the manifest:

| Environment | Default | Purpose |
| --- | --- | --- |
| `TAPES_DATABASE_URL` | required | Postgres URL for the `cassette_map` role. |
| `CASSETTE_LISTEN` | `0.0.0.0:9997` | Listen address. |

## HTTP contract

`cassette.toml` is loaded at runtime and embedded unchanged as
`x-tapes-cassette` in `/openapi`, so the two copies of the manifest cannot
differ. The API prefix is `api`. Tapes republishes `/api/map/points` as
`/v1/cassettes/map/points`.

`GET /api/map/points` returns `200 application/json`:

```json
{
  "computed_at": "2026-11-20T17:45:58.459011Z",
  "model": "embeddinggemma",
  "method": "umap",
  "dimensions": 2,
  "session_count": 7000,
  "points": [
    {
      "session_id": "00000000-0000-0000-0000-000000000001",
      "x": -0.23, "y": -0.35,
      "span_count": 17,
      "harness_id": "claude",
      "cwd": "/src/p0",
      "title": "topic 0 #0",
      "last_seen_at": "2026-11-01T00:00:00Z"
    }
  ]
}
```

`span_count` counts distinct spans, not chunks. `title` is the first non-blank
value of `display_name`, `derived_title`, `name`, the first 12 characters of
`harness_session_id`, and the session id. `harness_id` and `cwd` are `""` when
the session has none.

The endpoint returns `503 {"error": "..."}` until the first refresh succeeds.
After a failed refresh the error is the Postgres error.

### Caching

A `200` carries `ETag` (a strong tag: the first 16 hex digits of the body's
SHA-256), `Last-Modified` (`computed_at`) and
`Cache-Control: private, max-age=300, stale-while-revalidate=3600`. The body
includes `computed_at`, so every successful refresh produces a new tag.

When any tag in `If-None-Match` matches, or it is `*`, the endpoint returns
`304` with `ETag` and `Cache-Control` and no body. Tags are compared with any
`W/` prefix removed, because Cloudflare and Envoy weaken ETags when they
compress a response. A `503` carries `Cache-Control: no-store`. `HEAD` is
supported on every path.

Other endpoints, all `GET`:

| Path | Response |
| --- | --- |
| `/ping` | `200 {"status": "ok", "cassette": "map"}` |
| `/openapi` | OpenAPI 3.1 document with the manifest at `x-tapes-cassette` |
| `/healthz` | `200` while the process serves requests |
| `/readyz` | `200` once the cassette has reached Postgres, otherwise `503` |

## Run locally

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync
TAPES_DATABASE_URL=postgresql://cassette_map:...@localhost:5432/tapes uv run python -m tapes_map
```

Register it with tapes:

```bash
tapes serve api --cassettes http://127.0.0.1:9997/openapi
curl localhost:8081/v1/cassettes
curl localhost:8081/v1/cassettes/map/points
```

## Test

```bash
uv run pytest
```

The unit tests check the projection, the configuration and the HTTP contract
against an in-memory data source. `tests/test_integration.py` needs Docker and
is skipped without it. It starts `pgvector/pgvector:pg17`, creates search's table
as `cassette_search`, runs `provision.sql`, and runs one refresh as a
`cassette_map` role holding only the deployment's grants.

`tools/validate` checks admission with tapes' own code. Start the cassette, then
run:

```bash
cd tools/validate
go run . ../../cassette.toml http://127.0.0.1:9997/openapi
```

It validates `cassette.toml` and the served `x-tapes-cassette` against contract
`v1`, checks that their digests match, then admits the running server through
tapes' cassette runner and prints the republished paths.

## Image

`ghcr.io/lachieh/tapes-map-cassette` is built for linux/amd64 and linux/arm64.
Releases are tagged `v0.1.<run>` and `rolling`. The Dockerfile runs the tests
once on the build platform; running them under emulation is slow because of
numba's JIT. The release tag is stamped into `cassette.toml`, so `/openapi`
reports the release version and image.
