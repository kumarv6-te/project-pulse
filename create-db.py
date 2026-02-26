import textwrap, os, json, datetime, pathlib

schema_sql = textwrap.dedent("""
-- ProjectPulse AI (Hackathon) - Unified DB Schema (SQLite)
-- Supports: Slack + Jira unified events, project attribution, time-based status snapshots, evidence links.
-- Notes:
-- - Timestamps stored as ISO-8601 TEXT (e.g., 2026-02-26T17:00:00Z or without Z if you prefer).
-- - raw_json and status_json are stored as TEXT containing JSON.

PRAGMA foreign_keys = ON;

-- =========================
-- 1) Projects and Scopes
-- =========================
CREATE TABLE IF NOT EXISTS projects (
  project_id      TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  description     TEXT,
  created_at      TEXT NOT NULL,
  is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS project_scopes (
  scope_id        TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  source_type     TEXT NOT NULL,              -- 'slack' | 'jira'
  scope_kind      TEXT NOT NULL,              -- 'slack_channel' | 'jira_epic' | 'jira_project' | 'keyword'
  scope_value     TEXT NOT NULL,              -- e.g. 'G0123...' OR 'CLOPS-1447' OR 'CLOPS'
  created_at      TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_scopes_project ON project_scopes(project_id);
CREATE INDEX IF NOT EXISTS idx_scopes_lookup ON project_scopes(source_type, scope_kind, scope_value);

-- =========================
-- 2) Unified Event Log
-- =========================
CREATE TABLE IF NOT EXISTS events (
  event_id        TEXT PRIMARY KEY,
  source_type     TEXT NOT NULL,              -- 'slack' | 'jira'
  source_ref      TEXT NOT NULL,              -- unique in source: slack 'channel_id:ts', jira 'issueKey:commentId' etc.
  occurred_at     TEXT NOT NULL,              -- when it happened in the source (ISO-8601 TEXT)
  ingested_at     TEXT NOT NULL,              -- when we pulled it (ISO-8601 TEXT)

  container_id    TEXT,                       -- slack channel id OR jira project key
  container_name  TEXT,                       -- slack channel name OR jira project name (optional)

  actor_id        TEXT,
  actor_display   TEXT,

  event_kind      TEXT NOT NULL,              -- 'message' | 'comment' | 'status_change' | 'issue_update'
  title           TEXT,
  text            TEXT NOT NULL,              -- normalized plain text for retrieval/summarization
  permalink       TEXT,                       -- slack permalink / jira browse url

  raw_json        TEXT                        -- original payload as JSON string (audit/debug)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_ref ON events(source_type, source_ref);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_container ON events(source_type, container_id);

-- =========================
-- 3) Project Attribution
-- =========================
CREATE TABLE IF NOT EXISTS event_project_links (
  event_id          TEXT NOT NULL,
  project_id        TEXT NOT NULL,

  attribution_type  TEXT NOT NULL,           -- 'scope_rule' | 'entity_match' | 'ml_classify' | 'manual'
  confidence        REAL NOT NULL DEFAULT 1.0,
  rationale         TEXT,
  created_at        TEXT NOT NULL,

  PRIMARY KEY(event_id, project_id),
  FOREIGN KEY(event_id) REFERENCES events(event_id),
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_epl_project ON event_project_links(project_id);
CREATE INDEX IF NOT EXISTS idx_epl_event ON event_project_links(event_id);

-- =========================
-- 4) Time-based Project Status
-- =========================
CREATE TABLE IF NOT EXISTS project_status_snapshots (
  snapshot_id     TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,

  snapshot_at     TEXT NOT NULL,             -- when generated
  window_start    TEXT NOT NULL,             -- summarized range start
  window_end      TEXT NOT NULL,             -- summarized range end

  status_json     TEXT NOT NULL,             -- structured status JSON
  created_at      TEXT NOT NULL,

  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_project_time ON project_status_snapshots(project_id, snapshot_at);

-- Each snapshot line can be backed by multiple evidence events, grouped into sections.
CREATE TABLE IF NOT EXISTS snapshot_evidence (
  snapshot_id     TEXT NOT NULL,
  event_id        TEXT NOT NULL,
  section         TEXT NOT NULL,             -- 'progress' | 'blockers' | 'decisions' | 'next_steps' | 'risks'
  created_at      TEXT NOT NULL,

  PRIMARY KEY(snapshot_id, event_id, section),
  FOREIGN KEY(snapshot_id) REFERENCES project_status_snapshots(snapshot_id),
  FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_snapshot ON snapshot_evidence(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_evidence_event ON snapshot_evidence(event_id);

-- =========================
-- 5) Optional Checkpoints (helps "changes since last check-in")
-- =========================
CREATE TABLE IF NOT EXISTS project_checkpoints (
  project_id        TEXT PRIMARY KEY,
  last_viewed_at    TEXT,                    -- UI last opened time
  last_ingested_at  TEXT,                    -- last successful ingest time
  last_snapshot_at  TEXT,                    -- last snapshot generation time
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

-- =========================
-- 6) Convenience Views
-- =========================

-- Latest snapshot per project (UI "Pulse" tab).
CREATE VIEW IF NOT EXISTS v_project_latest_snapshot AS
SELECT s.*
FROM project_status_snapshots s
JOIN (
  SELECT project_id, MAX(snapshot_at) AS max_snapshot_at
  FROM project_status_snapshots
  GROUP BY project_id
) latest
ON latest.project_id = s.project_id
AND latest.max_snapshot_at = s.snapshot_at;

-- Project-scoped event feed (chatbot + "Changes" tab).
CREATE VIEW IF NOT EXISTS v_project_events AS
SELECT
  l.project_id,
  e.event_id,
  e.source_type,
  e.source_ref,
  e.occurred_at,
  e.ingested_at,
  e.container_id,
  e.container_name,
  e.actor_id,
  e.actor_display,
  e.event_kind,
  e.title,
  e.text,
  e.permalink,
  e.raw_json,
  l.attribution_type,
  l.confidence,
  l.rationale,
  l.created_at AS linked_at
FROM events e
JOIN event_project_links l ON l.event_id = e.event_id;

""").strip() + "\n"

# Create SQLite DB locally and execute schema
db_path = pathlib.Path(__file__).parent / "projectpulse.db"
import sqlite3
conn = sqlite3.connect(db_path)
conn.executescript(schema_sql)
conn.commit()
conn.close()
print(f"Schema created: {db_path}")

