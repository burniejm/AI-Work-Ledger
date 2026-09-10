CREATE TABLE activities (
  id                TEXT PRIMARY KEY,
  session_row_id    TEXT REFERENCES sessions(id),
  client_id         TEXT REFERENCES clients(id),
  project_id        TEXT REFERENCES projects(id),
  work_item_id      TEXT REFERENCES work_items(id),
  kind              TEXT NOT NULL CHECK (kind IN ('code','test','investigation','decision','review','manual','other')),
  summary           TEXT NOT NULL,
  outcome           TEXT,
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  stated_duration_minutes INTEGER,            -- only when the user said it (SPEC 7)
  confidence        TEXT NOT NULL CHECK (confidence IN ('high','medium','low','contradictory')),
  confidence_score  INTEGER NOT NULL DEFAULT 0,
  confidence_reason TEXT,
  source            TEXT NOT NULL CHECK (source IN ('stage_a','stage_b','manual','correction')),
  in_inbox          INTEGER NOT NULL DEFAULT 0,  -- low confidence -> excluded from normal reports
  idempotency_key   TEXT NOT NULL,
  source_hash       TEXT NOT NULL,
  origin_device_id  TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT
);
CREATE UNIQUE INDEX uq_activities_idem ON activities(idempotency_key);
CREATE INDEX ix_activities_started ON activities(started_at);
CREATE INDEX ix_activities_work_item ON activities(work_item_id, started_at);
CREATE INDEX ix_activities_scope ON activities(client_id, project_id, started_at);
CREATE INDEX ix_activities_inbox ON activities(in_inbox) WHERE in_inbox = 1;

CREATE TABLE activity_evidence (
  id               TEXT PRIMARY KEY,
  activity_id      TEXT NOT NULL REFERENCES activities(id),
  source_event_id  TEXT REFERENCES source_events(id),
  evidence_type    TEXT NOT NULL CHECK (evidence_type IN ('commit','file','tool_call','excerpt','jira','transcript','manual','test')),
  ref              TEXT,                       -- commit sha, file path, issue key, transcript uuid
  excerpt          TEXT,                       -- redacted, <= 512 chars
  occurred_at      TEXT,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX ix_activity_evidence_activity ON activity_evidence(activity_id);

CREATE TABLE remaining_items (
  id               TEXT PRIMARY KEY,
  work_item_id     TEXT REFERENCES work_items(id),
  client_id        TEXT REFERENCES clients(id),
  project_id       TEXT REFERENCES projects(id),
  title            TEXT NOT NULL,
  detail           TEXT,
  normalized_key   TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('open','in_progress','blocked','waiting','completed','superseded','cancelled','reopened')),
  kind             TEXT NOT NULL DEFAULT 'remaining' CHECK (kind IN ('remaining','blocker','waiting')),
  mention_count    INTEGER NOT NULL DEFAULT 1,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  resolved_at      TEXT,
  confidence       TEXT NOT NULL CHECK (confidence IN ('high','medium','low','contradictory')),
  source           TEXT NOT NULL CHECK (source IN ('stage_a','stage_b','manual','correction')),
  in_inbox         INTEGER NOT NULL DEFAULT 0,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_remaining_scope_key
  ON remaining_items(IFNULL(work_item_id,''), normalized_key) WHERE deleted_at IS NULL;
CREATE INDEX ix_remaining_state ON remaining_items(state, last_seen_at);

CREATE TABLE remaining_item_transitions (   -- APPEND ONLY
  id                 TEXT PRIMARY KEY,
  remaining_item_id  TEXT NOT NULL REFERENCES remaining_items(id),
  from_state         TEXT,
  to_state           TEXT NOT NULL,
  occurred_at        TEXT NOT NULL,
  source             TEXT NOT NULL CHECK (source IN ('stage_a','stage_b','manual','correction','jira')),
  confidence         TEXT NOT NULL CHECK (confidence IN ('high','medium','low','contradictory')),
  explanation        TEXT,
  source_event_id    TEXT REFERENCES source_events(id),
  activity_id        TEXT REFERENCES activities(id),
  origin_device_id   TEXT NOT NULL,
  created_at         TEXT NOT NULL
);
CREATE INDEX ix_transitions_item ON remaining_item_transitions(remaining_item_id, occurred_at);

CREATE TABLE decisions (
  id               TEXT PRIMARY KEY,
  work_item_id     TEXT REFERENCES work_items(id),
  activity_id      TEXT REFERENCES activities(id),
  client_id        TEXT REFERENCES clients(id),
  project_id       TEXT REFERENCES projects(id),
  statement        TEXT NOT NULL,
  rationale        TEXT,
  decided_at       TEXT NOT NULL,
  confidence       TEXT NOT NULL CHECK (confidence IN ('high','medium','low','contradictory')),
  source           TEXT NOT NULL CHECK (source IN ('stage_a','stage_b','manual','correction')),
  idempotency_key  TEXT NOT NULL,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_decisions_idem ON decisions(idempotency_key);

CREATE TABLE contradictions (
  id               TEXT PRIMARY KEY,
  rule             TEXT NOT NULL,     -- jira_done_open_remaining | jira_open_work_complete |
                                      -- assignee_mismatch | removed_requirement | manual_vs_evidence | possible_duplicate
  work_item_id     TEXT REFERENCES work_items(id),
  subject_table    TEXT,
  subject_id       TEXT,
  ledger_claim     TEXT NOT NULL,
  external_claim   TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved','stale')),
  detected_at      TEXT NOT NULL,
  resolved_at      TEXT,
  resolution_note  TEXT,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_contradictions_open
  ON contradictions(rule, IFNULL(work_item_id,''), IFNULL(subject_id,''))
  WHERE status = 'open' AND deleted_at IS NULL;

CREATE TABLE corrections (                  -- APPEND ONLY
  id                   TEXT PRIMARY KEY,
  correction_group_id  TEXT NOT NULL,
  target_table         TEXT NOT NULL,
  target_id            TEXT NOT NULL,
  field                TEXT,
  old_value_json       TEXT,
  new_value_json       TEXT,
  operation            TEXT NOT NULL CHECK (operation IN ('create','update','move','merge','soft_delete','restore','state_change')),
  reason               TEXT,
  actor                TEXT NOT NULL CHECK (actor IN ('user','model')),
  applied_at           TEXT NOT NULL,
  undone_at            TEXT,              -- RESERVED. Never written in v1 (Ruling 7): undo is
                                          -- append-only and "is undone" is derived by looking
                                          -- for a row whose undo_of_correction_id matches.
  undo_of_correction_id TEXT REFERENCES corrections(id),
  origin_device_id     TEXT NOT NULL,
  created_at           TEXT NOT NULL
);
CREATE INDEX ix_corrections_group ON corrections(correction_group_id);
CREATE INDEX ix_corrections_target ON corrections(target_table, target_id);

CREATE TABLE summary_cache (                -- rebuildable cache (SPEC 15.1)
  id           TEXT PRIMARY KEY,
  cache_key    TEXT NOT NULL,
  scope_kind   TEXT NOT NULL CHECK (scope_kind IN ('day','range','client','project','work_item')),
  scope_id     TEXT,
  period_start TEXT,
  period_end   TEXT,
  inputs_hash  TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  built_at     TEXT NOT NULL,
  stale        INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX uq_summary_cache_key ON summary_cache(cache_key);
