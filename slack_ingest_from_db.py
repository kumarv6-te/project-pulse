#!/usr/bin/env python3
"""ProjectPulse - Slack ingestion from DB channel scopes (SQLite)

Reads project_scopes where source_type='slack' and scope_kind='slack_channel',
fetches messages from each channel via Slack API, and writes into:
  - events
  - event_project_links

Message-to-project attribution (only messages that match are linked):
  - entity_match: Message contains a Jira issue key (e.g. CLOPS-1503) that maps to a project
  - keyword_match: Message contains project-specific keywords (derived from project name)

Messages with no match are stored in events but NOT linked to any project.

Deduping:
- events are deduped by (source_type, source_ref) so re-running is safe.
- source_ref format: {channel_id}:{ts}

Requirements:
  pip install slack_sdk

Env vars:
  SLACK_BOT_TOKEN   Bot token (xoxb-...) with scopes: channels:history, groups:history,
                    channels:read, users:read
  DB_PATH           default ./projectpulse_demo.db

Optional:
  INCREMENTAL=1     If set, uses project_checkpoints.last_ingested_at to fetch only
                    messages after that time (reduces API calls).
  FULL_REFRESH=1    If set, ignores last_ingested_at and fetches all messages.
  DEBUG=1           Print channel fetches and message counts.
  SCOPE_RULE=1      If set, fall back to scope_rule: link ALL messages to all projects
                    with channel in scope (legacy behavior). Default: use matching only.
  AI_ENABLED=1      If set, use OpenAI to classify messages first; rules (Jira/keyword) used only when AI returns nothing.
  OPENAI_API_KEY    Required for AI classification. OPENAI_MODEL defaults to gpt-4o-mini.

Future ML classification options:
  - Embedding similarity: project descriptions + message text -> cosine similarity threshold
  - Fine-tuned classifier: train on labeled (message, project) pairs
  - Zero-shot NLI: "This message discusses {project_name}" with entailment score
  - Hybrid: keyword/Jira as seed, ML to rank or filter
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
import datetime
from typing import Any, Dict, List, Optional, Tuple

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

try:
    from ai_utils import ai_classify_message_to_projects, ai_format_message_for_status
    _AI_AVAILABLE = True
except ImportError:
    ai_classify_message_to_projects = None
    ai_format_message_for_status = None
    _AI_AVAILABLE = False


DB_PATH = os.environ.get("DB_PATH", "./projectpulse_demo.db")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_API_TOKEN")
INCREMENTAL = os.environ.get("INCREMENTAL", "0") == "1"
FULL_REFRESH = os.environ.get("FULL_REFRESH", "0") == "1"
DEBUG = os.environ.get("DEBUG", "0") == "1"
SCOPE_RULE = os.environ.get("SCOPE_RULE", "0") == "1"  # Legacy: link all messages to all projects in scope
AI_ENABLED = os.environ.get("AI_ENABLED", "0") == "1"

# Jira issue key pattern: PROJECT-123
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

# Message subtypes to skip (no useful project context)
SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_name", "channel_purpose",
    "channel_topic", "channel_archive", "channel_unarchive",
    "group_join", "group_leave", "group_name", "group_archive", "group_unarchive",
    "bot_add", "bot_remove",
})


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slack_ts_to_iso(ts: str) -> str:
    """Convert Slack ts (e.g. '1700010000.000200') to ISO8601."""
    try:
        parts = ts.split(".")
        sec = int(parts[0])
        usec = int(parts[1]) if len(parts) > 1 else 0
        dt = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc)
        if usec:
            dt = dt.replace(microsecond=min(usec, 999999))
        return dt.isoformat().replace("+00:00", "Z")
    except (ValueError, IndexError):
        return now_iso()


def make_permalink(channel_id: str, ts: str) -> str:
    """Build Slack message permalink. ts format: 1700010000.000200"""
    ts_clean = ts.replace(".", "")
    return f"https://slack.com/archives/{channel_id}/p{ts_clean}"


def db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_slack_channel_scopes(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Returns [(project_id, channel_id), ...]"""
    rows = conn.execute(
        """
        SELECT project_id, scope_value
        FROM project_scopes
        WHERE source_type='slack' AND scope_kind='slack_channel'
        """
    ).fetchall()
    return [(r["project_id"], r["scope_value"]) for r in rows]


