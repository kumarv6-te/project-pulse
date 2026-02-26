#!/usr/bin/env python3
"""ProjectPulse - Jira ingestion from DB epic scopes (SQLite)

Updated to ingest CHILD issue activity under each epic:
- Finds all child issues for each epic scope (e.g., CLOPS-1447 includes CLOPS-1572)
- For EACH child issue (and its subtasks):
    - ingests ALL comments
    - ingests changelog items (currently: status changes; extendable)

Writes into SQLite tables:
  - events
  - event_project_links

Deduping:
- events are deduped by (source_type, source_ref) so re-running is safe.

Requirements:
  pip install requests

Env vars:
  DB_PATH         default ./projectpulse_demo.db
  JIRA_BASE_URL   e.g. https://yourcompany.atlassian.net
  JIRA_EMAIL      your Atlassian login email
  JIRA_API_TOKEN  your API token

Optional:
  INCREMENTAL=1   If set, uses project_checkpoints.last_ingested_at to limit the JQL to recently updated issues.
                  Comments/status changes are still deduped, so this is safe and reduces API calls.
  FULL_REFRESH=1  If set, ignores last_ingested_at and fetches all issues in the epic (use when child issues are missing).
  DEBUG=1         Print JQL queries and hit counts to diagnose why child issues (e.g. CLOPS-1570) are not found.

Notes:
- Jira Cloud v3 comment bodies are ADF JSON. We convert to plain text for events.text
  and store the raw comment in raw_json.
"""

from __future__ import annotations

import os
import json
import base64
import sqlite3
import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests


DB_PATH = os.environ.get("DB_PATH", "./projectpulse_demo.db")
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
INCREMENTAL = os.environ.get("INCREMENTAL", "0") == "1"
FULL_REFRESH = os.environ.get("FULL_REFRESH", "0") == "1"
DEBUG = os.environ.get("DEBUG", "0") == "1"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def adf_to_plain_text(adf: Any, max_len: int = 2000) -> str:
    """
    Extract plain text from Atlassian Document Format (ADF) JSON.
    Recursively traverses doc/paragraph/text nodes. Returns empty string if invalid.
    """
    if not adf or not isinstance(adf, dict):
        return ""

    parts: List[str] = []

    def _walk(node: Any, in_block: bool = False) -> None:
        if not isinstance(node, dict):
            return
        node_type = node.get("type") or ""
        content = node.get("content") or []

        if node_type == "text":
            parts.append(node.get("text") or "")
        elif node_type == "hardBreak":
            parts.append("\n")
        elif node_type == "mention":
            attrs = node.get("attrs") or {}
            parts.append(attrs.get("text") or attrs.get("id") or "")
        elif node_type == "emoji":
            attrs = node.get("attrs") or {}
            parts.append(attrs.get("shortName") or "")
        elif node_type in ("paragraph", "heading", "listItem", "tableCell", "tableHeader"):
            for child in content:
                _walk(child, in_block=True)
            if content:
                parts.append("\n")
        elif node_type in ("bulletList", "orderedList"):
            for child in content:
                _walk(child, in_block=True)
        elif node_type in ("doc", "blockquote", "panel", "expand", "nestedExpand"):
            for child in content:
                _walk(child, in_block=in_block)
        elif node_type == "table":
            for row in content:
                for cell in (row.get("content") or []):
                    _walk(cell, in_block=True)
        elif node_type == "tableRow":
            for child in content:
                _walk(child, in_block=True)
        elif node_type == "codeBlock":
            for child in content:
                if isinstance(child, dict) and child.get("type") == "text":
                    parts.append(child.get("text") or "")
            parts.append("\n")
        else:
            for child in content:
                _walk(child, in_block=in_block)

    _walk(adf)
    result = "".join(parts).strip()
    result = " ".join(result.split())  # normalize whitespace
    return result[:max_len] if result else ""


def jira_headers() -> Dict[str, str]:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{JIRA_BASE_URL}{path}"
    r = requests.get(url, headers=jira_headers(), params=params, timeout=45)
    r.raise_for_status()
    return r.json()


