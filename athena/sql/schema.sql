PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS life_areas (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
  priority INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS life_goals (
  id TEXT PRIMARY KEY,
  life_area_id TEXT NOT NULL REFERENCES life_areas(id) ON DELETE CASCADE,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  horizon TEXT NOT NULL CHECK (horizon IN ('now', 'quarter', 'year', 'multi_year')),
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
  success_definition TEXT,
  current_focus TEXT,
  supporting_rule TEXT,
  risk_if_ignored TEXT,
  last_reviewed_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  relationship_type TEXT NOT NULL,
  importance_score INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  contact_rule TEXT,
  last_meaningful_touch_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_documents (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  path TEXT,
  external_url TEXT,
  source_system TEXT NOT NULL,
  is_authoritative INTEGER NOT NULL DEFAULT 0 CHECK (is_authoritative IN (0, 1)),
  last_synced_at INTEGER,
  summary TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolios (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
  priority INTEGER NOT NULL DEFAULT 0,
  review_cadence TEXT NOT NULL DEFAULT 'weekly',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  life_area_id TEXT REFERENCES life_areas(id) ON DELETE SET NULL,
  life_goal_id TEXT REFERENCES life_goals(id) ON DELETE SET NULL,
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('product', 'client', 'internal', 'content', 'experiment', 'ops', 'personal')),
  tier TEXT NOT NULL CHECK (tier IN ('core', 'active', 'incubator', 'parked', 'archived')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'active', 'blocked', 'done', 'cancelled')),
  health TEXT NOT NULL CHECK (health IN ('green', 'yellow', 'red', 'unknown')),
  current_goal TEXT,
  next_milestone TEXT,
  blocker TEXT,
  notes TEXT,
  last_real_progress_at INTEGER,
  last_reviewed_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  alias TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS project_repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repo_name TEXT NOT NULL,
  repo_path TEXT NOT NULL UNIQUE,
  role TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
  last_seen_commit TEXT,
  last_seen_branch TEXT,
  last_seen_dirty INTEGER CHECK (last_seen_dirty IN (0, 1)),
  last_scanned_at INTEGER
);

CREATE TABLE IF NOT EXISTS workstreams (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  owner TEXT NOT NULL CHECK (owner IN ('ATHENA', 'FLEIRE', 'SHARED')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'blocked', 'done', 'parked')),
  current_goal TEXT,
  next_action TEXT,
  blocker TEXT,
  notes TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  life_area_id TEXT REFERENCES life_areas(id) ON DELETE SET NULL,
  life_goal_id TEXT REFERENCES life_goals(id) ON DELETE SET NULL,
  portfolio_id TEXT REFERENCES portfolios(id) ON DELETE SET NULL,
  project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
  workstream_id TEXT REFERENCES workstreams(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  owner TEXT NOT NULL CHECK (owner IN ('ATHENA', 'FLEIRE', 'SHARED')),
  bucket TEXT NOT NULL CHECK (bucket IN ('ATHENA', 'FLEIRE', 'BLOCKED', 'SOMEDAY')),
  status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'blocked', 'done', 'someday', 'cancelled')),
  priority INTEGER NOT NULL DEFAULT 0,
  source_text TEXT,
  why_now TEXT,
  next_action TEXT,
  blocker TEXT,
  notes TEXT,
  requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
  requires_browser INTEGER NOT NULL DEFAULT 0 CHECK (requires_browser IN (0, 1)),
  dedupe_key TEXT UNIQUE,
  source_channel TEXT,
  source_chat_id TEXT,
  source_message_ref TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_touched_at INTEGER NOT NULL,
  closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT,
  note TEXT,
  actor TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  summary TEXT NOT NULL,
  wins TEXT,
  risks TEXT,
  next_7_days TEXT,
  actor TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_state (
  channel TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  active_life_area_id TEXT REFERENCES life_areas(id) ON DELETE SET NULL,
  active_life_goal_id TEXT REFERENCES life_goals(id) ON DELETE SET NULL,
  current_portfolio_id TEXT REFERENCES portfolios(id) ON DELETE SET NULL,
  current_project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
  current_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  last_user_intent TEXT,
  last_progress TEXT,
  pending_approval_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (channel, chat_id)
);

CREATE TABLE IF NOT EXISTS awareness_briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('global', 'life_area', 'life_goal', 'portfolio', 'project', 'chat')),
  scope_id TEXT NOT NULL,
  brief_type TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_life_goals_area ON life_goals(life_area_id, status);
CREATE INDEX IF NOT EXISTS idx_projects_portfolio ON projects(portfolio_id, status);
CREATE INDEX IF NOT EXISTS idx_projects_goal ON projects(life_goal_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_bucket_status ON tasks(bucket, status, priority DESC, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status, priority DESC, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_status ON tasks(life_goal_id, status, priority DESC, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_updates_project ON project_updates(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_awareness_briefs_scope ON awareness_briefs(scope_kind, scope_id, created_at DESC);
