"""ProjectPulse MCP Server

Exposes ProjectPulse Flask API as MCP tools so that LLMs can
query project status, pulse summaries, and event feeds using
natural language.

Usage:
    python mcp/server.py                    # stdio (default)
    python mcp/server.py --transport sse    # SSE for remote clients
"""

from __future__ import annotations

import json
import os
from typing import Optional

import requests
from fastmcp import FastMCP

API_BASE = os.environ.get("PROJECTPULSE_API_URL", "http://127.0.0.1:5050")

mcp = FastMCP(
    name="ProjectPulse",
    instructions=(
        "You are a project intelligence assistant. Use the available tools to "
        "answer questions about project status, progress, blockers, decisions, "
        "risks, and recent activity. Always cite evidence (Slack permalinks or "
        "Jira ticket links) when presenting findings."
    ),
)


def _api_get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def list_projects() -> str:
    """List all active projects tracked by ProjectPulse.

    Use this tool when the user asks:
    - "What projects are being tracked?"
    - "Show me all projects"
    - "Which projects exist?"

    Returns a JSON list of projects with their IDs, names, and descriptions.
    """
    data = _api_get("/api/projects")
    projects = data.get("projects", [])
    if not projects:
        return "No active projects found."

    lines = []
    for p in projects:
        lines.append(
            f"- **{p['name']}** (`{p['project_id']}`): {p.get('description', 'No description')}"
        )
    return "Active projects:\n" + "\n".join(lines)