def jql_search(jql: str, fields: List[str], max_total: int = 1000) -> List[Dict[str, Any]]:
    """
    Search issues via JQL. Uses /rest/api/3/search/jql (legacy /search was removed).
    Pagination uses nextPageToken instead of startAt.
    """
    issues: List[Dict[str, Any]] = []
    next_page_token: Optional[str] = None

    while True:
        params: Dict[str, Any] = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": min(50, max_total - len(issues)),
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        data = jira_get("/rest/api/3/search/jql", params=params)
        batch = data.get("issues", [])
        issues.extend(batch)
        next_page_token = data.get("nextPageToken")

        if len(batch) == 0 or not next_page_token or len(issues) >= max_total:
            break

    return issues[:max_total]


def fetch_issue_with_changelog(issue_key: str) -> Dict[str, Any]:
    return jira_get(
        f"/rest/api/3/issue/{issue_key}",
        params={
            "fields": "summary,issuetype,project,subtasks,updated",
            "expand": "changelog",
        },
    )


def fetch_all_comments(issue_key: str, max_total: int = 1000) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []
    start_at = 0
    while True:
        data = jira_get(
            f"/rest/api/3/issue/{issue_key}/comment",
            params={"startAt": start_at, "maxResults": 50},
        )
        batch = data.get("comments", [])
        comments.extend(batch)
        start_at += len(batch)
        if len(batch) == 0 or start_at >= data.get("total", 0) or len(comments) >= max_total:
            break
    return comments[:max_total]


def _maybe_updated_clause(last_ingested_at: Optional[str]) -> str:
    if FULL_REFRESH or not (INCREMENTAL and last_ingested_at):
        return ""
    try:
        dt = datetime.datetime.fromisoformat(last_ingested_at.replace("Z", "+00:00"))
        return f' AND updated >= "{dt.strftime("%Y-%m-%d %H:%M")}"'
    except Exception:
        return ""


def issues_in_epic(epic_key: str, last_ingested_at: Optional[str]) -> List[Dict[str, Any]]:
    """Find issues in an epic across common Jira project types."""
    updated_clause = _maybe_updated_clause(last_ingested_at)
    fields = ["summary", "issuetype", "project", "subtasks", "updated"]

    all_issues: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    jqls = [
        f'"Epic Link" = {epic_key}{updated_clause}',  # company-managed epic link
        f"parentEpic = {epic_key}{updated_clause}",   # team-managed
        f"parent = {epic_key}{updated_clause}",       # parent link (child work items)
    ]

    for jql in jqls:
        try:
            hits = jql_search(jql, fields=fields)
            if DEBUG:
                keys = [i.get("key") for i in hits if i.get("key")]
                print(f"  JQL: {jql[:80]}... -> {len(hits)} hits: {keys[:10]}{'...' if len(keys) > 10 else ''}")
        except requests.HTTPError as e:
            if DEBUG:
                print(f"  JQL failed: {jql[:60]}... -> {e.response.status_code} {e.response.text[:200]}")
            hits = []
        for issue in hits:
            key = issue.get("key")
            if key and key not in seen:
                seen.add(key)
                all_issues.append(issue)

    if DEBUG and all_issues:
        print(f"  Total issues found: {sorted(i.get('key') for i in all_issues if i.get('key'))}")

    return all_issues


