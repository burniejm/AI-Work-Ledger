-- Token usage per session x model (DESIGN "Token cost"), accumulated by
-- transcript reconciliation exactly once per record (uuid-deduped by the
-- source_events idempotency key: a record only adds tokens the same write
-- that creates its event row).
CREATE TABLE session_tokens (
  id                    TEXT PRIMARY KEY,
  session_id            TEXT NOT NULL,
  model                 TEXT NOT NULL,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  origin_device_id      TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_session_tokens ON session_tokens(session_id, model);