@mcp.tool()
def get_project_pulse(project_id: str) -> str:
    """Get the current status pulse for a project — a structured summary
    with progress, blockers, decisions, next steps, and risks.

    Use this tool when the user asks:
    - "What's the status of <project>?"
    - "Give me a summary of <project>"
    - "What are the blockers on <project>?"
    - "What decisions have been made?"
    - "What's happening with <project>?"
    - "I just joined <project>, bring me up to speed"

    Each bullet includes evidence links (Slack permalinks or Jira URLs).

    Args:
        project_id: The project identifier (e.g. "proj_incidentops").
                    Use list_projects first if you don't know the ID.
    """
    try:
        data = _api_get("/api/pulse", params={"project_id": project_id})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return f"Project `{project_id}` not found. Use list_projects to see available projects."
        raise

    if data.get("snapshot_at") is None:
        return f"No status snapshot available yet for **{data.get('project_name', project_id)}**."

    lines = [
        f"# {data['project_name']} — Status Pulse",
        f"*Snapshot: {data['snapshot_at']}*  ",
        f"*Window: {data['window']['start']} → {data['window']['end']}*\n",
        f"**{data['headline']}**\n",
    ]

    section_labels = {
        "progress": "Progress",
        "blockers": "Blockers",
        "decisions": "Decisions",
        "next_steps": "Upcoming / Next Steps",
        "risks": "Risks",
    }

    sections = data.get("sections", {})
    for key, label in section_labels.items():
        items = sections.get(key, [])
        if not items:
            continue
        lines.append(f"## {label}")
        for item in items:
            owner = f" (Owner: {item['owner']})" if item.get("owner") else ""
            lines.append(f"- {item['text']}{owner}")
            for ev in item.get("evidence", []):
                icon = "Slack" if ev["source_type"] == "slack" else "Jira"
                lines.append(f"  → [{icon}]({ev['permalink']}) — _{ev['snippet']}_")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_project_events(
    project_id: str,
    source_type: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Get the raw event feed for a project — recent Slack messages,
    Jira status changes, and comments attributed to the project.

    Use this tool when the user asks:
    - "What happened recently on <project>?"
    - "Show me recent Slack messages for <project>"
    - "Show me Jira updates for <project>"
    - "What's the activity feed?"

    Args:
        project_id: The project identifier (e.g. "proj_incidentops").
        source_type: Optional filter — "slack" or "jira". Omit for all sources.
        limit: Max number of events to return (default 20, max 200).
    """
    params: dict = {"project_id": project_id, "limit": min(limit, 200)}
    if source_type in ("slack", "jira"):
        params["source_type"] = source_type

    try:
        data = _api_get("/api/events", params=params)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return f"Project `{project_id}` not found. Use list_projects to see available projects."
        raise

    events = data.get("events", [])
    if not events:
        return f"No events found for **{data.get('project_name', project_id)}**."

    lines = [
        f"# {data['project_name']} — Recent Events ({data['total']} total)\n"
    ]
    for ev in events:
        icon = "Slack" if ev["source_type"] == "slack" else "Jira"
        title = f" — {ev['title']}" if ev.get("title") else ""
        lines.append(
            f"- **[{icon}]** {ev['occurred_at']} | {ev['actor']} | {ev['event_kind']}{title}"
        )
        lines.append(f"  {ev['text']}")
        if ev.get("permalink"):
            lines.append(f"  [Link]({ev['permalink']})")
        conf = ev["attribution"]["confidence"]
        lines.append(
            f"  _Attribution: {ev['attribution']['type']} (confidence {conf:.0%})_"
        )
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_project_changes(
    project_id: str,
    since: str,
) -> str:
    """Get a "what changed" changelog for a project since a given date.
    Returns newly completed items, new blockers, new decisions, and an
    activity summary — like a project changelog.

    Use this tool when the user asks:
    - "What changed since Monday?"
    - "What happened this week on <project>?"
    - "Give me a delta / changelog for <project>"
    - "What's new since <date>?"
    - "Catch me up on what I missed"

    Each item includes evidence links (Slack permalinks or Jira URLs).

    Args:
        project_id: The project identifier (e.g. "proj_incidentops").
                    Use list_projects first if you don't know the ID.
        since: ISO-8601 date or datetime to look back from
               (e.g. "2026-02-23" or "2026-02-23T00:00:00Z").
    """
    try:
        data = _api_get("/api/changes", params={
            "project_id": project_id,
            "since": since,
        })
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return f"Project `{project_id}` not found. Use list_projects to see available projects."
        if exc.response is not None and exc.response.status_code == 400:
            return "Missing parameters. Provide both project_id and since (ISO-8601 date)."
        raise

    total = data.get("total_events", 0)
    if total == 0:
        return f"No changes found for **{data.get('project_name', project_id)}** since {since}."

    lines = [
        f"# {data['project_name']} — Changes Since {data['since']}",
        f"*{total} events detected*\n",
    ]

    section_labels = {
        "newly_completed": "Newly Completed",
        "new_blockers": "New Blockers",
        "new_decisions": "New Decisions",
        "other_activity": "Other Activity",
    }

    sections = data.get("sections", {})
    for key, label in section_labels.items():
        items = sections.get(key, [])
        if not items:
            continue
        lines.append(f"## {label}")
        for ev in items:
            icon = "Slack" if ev["source_type"] == "slack" else "Jira"
            lines.append(f"- {ev['text']} ({ev['actor']})")
            if ev.get("permalink"):
                lines.append(f"  → [{icon}]({ev['permalink']})")
        lines.append("")

    summary = data.get("activity_summary", {})
    by_source = summary.get("by_source", {})
    by_kind = summary.get("by_kind", {})

    lines.append("## Activity Summary")
    if by_source:
        parts = [f"{count} {src}" for src, count in by_source.items()]
        lines.append(f"- By source: {', '.join(parts)}")
    if by_kind:
        parts = [f"{count} {kind}" for kind, count in by_kind.items()]
        lines.append(f"- By type: {', '.join(parts)}")
    lines.append("")

    return "\n".join(lines)


def run_mcp(transport: str = "sse", host: str = "127.0.0.1", port: int = 8000):
    mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ProjectPulse MCP Server")
    parser.add_argument("--transport", default="sse", choices=["sse", "stdio"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run_mcp(transport=args.transport, host=args.host, port=args.port)