# -----------------------------
# DB helpers
# -----------------------------
def db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_epic_scopes(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT project_id, scope_value
        FROM project_scopes
        WHERE source_type='jira' AND scope_kind='jira_epic'
        """
    ).fetchall()
    return [(r["project_id"], r["scope_value"]) for r in rows]


def get_last_ingested_at(conn: sqlite3.Connection, project_id: str) -> Optional[str]:
    r = conn.execute(
        "SELECT last_ingested_at FROM project_checkpoints WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return r["last_ingested_at"] if r else None


def set_last_ingested_at(conn: sqlite3.Connection, project_id: str, ts: str) -> None:
    conn.execute(
        """
        INSERT INTO project_checkpoints(project_id, last_ingested_at)
        VALUES (?, ?)
        ON CONFLICT(project_id) DO UPDATE SET last_ingested_at=excluded.last_ingested_at
        """,
        (project_id, ts),
    )


def event_exists(conn: sqlite3.Connection, source_type: str, source_ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE source_type=? AND source_ref=? LIMIT 1",
        (source_type, source_ref),
    ).fetchone()
    return row is not None


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    source_type: str,
    source_ref: str,
    occurred_at: str,
    ingested_at: str,
    container_id: Optional[str],
    container_name: Optional[str],
    actor_id: Optional[str],
    actor_display: Optional[str],
    event_kind: str,
    title: Optional[str],
    text: str,
    permalink: Optional[str],
    raw_obj: Dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO events(
          event_id, source_type, source_ref, occurred_at, ingested_at,
          container_id, container_name, actor_id, actor_display,
          event_kind, title, text, permalink, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            source_type,
            source_ref,
            occurred_at,
            ingested_at,
            container_id,
            container_name,
            actor_id,
            actor_display,
            event_kind,
            title,
            text,
            permalink,
            json.dumps(raw_obj, ensure_ascii=False),
        ),
    )


def link_event_to_project(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    project_id: str,
    attribution_type: str,
    confidence: float,
    rationale: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO event_project_links(
          event_id, project_id, attribution_type, confidence, rationale, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (event_id, project_id, attribution_type, confidence, rationale, created_at),
    )


def make_event_id(prefix: str, source_ref: str) -> str:
    safe = source_ref.replace(":", "_").replace("/", "_")
    return f"{prefix}_{safe}"[:120]


# -----------------------------
# Ingestion logic
# -----------------------------
def ingest_issue_activity(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    epic_key: str,
    issue_key: str,
    ingested_at: str,
) -> Dict[str, int]:
    counters = {"comments": 0, "status_changes": 0, "skipped_existing": 0}

    issue = fetch_issue_with_changelog(issue_key)
    fields = issue.get("fields", {}) or {}

    proj = fields.get("project") or {}
    jira_project_key = proj.get("key")
    jira_project_name = proj.get("name")

    issue_url = f"{JIRA_BASE_URL}/browse/{issue_key}"

    # Comments
    try:
        comments = fetch_all_comments(issue_key)
    except requests.HTTPError:
        comments = []

    for c in comments:
        comment_id = c.get("id")
        if not comment_id:
            continue

        source_ref = f"{issue_key}:comment:{comment_id}"
        if event_exists(conn, "jira", source_ref):
            counters["skipped_existing"] += 1
            continue

        created = c.get("created") or ingested_at
        author = c.get("author") or {}
        author_id = author.get("accountId")
        author_name = author.get("displayName")

        # Jira Cloud v3 comment body is ADF JSON. Extract plain text for events.text.
        body = c.get("body")
        plain = adf_to_plain_text(body) if body else ""
        text = (
            f"{issue_key} comment by {author_name}: {plain}"
            if plain
            else f"{issue_key} comment by {author_name}: (no text)"
        )

        event_id = make_event_id("jira", source_ref)
        insert_event(
            conn,
            event_id=event_id,
            source_type="jira",
            source_ref=source_ref,
            occurred_at=created,
            ingested_at=ingested_at,
            container_id=jira_project_key,
            container_name=jira_project_name,
            actor_id=author_id,
            actor_display=author_name,
            event_kind="comment",
            title=f"{issue_key} comment",
            text=text,
            permalink=issue_url,
            raw_obj={"issueKey": issue_key, "comment": c},
        )
        link_event_to_project(
            conn,
            event_id=event_id,
            project_id=project_id,
            attribution_type="scope_rule",
            confidence=1.0,
            rationale=f"Issue belongs to epic {epic_key} (jira_epic scope)",
            created_at=ingested_at,
        )
        counters["comments"] += 1

    # Status changes from changelog
    changelog = issue.get("changelog") or {}
    histories = changelog.get("histories") or []
    for h in histories:
        history_id = h.get("id")
        created = h.get("created")
        items = h.get("items") or []
        author = h.get("author") or {}
        author_id = author.get("accountId")
        author_name = author.get("displayName")

        for idx, it in enumerate(items):
            if (it.get("field") or "").lower() != "status":
                continue

            from_s = it.get("fromString")
            to_s = it.get("toString")
            source_ref = f"{issue_key}:status:{history_id}:{idx}"
            if event_exists(conn, "jira", source_ref):
                counters["skipped_existing"] += 1
                continue

            text = f"{issue_key} status changed: {from_s} → {to_s}"
            event_id = make_event_id("jira", source_ref)
            insert_event(
                conn,
                event_id=event_id,
                source_type="jira",
                source_ref=source_ref,
                occurred_at=created or ingested_at,
                ingested_at=ingested_at,
                container_id=jira_project_key,
                container_name=jira_project_name,
                actor_id=author_id,
                actor_display=author_name,
                event_kind="status_change",
                title=f"{issue_key} status",
                text=text,
                permalink=issue_url,
                raw_obj={"issueKey": issue_key, "history": h, "item": it},
            )
            link_event_to_project(
                conn,
                event_id=event_id,
                project_id=project_id,
                attribution_type="scope_rule",
                confidence=1.0,
                rationale=f"Issue belongs to epic {epic_key} (jira_epic scope)",
                created_at=ingested_at,
            )
            counters["status_changes"] += 1

    return counters


def ingest_epic(conn: sqlite3.Connection, project_id: str, epic_key: str) -> Dict[str, int]:
    counters = {"issues_touched": 0, "comments": 0, "status_changes": 0, "skipped_existing": 0}
    ingested_at = now_iso()

    last_ingested_at = get_last_ingested_at(conn, project_id)
    if DEBUG:
        print(f"  last_ingested_at: {last_ingested_at}, INCREMENTAL: {INCREMENTAL}, FULL_REFRESH: {FULL_REFRESH}")

    # Find child issues in epic (CLOPS-1447 -> CLOPS-1572 etc.)
    if DEBUG:
        print(f"  Running JQL to find issues in epic {epic_key}:")
    base_issues = issues_in_epic(epic_key, last_ingested_at)
    issue_keys: Set[str] = {i["key"] for i in base_issues if i.get("key")}

    # Include subtasks from the search results
    for i in base_issues:
        subs = (i.get("fields", {}) or {}).get("subtasks", []) or []
        for st in subs:
            if st.get("key"):
                issue_keys.add(st["key"])

    # Include the epic itself
    issue_keys.add(epic_key)

    processed: Set[str] = set()
    while True:
        pending = sorted(issue_keys - processed)
        if not pending:
            break

        for issue_key in pending:
            processed.add(issue_key)
            counters["issues_touched"] += 1

            c = ingest_issue_activity(
                conn,
                project_id=project_id,
                epic_key=epic_key,
                issue_key=issue_key,
                ingested_at=ingested_at,
            )
            counters["comments"] += c["comments"]
            counters["status_changes"] += c["status_changes"]
            counters["skipped_existing"] += c["skipped_existing"]

            # Discover subtasks from full issue fetch
            try:
                issue_full = fetch_issue_with_changelog(issue_key)
                subs2 = ((issue_full.get("fields") or {}).get("subtasks") or [])
                for st in subs2:
                    k = st.get("key")
                    if k:
                        issue_keys.add(k)
            except Exception:
                pass

    set_last_ingested_at(conn, project_id, ingested_at)
    return counters


def main() -> None:
    conn = db_connect(DB_PATH)
    scopes = get_epic_scopes(conn)
    if not scopes:
        print("No jira_epic scopes found in DB. Add rows in project_scopes first.")
        return

    print(f"DB: {DB_PATH}")
    print(f"Found {len(scopes)} jira_epic scopes.")
    if INCREMENTAL:
        print("INCREMENTAL=1 -> limiting JQL by project_checkpoints.last_ingested_at (safe; still dedupes).")
    if FULL_REFRESH:
        print("FULL_REFRESH=1 -> fetching all issues (ignoring last_ingested_at).")
    if DEBUG:
        print("DEBUG=1 -> printing JQL and hit counts.")
    print()

    total = {"issues_touched": 0, "comments": 0, "status_changes": 0, "skipped_existing": 0}

    for project_id, epic_key in scopes:
        print(f"== Ingesting epic {epic_key} for project {project_id} ==")
        counters = ingest_epic(conn, project_id, epic_key)
        conn.commit()
        print(
            f"  issues_touched: {counters['issues_touched']}\n"
            f"  new comments:   {counters['comments']}\n"
            f"  new statuses:   {counters['status_changes']}\n"
            f"  skipped:        {counters['skipped_existing']}"
        )
        for k in total:
            total[k] += counters[k]

    print("== Done ==")
    print(
        f"Total issues touched: {total['issues_touched']}\n"
        f"Total new comments:   {total['comments']}\n"
        f"Total new statuses:   {total['status_changes']}\n"
        f"Total skipped:        {total['skipped_existing']}"
    )

    conn.close()


if __name__ == "__main__":
    main()