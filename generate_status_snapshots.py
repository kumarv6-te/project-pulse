#!/usr/bin/env python3
"""ProjectPulse - Weekly status snapshot generator

Reads events from the database and synthesizes them into structured
project_status_snapshots (headline, progress, blockers, decisions, next_steps, risks).

Classification:
  - Jira: heuristic rules (status changes, keyword matching)
  - Slack: AI extraction when AI_ENABLED=1 (OPENAI_API_KEY), else keyword heuristics

Usage:
  python generate_status_snapshots.py

Env vars:
  DB_PATH         default ./projectpulse_demo.db
  WINDOW_DAYS     default 7 (days of events to include in snapshot)
  AI_ENABLED=1    Use OpenAI to extract trimmed status from Slack messages
  OPENAI_API_KEY  Required for AI. OPENAI_MODEL defaults to gpt-4o-mini.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from ai_utils import ai_extract_status_from_slack
    _AI_EXTRACT_AVAILABLE = True
except ImportError:
    ai_extract_status_from_slack = None
    _AI_EXTRACT_AVAILABLE = False

DB_PATH = os.environ.get("DB_PATH", "./projectpulse_demo.db")
AI_ENABLED = os.environ.get("AI_ENABLED", "0") == "1"
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))

# Status values that indicate completion (progress)
DONE_STATUSES = {"done", "closed", "resolved", "to verify"}

# Status values that indicate in-flight work (next_steps)
ACTIVE_STATUSES = {"in progress", "in review", "to do"}

# Keywords for classification (lowercase)
BLOCKER_KEYWORDS = ["blocked", "waiting", "stuck", "blocker", "blocking"]
DECISION_KEYWORDS = ["decision", "decided", "agreed", "we will", "we'll", "adopt"]
RISK_KEYWORDS = ["risk", "delay", "dependency", "may delay", "could delay"]
NEXT_STEP_KEYWORDS = ["pr", "raised", "open", "review", "merge", "deploy"]
# Additional keywords for Slack standups (action verbs, common status phrases)
SLACK_NEXT_STEP_KEYWORDS = ["implement", "create", "update", "connect", "coordinate", "meeting", "ticket", "sprint", "work with"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extract_issue_key(text: str, event_id: str) -> Optional[str]:
    """Extract Jira issue key (e.g. CLOPS-1571) from text or event_id."""
    match = re.search(r"([A-Z][A-Z0-9]+-\d+)", text or "")
    if match:
        return match.group(1)
    # event_id format: jira_CLOPS-1571_comment_2138475
    parts = event_id.split("_")
    if len(parts) >= 2 and re.match(r"[A-Z0-9]+-\d+", parts[1]):
        return parts[1]
    return None


def classify_event(
    event_kind: str,
    text: str,
    raw_json: Optional[str],
    actor_display: Optional[str],
) -> Tuple[Optional[str], str]:
    """
    Classify event into section. Returns (section, summary_text) or (None, "") if unclassified.
    section: progress | blockers | decisions | next_steps | risks
    """
    text_lower = (text or "").lower()
    actor = actor_display or "Unknown"

    if event_kind == "status_change":
        # Parse "CLOPS-1571 status changed: In Progress → Closed"
        to_status = None
        from_status = None
        issue_key = extract_issue_key(text, "")

        if raw_json:
            try:
                obj = json.loads(raw_json)
                item = obj.get("item") or {}
                to_status = (item.get("toString") or "").lower()
                from_status = (item.get("fromString") or "").lower()
                if not issue_key:
                    issue_key = obj.get("issueKey")
            except json.JSONDecodeError:
                pass

        if not to_status:
            match = re.search(r"→\s*(\w+(?:\s+\w+)?)", text)
            if match:
                to_status = match.group(1).strip().lower()

        # Jira status "Blocked" → ticket is blocked
        if to_status and to_status == "blocked":
            summary = text or f"{issue_key or 'Issue'} is blocked"
            return ("blockers", summary)

        if to_status and to_status in DONE_STATUSES:
            summary = text or f"{issue_key or 'Issue'} completed"
            return ("progress", summary)

        if to_status and to_status in ACTIVE_STATUSES:
            summary = text or f"{issue_key or 'Issue'} in progress"
            return ("next_steps", summary)

    if event_kind == "comment":
        if any(kw in text_lower for kw in BLOCKER_KEYWORDS):
            return ("blockers", text)
        if any(kw in text_lower for kw in DECISION_KEYWORDS):
            return ("decisions", text)
        if any(kw in text_lower for kw in RISK_KEYWORDS):
            return ("risks", text)
        if any(kw in text_lower for kw in NEXT_STEP_KEYWORDS):
            return ("next_steps", text)

    # Slack messages: heuristic fallback (AI extraction handled separately in build_snapshot)
    if event_kind == "message":
        if any(kw in text_lower for kw in BLOCKER_KEYWORDS):
            return ("blockers", text[:500])
        if any(kw in text_lower for kw in DECISION_KEYWORDS):
            return ("decisions", text[:500])
        if any(kw in text_lower for kw in RISK_KEYWORDS):
            return ("risks", text[:500])
        all_next_keywords = NEXT_STEP_KEYWORDS + SLACK_NEXT_STEP_KEYWORDS
        if any(kw in text_lower for kw in all_next_keywords):
            return ("next_steps", text[:500])
        # Fallback: substantial Slack messages (standups, updates) linked to project get a summary
        if len(text_lower) > 150:
            return ("next_steps", text[:500])

    return (None, "")


def build_snapshot_for_project(
    conn: sqlite3.Connection,
    project_id: str,
    project_name: str,
    window_start: datetime,
    window_end: datetime,
    projects_for_ai: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Build status_json for a project from events in the window."""
    rows = conn.execute(
        """
        SELECT e.event_id, e.event_kind, e.actor_display, e.text, e.occurred_at, e.raw_json
        FROM v_project_events e
        WHERE e.project_id = ?
          AND e.occurred_at >= ?
          AND e.occurred_at <= ?
        ORDER BY e.occurred_at DESC
        """,
        (project_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchall()

    if not rows:
        return None

    progress: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    next_steps: List[Dict[str, Any]] = []
    risks: List[Dict[str, Any]] = []
    seen_progress: set = set()
    seen_blockers: set = set()
    seen_decisions: set = set()
    seen_next_steps: set = set()
    seen_risks: set = set()

    for r in rows:
        event_id = r["event_id"]
        event_kind = r["event_kind"]
        actor = r["actor_display"]
        text = r["text"]
        raw_json = r["raw_json"] if "raw_json" in r.keys() else None

        # Slack messages: use AI extraction when enabled (better trimmed status from standups, etc.)
        if event_kind == "message" and AI_ENABLED and _AI_EXTRACT_AVAILABLE and ai_extract_status_from_slack:
            linked_project_ids = [
                r["project_id"] for r in conn.execute(
                    "SELECT project_id FROM event_project_links WHERE event_id = ?", (event_id,)
                ).fetchall()
            ]
            ai_items = ai_extract_status_from_slack(
                text, actor,
                linked_project_ids=linked_project_ids if linked_project_ids else None,
                projects_info=projects_for_ai,
            )
            added_any = False
            for item in ai_items:
                section = item.get("section")
                summary = item.get("text", "")
                owner = item.get("owner") or actor
                item_project_ids = item.get("project_ids") or []
                if not section or not summary:
                    continue
                # When AI assigned project_ids, only add to current project if it's in the list
                if item_project_ids and project_id not in item_project_ids:
                    continue
                added_any = True
                key = (summary[:80], owner or "")
                if section == "progress" and key not in seen_progress:
                    seen_progress.add(key)
                    progress.append({"text": summary[:500], "owner": owner, "event_ids": [event_id]})
                elif section == "blockers" and key not in seen_blockers:
                    seen_blockers.add(key)
                    blockers.append({"text": summary[:500], "owner": owner, "event_ids": [event_id]})
                elif section == "decisions" and key not in seen_decisions:
                    seen_decisions.add(key)
                    decisions.append({"text": summary[:500], "owner": owner, "event_ids": [event_id]})
                elif section == "next_steps" and key not in seen_next_steps:
                    seen_next_steps.add(key)
                    next_steps.append({"text": summary[:500], "owner": owner, "event_ids": [event_id]})
                elif section == "risks" and key not in seen_risks:
                    seen_risks.add(key)
                    risks.append({"text": summary[:500], "event_ids": [event_id]})
            if added_any:
                continue  # AI handled this message
            # AI returned items but none for this project: fall through to heuristic

        section, summary = classify_event(event_kind, text, raw_json, actor)
        if not section or not summary:
            continue

        # Dedupe by normalized summary (first 80 chars)
        key = (summary[:80], actor or "")

        if section == "progress" and key not in seen_progress:
            seen_progress.add(key)
            progress.append({"text": summary[:500], "owner": actor, "event_ids": [event_id]})
        elif section == "blockers" and key not in seen_blockers:
            seen_blockers.add(key)
            blockers.append({"text": summary[:500], "owner": actor, "event_ids": [event_id]})
        elif section == "decisions" and key not in seen_decisions:
            seen_decisions.add(key)
            decisions.append({"text": summary[:500], "owner": actor, "event_ids": [event_id]})
        elif section == "next_steps" and key not in seen_next_steps:
            seen_next_steps.add(key)
            next_steps.append({"text": summary[:500], "owner": actor, "event_ids": [event_id]})
        elif section == "risks" and key not in seen_risks:
            seen_risks.add(key)
            risks.append({"text": summary[:500], "event_ids": [event_id]})

    # Build headline
    parts = []
    if progress:
        parts.append(f"{len(progress)} completed")
    if blockers:
        parts.append(f"{len(blockers)} blocker(s)")
    if next_steps:
        parts.append(f"{len(next_steps)} in progress")
    headline = f"{project_name}: " + "; ".join(parts) if parts else f"{project_name}: Activity in window"

    return {
        "headline": headline,
        "progress": progress[:10],
        "blockers": blockers[:5],
        "decisions": decisions[:5],
        "next_steps": next_steps[:10],
        "risks": risks[:5],
    }


def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    window_end = now
    window_start = now - timedelta(days=WINDOW_DAYS)

    projects = conn.execute(
        "SELECT project_id, name, description FROM projects WHERE is_active = 1"
    ).fetchall()
    projects_for_ai = [
        {"project_id": r["project_id"], "name": r["name"], "description": r["description"] or ""}
        for r in projects
    ]

    if AI_ENABLED:
        print("AI status extraction: enabled (Slack messages, per-project routing)")

    created = 0
    for p in projects:
        project_id = p["project_id"]
        project_name = p["name"]

        status = build_snapshot_for_project(
            conn, project_id, project_name, window_start, window_end,
            projects_for_ai=projects_for_ai,
        )
        if not status:
            continue

        snapshot_id = f"snap_{project_id}_{now.strftime('%Y%m%d_%H%M')}"
        created_iso = now_iso()

        conn.execute(
            """
            INSERT OR REPLACE INTO project_status_snapshots
            (snapshot_id, project_id, snapshot_at, window_start, window_end, status_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                project_id,
                created_iso,
                window_start.isoformat(),
                window_end.isoformat(),
                json.dumps(status, ensure_ascii=False),
                created_iso,
            ),
        )

        # Insert snapshot_evidence for each event in each section
        for section, items in [
            ("progress", status["progress"]),
            ("blockers", status["blockers"]),
            ("decisions", status["decisions"]),
            ("next_steps", status["next_steps"]),
            ("risks", status["risks"]),
        ]:
            for item in items:
                for evt_id in item.get("event_ids", []):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO snapshot_evidence (snapshot_id, event_id, section, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (snapshot_id, evt_id, section, created_iso),
                    )

        # Update project_checkpoints.last_snapshot_at
        conn.execute(
            """
            INSERT INTO project_checkpoints (project_id, last_snapshot_at)
            VALUES (?, ?)
            ON CONFLICT(project_id) DO UPDATE SET last_snapshot_at = excluded.last_snapshot_at
            """,
            (project_id, created_iso),
        )

        created += 1
        print(f"  {project_name}: {status['headline']}")

    conn.commit()
    conn.close()

    print(f"\n✅ Created {created} snapshot(s) for window {window_start.date()} to {window_end.date()}")


if __name__ == "__main__":
    main()
