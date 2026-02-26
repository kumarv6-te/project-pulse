#!/usr/bin/env python3
"""ProjectPulse AI - Demo SQLite DB bootstrapper

Creates:
  - projectpulse_demo.db (SQLite)
  - projectpulse_schema.sql (schema)

Includes sample data for:
  - Project: IncidentOps (Jira epic CLOPS-1447)
  - Project: CostOptimizer
  - Shared Slack private channel: #efficiency-and-perf-internal
    (contains messages for multiple projects; attribution happens per-message)

Usage:
  python create_demo_db.py
"""

import os
import json
import sqlite3
import datetime
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "projectpulse_demo.db")
SCHEMA_PATH = os.path.join(HERE, "projectpulse_schema.sql")

def iso(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"

SCHEMA_SQL = "PRAGMA foreign_keys = ON;\n\nCREATE TABLE IF NOT EXISTS projects (\n  project_id      TEXT PRIMARY KEY,\n  name            TEXT NOT NULL,\n  description     TEXT,\n  created_at      TEXT NOT NULL,\n  is_active       INTEGER NOT NULL DEFAULT 1\n);\n\nCREATE TABLE IF NOT EXISTS project_scopes (\n  scope_id        TEXT PRIMARY KEY,\n  project_id      TEXT NOT NULL,\n  source_type     TEXT NOT NULL,              -- 'slack' | 'jira'\n  scope_kind      TEXT NOT NULL,              -- 'slack_channel' | 'jira_epic' | 'jira_project' | 'keyword'\n  scope_value     TEXT NOT NULL,              -- e.g. 'G0123...' OR 'CLOPS-1447' OR 'CLOPS'\n  created_at      TEXT NOT NULL,\n  FOREIGN KEY(project_id) REFERENCES projects(project_id)\n);\n\nCREATE INDEX IF NOT EXISTS idx_scopes_project ON project_scopes(project_id);\nCREATE INDEX IF NOT EXISTS idx_scopes_lookup ON project_scopes(source_type, scope_kind, scope_value);\n\nCREATE TABLE IF NOT EXISTS events (\n  event_id        TEXT PRIMARY KEY,\n  source_type     TEXT NOT NULL,              -- 'slack' | 'jira'\n  source_ref      TEXT NOT NULL,              -- unique: slack 'channel_id:ts', jira 'issueKey:commentId' etc.\n  occurred_at     TEXT NOT NULL,\n  ingested_at     TEXT NOT NULL,\n\n  container_id    TEXT,\n  container_name  TEXT,\n\n  actor_id        TEXT,\n  actor_display   TEXT,\n\n  event_kind      TEXT NOT NULL,              -- 'message' | 'comment' | 'status_change' | 'issue_update'\n  title           TEXT,\n  text            TEXT NOT NULL,\n  permalink       TEXT,\n\n  raw_json        TEXT\n);\n\nCREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_ref ON events(source_type, source_ref);\nCREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at);\nCREATE INDEX IF NOT EXISTS idx_events_container ON events(source_type, container_id);\n\nCREATE TABLE IF NOT EXISTS event_project_links (\n  event_id          TEXT NOT NULL,\n  project_id        TEXT NOT NULL,\n\n  attribution_type  TEXT NOT NULL,           -- 'scope_rule' | 'entity_match' | 'ml_classify' | 'manual'\n  confidence        REAL NOT NULL DEFAULT 1.0,\n  rationale         TEXT,\n  created_at        TEXT NOT NULL,\n\n  PRIMARY KEY(event_id, project_id),\n  FOREIGN KEY(event_id) REFERENCES events(event_id),\n  FOREIGN KEY(project_id) REFERENCES projects(project_id)\n);\n\nCREATE INDEX IF NOT EXISTS idx_epl_project ON event_project_links(project_id);\nCREATE INDEX IF NOT EXISTS idx_epl_event ON event_project_links(event_id);\n\nCREATE TABLE IF NOT EXISTS project_status_snapshots (\n  snapshot_id     TEXT PRIMARY KEY,\n  project_id      TEXT NOT NULL,\n\n  snapshot_at     TEXT NOT NULL,\n  window_start    TEXT NOT NULL,\n  window_end      TEXT NOT NULL,\n\n  status_json     TEXT NOT NULL,\n  created_at      TEXT NOT NULL,\n\n  FOREIGN KEY(project_id) REFERENCES projects(project_id)\n);\n\nCREATE INDEX IF NOT EXISTS idx_snapshots_project_time ON project_status_snapshots(project_id, snapshot_at);\n\nCREATE TABLE IF NOT EXISTS snapshot_evidence (\n  snapshot_id     TEXT NOT NULL,\n  event_id        TEXT NOT NULL,\n  section         TEXT NOT NULL,             -- 'progress' | 'blockers' | 'decisions' | 'next_steps' | 'risks'\n  created_at      TEXT NOT NULL,\n\n  PRIMARY KEY(snapshot_id, event_id, section),\n  FOREIGN KEY(snapshot_id) REFERENCES project_status_snapshots(snapshot_id),\n  FOREIGN KEY(event_id) REFERENCES events(event_id)\n);\n\nCREATE INDEX IF NOT EXISTS idx_evidence_snapshot ON snapshot_evidence(snapshot_id);\nCREATE INDEX IF NOT EXISTS idx_evidence_event ON snapshot_evidence(event_id);\n\nCREATE TABLE IF NOT EXISTS project_checkpoints (\n  project_id        TEXT PRIMARY KEY,\n  last_viewed_at    TEXT,\n  last_ingested_at  TEXT,\n  last_snapshot_at  TEXT,\n  FOREIGN KEY(project_id) REFERENCES projects(project_id)\n);\n\nCREATE VIEW IF NOT EXISTS v_project_latest_snapshot AS\nSELECT s.*\nFROM project_status_snapshots s\nJOIN (\n  SELECT project_id, MAX(snapshot_at) AS max_snapshot_at\n  FROM project_status_snapshots\n  GROUP BY project_id\n) latest\nON latest.project_id = s.project_id\nAND latest.max_snapshot_at = s.snapshot_at;\n\nCREATE VIEW IF NOT EXISTS v_project_events AS\nSELECT\n  l.project_id,\n  e.*,\n  l.attribution_type,\n  l.confidence,\n  l.rationale,\n  l.created_at AS linked_at\nFROM events e\nJOIN event_project_links l ON l.event_id = e.event_id;\n"

def main():
    with open(SCHEMA_PATH, "w", encoding="utf-8") as f:
        f.write(SCHEMA_SQL)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)

    now = datetime.datetime(2026, 2, 26, 17, 0, 0)     # fixed for deterministic demo
    created = datetime.datetime(2026, 2, 25, 9, 0, 0)
    ingested_at = iso(now)

    conn.executemany(
        "INSERT INTO projects(project_id,name,description,created_at,is_active) VALUES (?,?,?,?,?)",
        [
            ("proj_incidentops", "IncidentOps", "Internal tooling for incident detection and response", iso(created), 1),
            ("proj_costoptimizer", "CostOptimizer", "Reduce infra spend via automated rightsizing and anomaly detection", iso(created), 1),
        ]
    )

    SLACK_CH_ID = "G0EFFPERF123"
    SLACK_CH_NAME = "efficiency-and-perf-internal"

    conn.executemany(
        "INSERT INTO project_scopes(scope_id,project_id,source_type,scope_kind,scope_value,created_at) VALUES (?,?,?,?,?,?)",
        [
            ("scope_slack_inc", "proj_incidentops", "slack", "slack_channel", SLACK_CH_ID, iso(created + datetime.timedelta(minutes=1))),
            ("scope_slack_cost", "proj_costoptimizer", "slack", "slack_channel", SLACK_CH_ID, iso(created + datetime.timedelta(minutes=1))),
            ("scope_jira_inc", "proj_incidentops", "jira", "jira_epic", "CLOPS-1447", iso(created + datetime.timedelta(minutes=1))),
            ("scope_jira_cost", "proj_costoptimizer", "jira", "jira_project", "COST", iso(created + datetime.timedelta(minutes=1))),
        ]
    )

    def insert_event(event_id, source_type, source_ref, occurred_at, container_id, container_name,
                     actor_id, actor_display, event_kind, title, text, permalink, raw_obj):
        conn.execute(
            """INSERT INTO events(
                event_id, source_type, source_ref, occurred_at, ingested_at,
                container_id, container_name, actor_id, actor_display,
                event_kind, title, text, permalink, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, source_type, source_ref, occurred_at, ingested_at,
                container_id, container_name, actor_id, actor_display,
                event_kind, title, text, permalink, json.dumps(raw_obj, ensure_ascii=False)
            )
        )

    def link_event(event_id, project_id, attribution_type, confidence, rationale):
        conn.execute(
            "INSERT INTO event_project_links(event_id,project_id,attribution_type,confidence,rationale,created_at) VALUES (?,?,?,?,?,?)",
            (event_id, project_id, attribution_type, confidence, rationale, ingested_at)
        )

    # Slack events (shared channel)
    evt_301 = "evt_301"
    slack_ts_1 = "1700010000.000200"
    insert_event(
        evt_301, "slack", f"{SLACK_CH_ID}:{slack_ts_1}", iso(datetime.datetime(2026, 2, 26, 9, 15, 0)),
        SLACK_CH_ID, SLACK_CH_NAME, "U111", "Ananya", "message", None,
        "Blocked waiting on PagerDuty API access for CLOPS-1503.",
        f"https://slack.com/archives/{SLACK_CH_ID}/p{slack_ts_1.replace('.','')}",
        {"ts": slack_ts_1, "channel": SLACK_CH_ID, "user": "U111", "text": "Blocked waiting on PagerDuty API access for CLOPS-1503."}
    )
    link_event(evt_301, "proj_incidentops", "entity_match", 1.0, "Contains Jira issue key CLOPS-1503 (maps to epic CLOPS-1447)")

    evt_302 = "evt_302"
    slack_ts_2 = "1700010200.000250"
    insert_event(
        evt_302, "slack", f"{SLACK_CH_ID}:{slack_ts_2}", iso(datetime.datetime(2026, 2, 26, 9, 30, 0)),
        SLACK_CH_ID, SLACK_CH_NAME, "U222", "Ben", "message", None,
        "CostOptimizer: memory leak still unresolved in the rightsizer worker.",
        f"https://slack.com/archives/{SLACK_CH_ID}/p{slack_ts_2.replace('.','')}",
        {"ts": slack_ts_2, "channel": SLACK_CH_ID, "user": "U222", "text": "CostOptimizer: memory leak still unresolved in the rightsizer worker."}
    )
    link_event(evt_302, "proj_costoptimizer", "ml_classify", 0.86, "Embedding/keyword match to project CostOptimizer")

    evt_303 = "evt_303"
    slack_ts_3 = "1700010500.000300"
    insert_event(
        evt_303, "slack", f"{SLACK_CH_ID}:{slack_ts_3}", iso(datetime.datetime(2026, 2, 26, 10, 45, 0)),
        SLACK_CH_ID, SLACK_CH_NAME, "U333", "Rahul", "message", None,
        "Decision: adopt event-driven architecture for alert ingestion (IncidentOps).",
        f"https://slack.com/archives/{SLACK_CH_ID}/p{slack_ts_3.replace('.','')}",
        {"ts": slack_ts_3, "channel": SLACK_CH_ID, "user": "U333", "text": "Decision: adopt event-driven architecture for alert ingestion (IncidentOps)."}
    )
    link_event(evt_303, "proj_incidentops", "ml_classify", 0.78, "Mentions IncidentOps + alert ingestion keywords")

    evt_304 = "evt_304"
    slack_ts_4 = "1700010800.000350"
    insert_event(
        evt_304, "slack", f"{SLACK_CH_ID}:{slack_ts_4}", iso(datetime.datetime(2026, 2, 26, 11, 20, 0)),
        SLACK_CH_ID, SLACK_CH_NAME, "U444", "Dina", "message", None,
        "Retry logic still flaky — investigating.",
        f"https://slack.com/archives/{SLACK_CH_ID}/p{slack_ts_4.replace('.','')}",
        {"ts": slack_ts_4, "channel": SLACK_CH_ID, "user": "U444", "text": "Retry logic still flaky — investigating."}
    )
    link_event(evt_304, "proj_incidentops", "ml_classify", 0.62, "Similarity to IncidentOps retry/ingestion discussions (low confidence)")

    # Jira events (IncidentOps epic CLOPS-1447)
    evt_305 = "evt_305"
    insert_event(
        evt_305, "jira", "CLOPS-1501:status-change-1", iso(datetime.datetime(2026, 2, 26, 11, 30, 0)),
        "CLOPS", "CloudOps", "jira:meera", "Meera", "status_change", "CLOPS-1501 status",
        "CLOPS-1501 moved from In Progress to Done (Incident creation workflow).",
        "https://yourcompany.atlassian.net/browse/CLOPS-1501",
        {"issueKey": "CLOPS-1501", "from": "In Progress", "to": "Done"}
    )
    link_event(evt_305, "proj_incidentops", "scope_rule", 1.0, "Issue is under epic CLOPS-1447")

    evt_306 = "evt_306"
    insert_event(
        evt_306, "jira", "CLOPS-1503:comment-1", iso(datetime.datetime(2026, 2, 26, 12, 15, 0)),
        "CLOPS", "CloudOps", "jira:vikram", "Vikram", "comment", "CLOPS-1503 comment",
        "Still investigating retry logic failure; suspect exponential backoff bug.",
        "https://yourcompany.atlassian.net/browse/CLOPS-1503",
        {"issueKey": "CLOPS-1503", "commentId": "10001", "body": "Still investigating retry logic failure; suspect exponential backoff bug."}
    )
    link_event(evt_306, "proj_incidentops", "scope_rule", 1.0, "Issue is under epic CLOPS-1447")

    # Snapshot for IncidentOps (UI + chatbot can read this directly)
    snapshot_id = "snap_incidentops_1"
    status = {
        "headline": "IncidentOps alert ingestion progressing; PagerDuty access blocker remains.",
        "progress": [{"text": "CLOPS-1501 completed (Incident creation workflow)", "owner": "Meera", "event_ids": [evt_305]}],
        "blockers": [{"text": "Waiting on PagerDuty API access for CLOPS-1503", "owner": "Ananya", "event_ids": [evt_301]}],
        "decisions": [{"text": "Adopt event-driven architecture for alert ingestion", "owner": "Rahul", "event_ids": [evt_303]}],
        "next_steps": [{"text": "Fix retry/backoff bug in CLOPS-1503 and re-run integration test", "owner": "Vikram", "event_ids": [evt_306]}],
        "risks": [{"text": "Dependency on external PagerDuty approval may delay rollout", "event_ids": [evt_301]}]
    }

    conn.execute(
        "INSERT INTO project_status_snapshots(snapshot_id,project_id,snapshot_at,window_start,window_end,status_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, "proj_incidentops", iso(now), iso(datetime.datetime(2026, 2, 19, 0, 0, 0)), iso(now), json.dumps(status, ensure_ascii=False), iso(now))
    )

    conn.executemany(
        "INSERT INTO snapshot_evidence(snapshot_id,event_id,section,created_at) VALUES (?,?,?,?)",
        [
            (snapshot_id, evt_305, "progress", iso(now)),
            (snapshot_id, evt_301, "blockers", iso(now)),
            (snapshot_id, evt_303, "decisions", iso(now)),
            (snapshot_id, evt_306, "next_steps", iso(now)),
            (snapshot_id, evt_301, "risks", iso(now)),
        ]
    )

    conn.execute(
        "INSERT INTO project_checkpoints(project_id,last_viewed_at,last_ingested_at,last_snapshot_at) VALUES (?,?,?,?)",
        ("proj_incidentops", None, ingested_at, iso(now))
    )

    conn.commit()
    conn.close()

    print(f"✅ Created demo DB: {DB_PATH}")
    print(f"✅ Wrote schema:   {SCHEMA_PATH}")

if __name__ == "__main__":
    main()
