CREATE TABLE repositories (
  id               TEXT PRIMARY KEY,
  root_path        TEXT NOT NULL,
  name             TEXT NOT NULL,
  default_branch   TEXT,
  remotes_json     TEXT NOT NULL DEFAULT '[]',
  approved         INTEGER NOT NULL DEFAULT 1,   -- SPEC 12.6: only user-approved locations
  last_seen_at     TEXT,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_repositories_path ON repositories(root_path) WHERE deleted_at IS NULL;

CREATE TABLE repository_mappings (
  id               TEXT PRIMARY KEY,
  repository_id    TEXT NOT NULL REFERENCES repositories(id),
  path_prefix      TEXT NOT NULL DEFAULT '',     -- '' = whole repo; supports monorepo subpaths
  client_id        TEXT REFERENCES clients(id),
  project_id       TEXT REFERENCES projects(id),
  confidence       TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
  source           TEXT NOT NULL CHECK (source IN ('setup','inference','correction','manual')),
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_repo_mappings ON repository_mappings(repository_id, path_prefix) WHERE deleted_at IS NULL;

CREATE TABLE connections (
  id                   TEXT PRIMARY KEY,
  name                 TEXT NOT NULL,
  provider             TEXT NOT NULL DEFAULT 'jira',
  adapter              TEXT NOT NULL CHECK (adapter IN ('mcp','jira-rest')),
  base_url             TEXT,
  cloud_id             TEXT,
  username             TEXT,
  auth_method          TEXT CHECK (auth_method IS NULL OR auth_method IN ('mcp-oauth','cloud-api-token','pat')),
  credential_reference TEXT,                     -- 'keychain://work-ledger/<name>' or 'env://VAR'
  mcp_server_name      TEXT,                     -- e.g. 'atlassian'
  client_hint          TEXT,
  ticket_patterns_json TEXT NOT NULL DEFAULT '[]',
  project_filter_json  TEXT NOT NULL DEFAULT '[]',
  health_state         TEXT NOT NULL DEFAULT 'missing'
                       CHECK (health_state IN ('healthy','degraded','expired','missing','unsupported','disabled')),
  health_detail        TEXT,
  health_checked_at    TEXT,
  enabled              INTEGER NOT NULL DEFAULT 1,
  origin_device_id     TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  deleted_at           TEXT
);
CREATE UNIQUE INDEX uq_connections_name ON connections(name) WHERE deleted_at IS NULL;

CREATE TABLE connection_capabilities (
  id             TEXT PRIMARY KEY,
  connection_id  TEXT NOT NULL REFERENCES connections(id),
  capability     TEXT NOT NULL,   -- identify_current_user|list_sites|list_projects|get_issue|search_issues|get_issue_history|get_user_activity
  supported      INTEGER NOT NULL,
  tool_name      TEXT,            -- for MCP: the concrete mcp__<server>__<tool> that provides it
  detail         TEXT,
  checked_at     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_conn_capability ON connection_capabilities(connection_id, capability);

CREATE TABLE user_identities (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('git','jira')),
  connection_id    TEXT REFERENCES connections(id),
  value            TEXT NOT NULL,     -- git email, or Jira accountId
  display_name     TEXT,
  is_primary       INTEGER NOT NULL DEFAULT 0,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT
);
CREATE UNIQUE INDEX uq_user_identities ON user_identities(kind, IFNULL(connection_id,''), value)
  WHERE deleted_at IS NULL;
