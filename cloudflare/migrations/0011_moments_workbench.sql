-- Visitor-private writing workspace. This data never joins public logs or search.
CREATE TABLE IF NOT EXISTS moments_workspaces (
  id TEXT PRIMARY KEY,
  profiles_json TEXT NOT NULL DEFAULT '{}',
  active_round_id TEXT,
  active_job_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moments_history (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES moments_workspaces(id),
  filename TEXT NOT NULL,
  body TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(owner_id, filename)
);
CREATE INDEX IF NOT EXISTS moments_history_owner ON moments_history(owner_id, created_at);
CREATE TABLE IF NOT EXISTS moments_drafts (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES moments_workspaces(id),
  round_id TEXT NOT NULL,
  variant INTEGER NOT NULL CHECK(variant IN (1,2,3)),
  title TEXT NOT NULL,
  angle TEXT NOT NULL,
  body TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待发布' CHECK(status IN ('待发布','已发布')),
  kept INTEGER NOT NULL DEFAULT 0,
  retired INTEGER NOT NULL DEFAULT 0,
  is_final INTEGER NOT NULL DEFAULT 0,
  published_body_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  published_at TEXT,
  UNIQUE(owner_id, round_id, variant)
);
CREATE INDEX IF NOT EXISTS moments_drafts_owner ON moments_drafts(owner_id, updated_at);
CREATE TABLE IF NOT EXISTS moments_revisions (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES moments_workspaces(id),
  draft_id TEXT NOT NULL REFERENCES moments_drafts(id),
  prior_body TEXT NOT NULL,
  revised_body TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moments_jobs (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES moments_workspaces(id),
  kind TEXT NOT NULL CHECK(kind IN ('generate','revise')),
  target INTEGER,
  status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','DONE','ERROR')),
  phase TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS moments_one_active_job ON moments_jobs(owner_id)
  WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX IF NOT EXISTS moments_jobs_owner ON moments_jobs(owner_id, created_at);
CREATE TABLE IF NOT EXISTS moments_usage (
  day TEXT NOT NULL,
  bucket TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, bucket)
);
