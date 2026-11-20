-- Deployment-side provisioning for the map cassette. Run it as a superuser
-- after the cassette_search and cassette_map roles exist. It is idempotent and
-- skips whatever is not there yet, so it can run at every Postgres start.
--
-- search_contract_v1.span_embeddings is a definer-rights view owned by
-- cassette_search over search's private table. cassette_map gets SELECT on the
-- view and never on search.span_embeddings. security_invoker must stay off:
-- with it, the view would be evaluated with cassette_map's rights and fail.
--
-- The column list is search-cassette's private layout (tested against
-- search-cassette v0.3.1). If it changes, CREATE OR REPLACE VIEW fails; the
-- failure is downgraded to a warning so a Postgres start hook running this
-- script never fails because of it. See the README for what the view blocks.
DO $$
BEGIN
  IF to_regrole('cassette_search') IS NULL OR to_regrole('cassette_map') IS NULL THEN
    RAISE NOTICE 'map contract view skipped: cassette_search or cassette_map role does not exist';
    RETURN;
  END IF;
  IF to_regclass('search.span_embeddings') IS NULL THEN
    RAISE NOTICE 'map contract view skipped: search.span_embeddings does not exist yet';
    RETURN;
  END IF;

  BEGIN
    CREATE SCHEMA IF NOT EXISTS search_contract_v1 AUTHORIZATION cassette_search;
    SET LOCAL ROLE cassette_search;
    CREATE OR REPLACE VIEW search_contract_v1.span_embeddings AS
      SELECT trace_id, span_id, chunk_idx, session_id, model, embedded_at, embedding
      FROM search.span_embeddings;
    GRANT USAGE ON SCHEMA search_contract_v1 TO cassette_map;
    GRANT SELECT ON search_contract_v1.span_embeddings TO cassette_map;
    RESET ROLE;
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'map contract view not updated: %', SQLERRM;
  END;

  -- The tapes_v1 views the manifest lists under depends.views: sessions for
  -- point fields, span_turns for each session's first prompt.
  IF to_regclass('tapes_v1.sessions') IS NULL THEN
    RAISE NOTICE 'tapes_v1 grants skipped: tapes has not migrated yet';
    RETURN;
  END IF;
  GRANT USAGE ON SCHEMA tapes_v1 TO cassette_map;
  GRANT SELECT ON tapes_v1.sessions TO cassette_map;
  IF to_regclass('tapes_v1.span_turns') IS NULL THEN
    RAISE NOTICE 'tapes_v1 span grants skipped: tapes has not created its span views yet';
    RETURN;
  END IF;
  GRANT SELECT ON tapes_v1.span_turns TO cassette_map;
END
$$;