def group_scopes_by_channel(
    scopes: List[Tuple[str, str]],
) -> Dict[str, List[str]]:
    """Group (project_id, channel_id) by channel_id -> [project_id, ...]"""
    by_channel: Dict[str, List[str]] = {}
    for project_id, channel_id in scopes:
        by_channel.setdefault(channel_id, []).append(project_id)
    return by_channel


def load_projects_for_ai(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    """Load project id, name, description for AI classification."""
    return [
        {"project_id": r["project_id"], "name": r["name"], "description": r["description"] or ""}
        for r in conn.execute("SELECT project_id, name, description FROM projects WHERE is_active = 1").fetchall()
    ]


def load_project_matching_metadata(conn: sqlite3.Connection) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Load metadata for message-to-project matching.
    Returns:
      jira_key_to_projects: project_key (e.g. CLOPS) -> [project_id]
      project_keywords: project_id -> [keyword] (lowercase, from project name)
    """
    jira_key_to_projects: Dict[str, List[str]] = {}
    project_keywords: Dict[str, List[str]] = {}

    # Jira epic scopes: CLOPS-1447 -> project_key CLOPS, project_id proj_incidentops
    for row in conn.execute(
        "SELECT project_id, scope_value FROM project_scopes WHERE source_type='jira' AND scope_kind='jira_epic'"
    ).fetchall():
        project_id, epic_key = row[0], row[1]
        if "-" in epic_key:
            project_key = epic_key.split("-", 1)[0].upper()
            jira_key_to_projects.setdefault(project_key, []).append(project_id)

    # Keywords from project name: extract significant terms (4+ chars, alphanumeric)
    for row in conn.execute("SELECT project_id, name FROM projects").fetchall():
        project_id, name = row[0], (row[1] or "")
        words = re.findall(r"[A-Za-z0-9]+", name)
        keywords = [w.lower() for w in words if len(w) >= 4]
        # Dedupe and add project key if we have it
        seen = set(keywords)
        for pk, pids in jira_key_to_projects.items():
            if project_id in pids and pk.lower() not in seen:
                keywords.append(pk.lower())
                break
        project_keywords[project_id] = list(dict.fromkeys(keywords)) if keywords else []

    return jira_key_to_projects, project_keywords


def match_message_to_projects(
    text: str,
    eligible_project_ids: List[str],
    jira_key_to_projects: Dict[str, List[str]],
    project_keywords: Dict[str, List[str]],
) -> List[Tuple[str, str, float, str]]:
    """Match message text to projects. Returns [(project_id, attribution_type, confidence, rationale), ...].
    Only considers projects in eligible_project_ids (those with this channel in scope).
    """
    text_lower = text.lower()
    matches: Dict[str, Tuple[str, float, str]] = {}  # project_id -> (attribution_type, confidence, rationale)

    # 1. Jira key matching (entity_match) - highest confidence
    for m in JIRA_KEY_RE.finditer(text):
        jira_key = m.group(1)
        project_key = jira_key.split("-", 1)[0].upper()
        for project_id in jira_key_to_projects.get(project_key, []):
            if project_id in eligible_project_ids:
                matches[project_id] = (
                    "entity_match",
                    1.0,
                    f"Contains Jira issue key {jira_key} (maps to project)",
                )

    # 2. Keyword matching - lower confidence, only if not already matched by entity
    for project_id in eligible_project_ids:
        if project_id in matches:
            continue
        keywords = project_keywords.get(project_id, [])
        for kw in keywords:
            if kw in text_lower:
                matches[project_id] = (
                    "keyword_match",
                    0.75,
                    f"Contains project keyword '{kw}'",
                )
                break

    return [(pid, att, conf, rat) for pid, (att, conf, rat) in matches.items()]


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
# Slack API helpers
# -----------------------------
def _is_channel_id(scope_value: str) -> bool:
    """Slack channel IDs start with C (public) or G (private) followed by alphanumeric."""
    if not scope_value or len(scope_value) < 9:
        return False
    return scope_value[0] in ("C", "G") and scope_value[1:].replace("-", "").isalnum()


def resolve_channel_id(client: WebClient, scope_value: str, cache: Dict[str, str]) -> Optional[str]:
    """Resolve scope_value to channel ID. If it's already an ID, return as-is. Else look up by name."""
    if _is_channel_id(scope_value):
        return scope_value
    if scope_value in cache:
        return cache[scope_value]
    try:
        cursor = None
        while True:
            resp = client.conversations_list(types="public_channel,private_channel", limit=200, cursor=cursor)
            for ch in (resp.get("channels") or []):
                if (ch.get("name") or "").lower() == scope_value.lower():
                    ch_id = ch.get("id")
                    if ch_id:
                        cache[scope_value] = ch_id
                        return ch_id
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return None
    except SlackApiError:
        return None


def get_channel_name(client: WebClient, channel_id: str) -> str:
    """Resolve channel ID to display name."""
    try:
        resp = client.conversations_info(channel=channel_id)
        ch = (resp.get("channel") or {})
        return ch.get("name") or channel_id
    except SlackApiError:
        return channel_id


def get_user_display(client: WebClient, user_id: str, cache: Dict[str, str]) -> str:
    """Resolve user ID to display name, with caching."""
    if user_id in cache:
        return cache[user_id]
    try:
        resp = client.users_info(user=user_id)
        u = (resp.get("user") or {})
        profile = u.get("profile") or {}
        name = profile.get("display_name") or profile.get("real_name") or u.get("name") or user_id
        cache[user_id] = name
        return name
    except SlackApiError:
        cache[user_id] = user_id
        return user_id


def fetch_channel_messages(
    client: WebClient,
    channel_id: str,
    oldest_ts: Optional[str] = None,
    limit_per_page: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch messages from a channel with pagination."""
    messages: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while True:
        kwargs: Dict[str, Any] = {
            "channel": channel_id,
            "limit": limit_per_page,
        }
        if oldest_ts:
            kwargs["oldest"] = oldest_ts
        if cursor:
            kwargs["cursor"] = cursor

        resp = client.conversations_history(**kwargs)
        batch = resp.get("messages", [])
        messages.extend(batch)

        meta = resp.get("response_metadata") or {}
        cursor = meta.get("next_cursor")
        if not cursor or not batch:
            break

    return messages


def should_skip_message(msg: Dict[str, Any]) -> bool:
    """Skip join/leave and other non-content messages."""
    subtype = (msg.get("subtype") or "").strip()
    if subtype and subtype in SKIP_SUBTYPES:
        return True
    # Skip messages with no text (e.g. some file-only posts)
    text = (msg.get("text") or "").strip()
    if not text:
        return True
    return False


def message_to_text(msg: Dict[str, Any]) -> str:
    """Extract displayable text from a Slack message (max 2000 chars)."""
    text = (msg.get("text") or "").strip()
    if not text:
        return "(no text)"
    return text[:2000]


# -----------------------------
# Ingestion logic
# -----------------------------
def ingest_channel(
    conn: sqlite3.Connection,
    client: WebClient,
    *,
    channel_id: str,
    project_ids: List[str],
    ingested_at: str,
    user_cache: Dict[str, str],
    jira_key_to_projects: Dict[str, List[str]],
    project_keywords: Dict[str, List[str]],
    projects_for_ai: List[Dict[str, str]],
) -> Dict[str, int]:
    """Ingest messages from a Slack channel into events and event_project_links.
    Links each message only to projects that match (Jira key or keyword).
    Use SCOPE_RULE=1 for legacy behavior (link all to all)."""
    counters = {"messages": 0, "skipped_existing": 0, "skipped_filter": 0, "no_match": 0}

    # oldest_ts = max of last_ingested_at across all projects for this channel
    oldest_ts: Optional[str] = None
    if INCREMENTAL and not FULL_REFRESH:
        for project_id in project_ids:
            last = get_last_ingested_at(conn, project_id)
            if last:
                try:
                    dt = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
                    ts_val = dt.timestamp()
                    if oldest_ts is None or ts_val > float(oldest_ts):
                        oldest_ts = str(ts_val)
                except Exception:
                    pass

    channel_name = get_channel_name(client, channel_id)
    messages = fetch_channel_messages(client, channel_id, oldest_ts=oldest_ts)

    if DEBUG:
        print(f"    Channel {channel_id} ({channel_name}): {len(messages)} messages fetched")

    for msg in messages:
        if should_skip_message(msg):
            counters["skipped_filter"] += 1
            continue

        ts = msg.get("ts") or ""
        if not ts:
            continue

        source_ref = f"{channel_id}:{ts}"
        if event_exists(conn, "slack", source_ref):
            counters["skipped_existing"] += 1
            continue

        # Resolve actor
        actor_id: Optional[str] = msg.get("user")
        actor_display: Optional[str] = None
        if actor_id:
            actor_display = get_user_display(client, actor_id, user_cache)
        else:
            # Bot message
            actor_id = msg.get("bot_id") or msg.get("user")
            actor_display = msg.get("username") or actor_id

        occurred_at = slack_ts_to_iso(ts)
        raw_text = message_to_text(msg)
        # Optionally format text for clean display in status views (AI_ENABLED)
        if AI_ENABLED and _AI_AVAILABLE and ai_format_message_for_status:
            formatted = ai_format_message_for_status(raw_text)
            text = formatted if formatted else raw_text
        else:
            text = raw_text
        permalink = make_permalink(channel_id, ts)
        event_id = make_event_id("slack", source_ref)

        insert_event(
            conn,
            event_id=event_id,
            source_type="slack",
            source_ref=source_ref,
            occurred_at=occurred_at,
            ingested_at=ingested_at,
            container_id=channel_id,
            container_name=channel_name,
            actor_id=actor_id,
            actor_display=actor_display,
            event_kind="message",
            title=None,
            text=text,
            permalink=permalink,
            raw_obj=msg,
        )

        # Determine which projects to link: matching or scope_rule (legacy)
        if SCOPE_RULE:
            links = [(pid, "scope_rule", 1.0, f"Message in channel {channel_name} (slack_channel scope)") for pid in project_ids]
        else:
            # AI first: use LLM to classify when enabled; fall back to rules only when AI returns nothing
            links: List[Tuple[str, str, float, str]] = []
            if AI_ENABLED and _AI_AVAILABLE and ai_classify_message_to_projects:
                ai_links = ai_classify_message_to_projects(text, projects_for_ai, project_ids)
                for pid, att, conf, rat in ai_links:
                    if conf >= 0.5:  # Only use AI matches above threshold
                        links.append((pid, att, conf, rat))
            if not links:
                links = match_message_to_projects(text, project_ids, jira_key_to_projects, project_keywords)

        for project_id, attribution_type, confidence, rationale in links:
            link_event_to_project(
                conn,
                event_id=event_id,
                project_id=project_id,
                attribution_type=attribution_type,
                confidence=confidence,
                rationale=rationale,
                created_at=ingested_at,
            )

        if links:
            counters["messages"] += 1
        else:
            counters["no_match"] += 1

    return counters


def main() -> None:
    if not SLACK_BOT_TOKEN:
        print("Error: SLACK_BOT_TOKEN or SLACK_API_TOKEN env var required.")
        print("Create a Slack app with scopes: channels:history, groups:history, channels:read, users:read")
        return

    conn = db_connect(DB_PATH)
    scopes = get_slack_channel_scopes(conn)
    if not scopes:
        print("No slack_channel scopes found in DB. Add rows in project_scopes first.")
        print("Example: INSERT INTO project_scopes(scope_id, project_id, source_type, scope_kind, scope_value, created_at)")
        print("  VALUES ('scope_slack_1', 'proj_xxx', 'slack', 'slack_channel', 'C0XXXXXX', datetime('now'));")
        return

    client = WebClient(token=SLACK_BOT_TOKEN)
    ingested_at = now_iso()
    user_cache: Dict[str, str] = {}
    channel_id_cache: Dict[str, str] = {}

    # Resolve channel names to IDs (scope_value can be name or ID)
    resolved_scopes: List[Tuple[str, str]] = []
    for project_id, scope_value in scopes:
        ch_id = resolve_channel_id(client, scope_value, channel_id_cache)
        if ch_id:
            resolved_scopes.append((project_id, ch_id))
        else:
            print(f"  Skipping {project_id}: could not resolve channel '{scope_value}' to ID")

    if not resolved_scopes:
        print("No slack_channel scopes could be resolved. Check channel name/ID and bot permissions.")
        return

    jira_key_to_projects, project_keywords = load_project_matching_metadata(conn)
    projects_for_ai = load_projects_for_ai(conn)
    if DEBUG:
        print(f"  Jira keys: {dict(jira_key_to_projects)}")
        print(f"  Project keywords (sample): {list(project_keywords.items())[:3]}")
    if AI_ENABLED:
        print("  AI classification: enabled (OPENAI_API_KEY)")

    print(f"DB: {DB_PATH}")
    print(f"Found {len(scopes)} slack_channel scopes -> {len(resolved_scopes)} resolved.")
    print("Attribution: entity_match (Jira keys) + keyword_match (project name). Use SCOPE_RULE=1 for legacy.")
    if INCREMENTAL:
        print("INCREMENTAL=1 -> limiting fetch by project_checkpoints.last_ingested_at")
    if FULL_REFRESH:
        print("FULL_REFRESH=1 -> fetching all messages (ignoring last_ingested_at)")
    if DEBUG:
        print("DEBUG=1 -> printing channel fetches")
    print()

    total = {"messages": 0, "skipped_existing": 0, "skipped_filter": 0, "no_match": 0}
    by_channel = group_scopes_by_channel(resolved_scopes)

    for channel_id, project_ids in by_channel.items():
        print(f"== Ingesting channel {channel_id} for projects {project_ids} ==")
        try:
            counters = ingest_channel(
                conn,
                client,
                channel_id=channel_id,
                project_ids=project_ids,
                ingested_at=ingested_at,
                user_cache=user_cache,
                jira_key_to_projects=jira_key_to_projects,
                project_keywords=project_keywords,
                projects_for_ai=projects_for_ai,
            )
            conn.commit()
            for project_id in project_ids:
                set_last_ingested_at(conn, project_id, ingested_at)
            conn.commit()

            print(
                f"  linked to projects: {counters['messages']}\n"
                f"  no match (stored):  {counters['no_match']}\n"
                f"  skipped existing:   {counters['skipped_existing']}\n"
                f"  skipped filter:    {counters['skipped_filter']}"
            )
            for k in total:
                total[k] += counters[k]
        except SlackApiError as e:
            print(f"  ERROR: {e.response.get('error', 'unknown')} - {e.response.get('error_detail', '')}")
            if DEBUG:
                print(f"  Full response: {e.response}")

    print("== Done ==")
    print(
        f"Total linked:       {total['messages']}\n"
        f"Total no match:    {total['no_match']}\n"
        f"Total skipped:     {total['skipped_existing']}\n"
        f"Total filtered:    {total['skipped_filter']}"
    )

    conn.close()


if __name__ == "__main__":
    main()
