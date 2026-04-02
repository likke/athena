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
  status_note TEXT,
  last_reviewed_at INTEGER,
  next_review_at INTEGER,
  completion_record_id INTEGER,
  derived_status TEXT,
  derived_summary TEXT,
  rollup_updated_at INTEGER,
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
  status_source TEXT NOT NULL DEFAULT 'manual',
  health_source TEXT NOT NULL DEFAULT 'derived',
  derived_status TEXT,
  derived_health TEXT,
  rollup_summary TEXT,
  rollup_updated_at INTEGER,
  completion_summary TEXT,
  completion_record_id INTEGER,
  completion_mode TEXT,
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

CREATE TABLE IF NOT EXISTS completion_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_kind TEXT NOT NULL CHECK (entity_kind IN ('task', 'project', 'life_goal', 'workstream')),
  entity_id TEXT NOT NULL,
  resolution TEXT NOT NULL CHECK (resolution IN ('done', 'cancelled', 'superseded', 'merged')),
  summary TEXT NOT NULL,
  evidence_json TEXT,
  completed_by TEXT NOT NULL,
  verified_by TEXT,
  completed_at INTEGER NOT NULL,
  verified_at INTEGER
);

CREATE TABLE IF NOT EXISTS captured_items (
  id TEXT PRIMARY KEY,
  source_channel TEXT,
  source_chat_id TEXT,
  source_message_ref TEXT,
  dedupe_key TEXT UNIQUE,
  raw_text TEXT NOT NULL,
  classification TEXT CHECK (classification IN ('task', 'project_update', 'life_update', 'note', 'ignore')),
  linked_entity_kind TEXT,
  linked_entity_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('new', 'triaged', 'applied', 'ignored')),
  note TEXT,
  applied_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  capture_id TEXT,
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
  required_for_project_completion INTEGER NOT NULL DEFAULT 1 CHECK (required_for_project_completion IN (0, 1)),
  resolution TEXT,
  completion_summary TEXT,
  completion_record_id INTEGER,
  reopened_at INTEGER,
  reopen_reason TEXT,
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

CREATE TABLE IF NOT EXISTS outbox_items (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
  provider TEXT NOT NULL CHECK (provider IN ('gmail')),
  account_label TEXT NOT NULL DEFAULT 'primary',
  to_recipients TEXT NOT NULL,
  cc_recipients TEXT,
  bcc_recipients TEXT,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('drafting', 'needs_approval', 'approved', 'sending', 'sent', 'rejected', 'cancelled', 'error')),
  draft_id TEXT,
  external_ref TEXT,
  external_url TEXT,
  approval_note TEXT,
  error_message TEXT,
  sent_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  outbox_id TEXT NOT NULL REFERENCES outbox_items(id) ON DELETE CASCADE,
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
  current_capture_id TEXT,
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

CREATE TABLE IF NOT EXISTS review_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cadence TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly', 'monthly')),
  findings_count INTEGER NOT NULL DEFAULT 0,
  created_items_count INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_life_goals_area ON life_goals(life_area_id, status);
CREATE INDEX IF NOT EXISTS idx_projects_portfolio ON projects(portfolio_id, status);
CREATE INDEX IF NOT EXISTS idx_projects_goal ON projects(life_goal_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_bucket_status ON tasks(bucket, status, priority DESC, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status, priority DESC, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_status ON tasks(life_goal_id, status, priority DESC, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_items_status ON outbox_items(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_items_task ON outbox_items(task_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_events_item ON outbox_events(outbox_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_updates_project ON project_updates(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_awareness_briefs_scope ON awareness_briefs(scope_kind, scope_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_captured_items_status ON captured_items(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_captured_items_channel ON captured_items(source_channel, source_chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_completion_records_entity ON completion_records(entity_kind, entity_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_runs_cadence ON review_runs(cadence, created_at DESC);
