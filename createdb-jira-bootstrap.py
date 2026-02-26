#!/usr/bin/env python3
"""ProjectPulse AI - Jira bootstrap (minimal DB for Jira ingestion)

Creates:
  - projectpulse_demo.db (SQLite)
  - projectpulse_schema.sql (schema)

Fills only the data needed to run jira_ingest_from_db.py:
  - projects
  - project_scopes (jira_epic entries only)

No events, snapshots, or Slack scopes. Run jira_ingest_from_db.py after this
to fetch Jira data into events and event_project_links.

Usage:
  python createdb-jira-bootstrap.py

Customize epic keys by editing JIRA_EPIC_SCOPES below.
"""

import os
import sqlite3
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "projectpulse_demo.db")
SCHEMA_PATH = os.path.join(HERE, "projectpulse_schema.sql")


def iso(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


# (project_id, name, description), (epic_key, ...)
# Add more projects/epics as needed for your Jira
JIRA_EPIC_SCOPES = [
    ("proj_incidentops", "IncidentOps", "Internal tooling for incident detection and response", "CLOPS-1447"),
    # ("proj_costoptimizer", "CostOptimizer", "Reduce infra spend", "COST-123"),  # example
]

SCHEMA_SQL = """PRAGMA foreign_keys = ON;

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
  source_type     TEXT NOT NULL,
  scope_kind      TEXT NOT NULL,
  scope_value     TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_scopes_project ON project_scopes(project_id);
CREATE INDEX IF NOT EXISTS idx_scopes_lookup ON project_scopes(source_type, scope_kind, scope_value);

CREATE TABLE IF NOT EXISTS events (
  event_id        TEXT PRIMARY KEY,
  source_type     TEXT NOT NULL,
  source_ref      TEXT NOT NULL,
  occurred_at     TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  container_id     TEXT,
  container_name  TEXT,
  actor_id        TEXT,
  actor_display   TEXT,
  event_kind      TEXT NOT NULL,
  title           TEXT,
  text            TEXT NOT NULL,
  permalink       TEXT,
  raw_json        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_ref ON events(source_type, source_ref);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_container ON events(source_type, container_id);

CREATE TABLE IF NOT EXISTS event_project_links (
  event_id          TEXT NOT NULL,
  project_id        TEXT NOT NULL,
  attribution_type  TEXT NOT NULL,
  confidence        REAL NOT NULL DEFAULT 1.0,
  rationale         TEXT,
  created_at        TEXT NOT NULL,
  PRIMARY KEY(event_id, project_id),
  FOREIGN KEY(event_id) REFERENCES events(event_id),
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_epl_project ON event_project_links(project_id);
CREATE INDEX IF NOT EXISTS idx_epl_event ON event_project_links(event_id);

CREATE TABLE IF NOT EXISTS project_status_snapshots (
  snapshot_id     TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  snapshot_at     TEXT NOT NULL,
  window_start    TEXT NOT NULL,
  window_end      TEXT NOT NULL,
  status_json     TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_project_time ON project_status_snapshots(project_id, snapshot_at);

CREATE TABLE IF NOT EXISTS snapshot_evidence (
  snapshot_id     TEXT NOT NULL,
  event_id        TEXT NOT NULL,
  section         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, event_id, section),
  FOREIGN KEY(snapshot_id) REFERENCES project_status_snapshots(snapshot_id),
  FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_snapshot ON snapshot_evidence(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_evidence_event ON snapshot_evidence(event_id);

CREATE TABLE IF NOT EXISTS project_checkpoints (
  project_id        TEXT PRIMARY KEY,
  last_viewed_at    TEXT,
  last_ingested_at  TEXT,
  last_snapshot_at  TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

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

CREATE VIEW IF NOT EXISTS v_project_events AS
SELECT
  l.project_id,
  e.*,
  l.attribution_type,
  l.confidence,
  l.rationale,
  l.created_at AS linked_at
FROM events e
JOIN event_project_links l ON l.event_id = e.event_id;
"""


def main() -> None:
    with open(SCHEMA_PATH, "w", encoding="utf-8") as f:
        f.write(SCHEMA_SQL)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)

    created = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    created_iso = iso(created)

    for idx, (project_id, name, description, epic_key) in enumerate(JIRA_EPIC_SCOPES):
        conn.execute(
            "INSERT INTO projects(project_id,name,description,created_at,is_active) VALUES (?,?,?,?,?)",
            (project_id, name, description, created_iso, 1),
        )
        scope_id = f"scope_jira_{project_id}"
        conn.execute(
            "INSERT INTO project_scopes(scope_id,project_id,source_type,scope_kind,scope_value,created_at) VALUES (?,?,?,?,?,?)",
            (scope_id, project_id, "jira", "jira_epic", epic_key, created_iso),
        )

    conn.commit()
    conn.close()

    print(f"✅ Created Jira bootstrap DB: {DB_PATH}")
    print(f"✅ Wrote schema:             {SCHEMA_PATH}")
    print(f"✅ Inserted {len(JIRA_EPIC_SCOPES)} project(s) with jira_epic scope(s)")
    print()
    print("Next: set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN and run:")
    print("  python jira_ingest_from_db.py")


if __name__ == "__main__":
    main()
