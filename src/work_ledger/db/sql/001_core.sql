CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE devices (
  id          TEXT PRIMARY KEY,
  device_key  TEXT NOT NULL UNIQUE,      -- stable per-machine key, generated once
  hostname    TEXT,
  platform    TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE display_id_counters (
  scope       TEXT PRIMARY KEY,          -- e.g. 'LOCAL'
  next_value  INTEGER NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE clients (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  slug             TEXT NOT NULL,
  notes            TEXT,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_clients_slug ON clients(slug) WHERE deleted_at IS NULL;

CREATE TABLE projects (
  id               TEXT PRIMARY KEY,
  client_id        TEXT NOT NULL REFERENCES clients(id),
  name             TEXT NOT NULL,
  slug             TEXT NOT NULL,
  notes            TEXT,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_projects_client_slug ON projects(client_id, slug) WHERE deleted_at IS NULL;
CREATE INDEX ix_projects_client ON projects(client_id);

CREATE TABLE work_items (
  id                    TEXT PRIMARY KEY,
  kind                  TEXT NOT NULL CHECK (kind IN ('jira','local')),
  client_id             TEXT REFERENCES clients(id),
  project_id            TEXT REFERENCES projects(id),
  connection_id         TEXT,                       -- no SQL-level FK; connection validity is enforced by application code
  external_key          TEXT,                       -- e.g. 'AB-76'
  external_id           TEXT,                       -- Jira numeric/opaque id
  display_id            TEXT,                       -- 'LOCAL-0192' for kind='local'
  title                 TEXT NOT NULL,
  description           TEXT,
  local_state           TEXT NOT NULL DEFAULT 'open'
                        CHECK (local_state IN ('open','in_progress','blocked','waiting','completed','cancelled')),
  jira_status           TEXT,
  jira_status_category  TEXT,                       -- 'new' | 'indeterminate' | 'done'
  jira_resolution       TEXT,
  jira_assignee         TEXT,
  jira_updated_at       TEXT,
  jira_fetched_at       TEXT,
  jira_stale            INTEGER NOT NULL DEFAULT 1,
  merged_into_id        TEXT REFERENCES work_items(id),
  origin_device_id      TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  deleted_at            TEXT
);
-- SPEC 15.1: "Enforce uniqueness for external Jira identity: connection plus issue ID/key."
CREATE UNIQUE INDEX uq_work_items_conn_key ON work_items(connection_id, external_key)
  WHERE deleted_at IS NULL AND connection_id IS NOT NULL AND external_key IS NOT NULL;
CREATE UNIQUE INDEX uq_work_items_conn_extid ON work_items(connection_id, external_id)
  WHERE deleted_at IS NULL AND connection_id IS NOT NULL AND external_id IS NOT NULL;
-- Ruling 18: a free-text ticket key captured by `worklog add` before any connection
-- claims it has connection_id IS NULL, which uq_work_items_conn_key cannot cover.
-- Without this index, repeating "Reviewed AB-76" creates duplicate work_items rows.
CREATE UNIQUE INDEX uq_work_items_orphan_key ON work_items(external_key)
  WHERE connection_id IS NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_work_items_display ON work_items(display_id)
  WHERE display_id IS NOT NULL;
CREATE INDEX ix_work_items_client_project ON work_items(client_id, project_id);
CREATE INDEX ix_work_items_updated ON work_items(updated_at);

CREATE TABLE work_item_links (
  id               TEXT PRIMARY KEY,
  from_work_item_id TEXT NOT NULL REFERENCES work_items(id),
  to_work_item_id   TEXT NOT NULL REFERENCES work_items(id),
  link_type        TEXT NOT NULL CHECK (link_type IN ('linked_to','merged_into','duplicate_of','supersedes','relates_to')),
  note             TEXT,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_work_item_links ON work_item_links(from_work_item_id, to_work_item_id, link_type)
  WHERE deleted_at IS NULL;

CREATE TABLE aliases (
  id               TEXT PRIMARY KEY,
  alias_kind       TEXT NOT NULL CHECK (alias_kind IN ('client','project','work_item','ticket_prefix','repo_path','git_identity')),
  alias_text       TEXT NOT NULL,           -- stored lowercased/normalized
  target_kind      TEXT NOT NULL CHECK (target_kind IN ('client','project','work_item','connection')),
  target_id        TEXT NOT NULL,
  confidence       TEXT NOT NULL DEFAULT 'high' CHECK (confidence IN ('high','medium','low')),
  source           TEXT NOT NULL CHECK (source IN ('setup','inference','correction','manual')),
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_aliases ON aliases(alias_kind, alias_text) WHERE deleted_at IS NULL;
