CREATE TABLE sessions (
  id               TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL,          -- Claude Code session_id
  transcript_path  TEXT,
  cwd              TEXT,
  git_branch       TEXT,
  repository_id    TEXT REFERENCES repositories(id),
  claude_version   TEXT,
  started_at       TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  ended_at         TEXT,
  end_reason       TEXT,                   -- 'session_end' | 'idle_finalized' | 'clear' | 'unknown'
  transcript_watermark TEXT,               -- last transcript record uuid successfully ingested
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_sessions_session_id ON sessions(session_id);
CREATE INDEX ix_sessions_last_seen ON sessions(last_seen_at);

CREATE TABLE source_events (               -- APPEND ONLY. Never updated, never deleted.
  id               TEXT PRIMARY KEY,
  session_row_id   TEXT REFERENCES sessions(id),
  session_id       TEXT,
  idempotency_key  TEXT NOT NULL,
  event_type       TEXT NOT NULL,          -- session_start|prompt|tool_use|tool_failure|commit|
                                           -- git_commit_signal|files_touched|test_run|jira_snapshot|
                                           -- manual|compact|subagent_stop|session_end
                                           -- (git_commit_signal added per Ruling 30(g); it is
                                           --  emitted by the PostToolUse hook, see 4.2)
  hook_event       TEXT,
  tool_name        TEXT,
  tool_use_id      TEXT,
  repository_id    TEXT REFERENCES repositories(id),
  git_branch       TEXT,
  cwd              TEXT,
  occurred_at      TEXT NOT NULL,          -- event time
  ingested_at      TEXT NOT NULL,          -- ingestion time (kept separate: SPEC 15.1)
  payload_json     TEXT NOT NULL,          -- redacted, size-capped
  payload_bytes    INTEGER NOT NULL,
  redaction_json   TEXT NOT NULL DEFAULT '[]',
  source_hash      TEXT NOT NULL,
  is_sidechain     INTEGER NOT NULL DEFAULT 0,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_source_events_idem ON source_events(idempotency_key);
CREATE INDEX ix_source_events_session_time ON source_events(session_id, occurred_at);
CREATE INDEX ix_source_events_type_time ON source_events(event_type, occurred_at);

CREATE TABLE capture_checkpoints (         -- APPEND ONLY
  id               TEXT PRIMARY KEY,
  session_id       TEXT,
  hook_event       TEXT NOT NULL,
  checkpoint_key   TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('ok','partial','failed','skipped')),
  detail           TEXT,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  duration_ms      INTEGER,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_checkpoint_key ON capture_checkpoints(checkpoint_key);
CREATE INDEX ix_checkpoints_session ON capture_checkpoints(session_id, created_at);
