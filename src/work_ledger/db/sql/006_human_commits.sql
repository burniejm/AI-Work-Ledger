-- Git-side capture (DESIGN "Git-side capture"): commits recorded by the
-- opt-in per-repo `post-commit` hook (`worklog githooks install`), at the
-- moment they happen, with or without a live Claude session.
-- `session_active=0` marks a commit made with no live session for that
-- repo: definitive human-side evidence for the AI-usage score.
CREATE TABLE human_commits (
  id               TEXT PRIMARY KEY,
  repo_root        TEXT NOT NULL,
  sha              TEXT NOT NULL,
  authored_at      TEXT NOT NULL,
  author_email     TEXT,
  subject          TEXT,
  files_json       TEXT NOT NULL DEFAULT '[]',
  session_active   INTEGER NOT NULL DEFAULT 0,
  origin_device_id TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_human_commits_repo_sha ON human_commits(repo_root, sha);
CREATE INDEX ix_human_commits_authored ON human_commits(authored_at);
