# tapes-map-cassette

A [tapes](https://github.com/papercomputeco/tapes) cassette named `map`. It
serves 2-D coordinates for every tapes session, placed so that sessions about
similar things sit near each other, groups nearby sessions into labelled
clusters, and serves a timeline of sessions and their turns for any window of up
to 31 days.

For each session it averages the session's span embeddings, L2-normalizes the
average, projects all sessions to 2-D with UMAP (cosine metric, 15 neighbours,
`min_dist` 0.1, fixed random seed), then centres the result and scales it into
[-1, 1]. It then clusters the 2-D points with HDBSCAN and labels each cluster
with the words that set its sessions' titles and first prompts apart from the
other clusters'. It does this at startup and then once per `refresh.interval`
(default 1 hour), and keeps only the latest result in memory. It creates no
tables.

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
runs with its owner's rights. [`provision.sql`](provision.sql) creates the view
and every grant the map needs. Run it as a superuser after the `cassette_search`
and `cassette_map` roles exist.

`cassette_map` gets `USAGE` on schemas `search_contract_v1` and `tapes_v1` and
`SELECT` on these relations, and nothing else:

| Relation | Read for |
| --- | --- |
| `search_contract_v1.span_embeddings` | Session centroids for the map. |
| `tapes_v1.sessions` | Point and timeline session fields: title, harness, cwd, times, status, model, totals. |
| `tapes_v1.span_turns` | Each point's first prompt; the timeline's turns. |
| `tapes_v1.spans` | Span and error-span counts per timeline turn; a turn's spans. Never `input` or `output`. |
| `tapes_v1.span_links` | The links with an end in a turn. |

The four `tapes_v1` views are listed under `depends.views` in `cassette.toml`.

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
- **Clusters.** HDBSCAN runs on the 2-D coordinates with `min_cluster_size`
  `max(10, n / 150)` and `min_samples` half that (at least 5). On 3,070 real
  sessions this gave 24 clusters from 26 to 983 sessions, with 290 sessions
  (9%) as noise; clustering and labelling took 40 ms. Cluster ids follow the
  sessions' order by id, so they are stable while the layout is, but a refresh
  that moves points can renumber them.
- **Labels.** Each session contributes the words of its title and its first
  genuine (non-synthetic) prompt. The text is cleaned first: tag blocks such as
  `<system-reminder>…</system-reminder>` and lone tags, `[T3 Code: …]`
  preambles, `(part 2/4)` suffixes, URLs, file paths and names, UUIDs, hex ids
  and code fences are removed. Words are lowercase runs of three or more letters,
  minus English stopwords and words nearly every agent prompt uses (`please`,
  `fix`, `agent`, `thread`, …; see `tapes_map/text.py`). Terms are ranked by
  class-based TF-IDF: a term's share of the cluster's words times
  `log(1 + A / f)`, where `A` is the average word count per cluster and `f` the
  term's count over all sessions, noise included. A label is the top three
  terms joined by ` · `. Sessions an automation opens with the same prompt
  (branch-name generators, approval reviewers) form their own clusters, and
  their labels say so.
- **Timeline reads.** The timeline endpoints query Postgres on each request, at
  most four at once, each statement limited to 15 s. A response is reused for
  15 s per query string. Every query is bounded and uses an index of the table
  under the `tapes_v1` view; see [Timeline queries](#timeline-queries).
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
differ. The API prefix is `api`. Tapes republishes `/api/map/<path>` as
`/v1/cassettes/map/<path>`.

`GET /api/map/points` returns `200 application/json`:

```json
{
  "computed_at": "2026-11-20T17:45:58.459011Z",
  "model": "embeddinggemma",
  "method": "umap",
  "dimensions": 2,
  "session_count": 7000,
  "clusters": [
    {
      "id": 0,
      "label": "coverage · area · alabama",
      "terms": ["coverage", "area", "alabama", "catalog", "research"],
      "size": 243,
      "x": 0.41, "y": -0.12
    }
  ],
  "points": [
    {
      "session_id": "00000000-0000-0000-0000-000000000001",
      "x": -0.23, "y": -0.35,
      "cluster": 0,
      "span_count": 17,
      "harness_id": "claude",
      "cwd": "/src/p0",
      "title": "topic 0 #0",
      "prompt": "Tune the map clusters",
      "started_at": "2026-10-31T23:00:00Z",
      "last_seen_at": "2026-11-01T00:00:00Z",
      "ended_at": null,
      "status": "completed",
      "model": "claude-opus-5-5",
      "parent_session_id": null,
      "turn_count": 3,
      "duration_ns": 3600000000000,
      "cost_usd": 0.125,
      "input_tokens": 1000,
      "output_tokens": 200,
      "tool_error_count": 0
    }
  ]
}
```

`span_count` counts distinct spans, not chunks. `title` is the first non-blank
value of `display_name`, `derived_title`, `name`, the first 12 characters of
`harness_session_id`, and the session id. `harness_id`, `cwd`, `status`
(tapes' `derived_status`) and `model` (`derived_model`) are `""` when the
session has none. `prompt` is the session's first turn with a non-empty
`user_prompt` that is not synthetic, cleaned as for labels and cut to 200
characters with a trailing `…`; it is `""` when there is none. `ended_at` and
`parent_session_id` are `null` when unset. `cost_usd` is a number, and the
counts and `duration_ns` are integers.

`cluster` is a cluster `id`, or `-1` for a session in no cluster. A cluster's
`x` and `y` are its members' median, a place to draw its label; `size` is its
member count; `terms` are its top five terms, best first.

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

### Timeline

`GET /api/map/timeline?since=<RFC 3339>&until=<RFC 3339>` returns the sessions
whose activity overlaps the window (`last_seen_at >= since` and
`started_at <= until`), most recently active first:

```json
{
  "since": "2026-11-15T00:00:00Z",
  "until": "2026-11-16T00:00:00Z",
  "truncated": false,
  "sessions": [
    {
      "session_id": "…", "parent_session_id": null,
      "harness_id": "claude", "cwd": "/src/a", "title": "Session A",
      "started_at": "2026-11-15T10:00:00Z", "last_seen_at": "2026-11-15T12:00:00Z",
      "ended_at": "2026-11-15T12:00:00Z", "status": "completed",
      "turn_count": 3, "cost_usd": 0.5, "input_tokens": 1000, "output_tokens": 100,
      "tool_error_count": 1,
      "turns": [
        {
          "trace_id": "ta2",
          "started_at": "2026-11-15T11:00:00Z", "ended_at": "2026-11-15T11:30:00Z",
          "duration_ns": 1800000000000, "status": "ok", "synthetic": false,
          "cost_usd": 0.4, "input_tokens": 900, "output_tokens": 90,
          "span_count": 3, "error_span_count": 1,
          "prompt": "Deploy the gateway"
        }
      ]
    }
  ]
}
```

`until` defaults to now and `since` to 24 hours before `until`. A timestamp
needs an offset or `Z`. A window of more than 31 days, `since >= until`, or a
value that is not a timestamp is `400 {"error": "..."}`.

Session fields follow the points. `turns` holds the session's turns that started
inside the window, oldest first. `synthetic` is `true` for turns tapes opened
itself (such as after a compaction). `prompt` is the turn's prompt, cleaned and
cut to 160 characters. `span_count` and `error_span_count` count the turn's
spans and those with status `error`. Turn times, `turn_count` and the totals
come from tapes as stored.

At most 400 sessions, the most recently active, and 2,000 turns, the newest,
are returned; `truncated` is `true` when either cap cut something.

`GET /api/map/timeline/turns/{trace_id}` returns one turn's spans in start order
and every link with an end in the turn:

```json
{
  "trace_id": "ta2",
  "session_id": "…",
  "truncated": false,
  "spans": [
    {
      "span_id": "s2", "parent_span_id": "s1",
      "kind": "llm", "name": "messages", "status": "ok",
      "call_kind": "main", "thread_id": "main", "model": "claude-opus-5-5",
      "started_at": "2026-11-15T11:01:00Z", "duration_ns": 6821157962,
      "input_tokens": 33868, "output_tokens": 662, "cost_usd": null
    }
  ],
  "links": [
    {"kind": "tool_call", "from_trace_id": "ta2", "from_span_id": "s2", "to_trace_id": "ta2", "to_span_id": "s3"}
  ]
}
```

`input_tokens` and `output_tokens` are the span's `usage.prompt_tokens` and
`usage.completion_tokens` (tapes' keys), falling back to `input_tokens` and
`output_tokens`; `cost_usd` is `usage.cost_usd`. Each is `null` when the usage
has no such key: tapes records tokens on `llm` spans only and stores no per-span
cost, so `cost_usd` is `null` for spans tapes writes today (the turn's cost is
on the timeline turn). `parent_span_id` and `session_id` are `null` when unset.
At most 5,000 spans and 5,000 links are returned (`truncated`). A trace with no
spans is `404 {"error": "..."}`.

Both endpoints read Postgres per request and reuse a response for 15 s per
query. They send `ETag` and answer a matching `If-None-Match` with `304`, as for
the points. `Cache-Control` is `private, max-age=15, stale-while-revalidate=60`
for a window whose `until` is within the last 5 minutes (or later),
`private, max-age=3600` for an older window, and `private, max-age=60` for a
turn. A failed query is `503 {"error": "..."}`; errors carry
`Cache-Control: no-store`.

#### Timeline queries

Each request runs at most three statements. The `tapes_v1` views are plain
projections of tapes' tables, so the indexes below are the tables'.

| Statement | Plan on the live database (November 2026) |
| --- | --- |
| Sessions overlapping the window, `LIMIT 401` | Sequential scan of `sessions` (about 3,100 rows, 6 MB); its indexes all lead with `org_id`. |
| Turns of those sessions in the window, `LIMIT 2001` | Index scan on `span_turns (session_id, started_at)` with both conditions in the index. |
| Span counts for those turns | Bitmap index scan on `spans (trace_id, span_id)`. |
| A turn's spans, `LIMIT 5001` | Index scan on `spans (trace_id, span_id)`; `input` and `output` are never read. |
| A turn's links, `LIMIT 5001` | `BitmapOr` of `span_links (org_id, from_trace_id)` and `(org_id, to_trace_id)`, with the turn's `org_id` from its spans. |
| First prompts at each refresh | Index scan on `span_turns (session_id, started_at)`, already in `DISTINCT ON` order. |

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
as `cassette_search` and reduced `tapes_v1` relations, runs `provision.sql`, and
runs one refresh and the timeline endpoints as a `cassette_map` role holding only
the deployment's grants.

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
