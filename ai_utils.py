#!/usr/bin/env python3
"""ProjectPulse - AI utilities for LLM-powered classification and extraction.

Uses OpenAI API (or compatible) for:
  - Project attribution: classify Slack messages to relevant projects
  - Status extraction: extract trimmed progress/blockers/decisions from Slack and Jira messages
  - Message formatting: format raw Slack text for clean display in status views

Env: OPENAI_API_KEY (required for AI features). Set AI_ENABLED=1 to use.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

AI_ENABLED = os.environ.get("AI_ENABLED", "0") == "1"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


def ai_format_message_for_status(raw_text: str) -> Optional[str]:
    """
    Format raw Slack message text for clean display in project status views.
    - Preserves project-relevant content (standups, updates, blockers)
    - Normalizes structure (bullets, per-person sections)
    - Removes casual chat and noise
    - Returns None on failure; caller should fall back to raw text.
    """
    if not AI_ENABLED or not OPENAI_API_KEY or not (raw_text or "").strip():
        return None

    client = _client()
    if not client:
        return None

    prompt = f"""Format this Slack message for display in a project status view. Keep it concise and readable.

Rules:
- Preserve all project-relevant content: standup updates, blockers, decisions, next steps
- Keep per-person sections (e.g. *Name*: bullet points) if present
- Normalize bullets and structure for clarity
- Remove casual chat, greetings, and non-status content
- If the message is purely casual (e.g. "hey how are you"), return exactly: [CASUAL]
- Output the formatted text only, no JSON, no explanation. Max 2000 chars.

Raw message:
---
{(raw_text or "")[:4000]}
---"""

    try:
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content == "[CASUAL]":
            return raw_text[:500]  # Keep original for casual messages
        return content[:2000] if content else None
    except Exception:
        return None


def _client():
    """Lazy import to avoid requiring openai when AI is disabled."""
    try:
        from openai import OpenAI
        return OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    except ImportError:
        return None


def ai_classify_message_to_projects(
    message_text: str,
    projects: List[Dict[str, str]],
    eligible_project_ids: List[str],
) -> List[Tuple[str, str, float, str]]:
    """
    Use LLM to classify which project(s) a Slack message relates to.
    projects: [{"project_id": "...", "name": "...", "description": "..."}, ...]
    Returns: [(project_id, attribution_type, confidence, rationale), ...]
    """
    if not AI_ENABLED or not OPENAI_API_KEY:
        return []

    client = _client()
    if not client:
        return []

    projects_str = "\n".join(
        f"- {p['project_id']}: {p.get('name', '')} ({p.get('description', '')[:80]}...)"
        for p in projects if p["project_id"] in eligible_project_ids
    )
    if not projects_str:
        return []

    prompt = f"""You are a project classifier. Given a Slack message and a list of projects, identify which project(s) this message is relevant to.

Projects (only these are valid):
{projects_str}

Slack message:
---
{message_text[:3000]}
---

Respond with a JSON array of matches. Each match: {{"project_id": "...", "confidence": 0.0-1.0, "rationale": "brief reason"}}.
Only include projects that are clearly relevant. Use confidence 0.0-1.0 (0.9+ for strong match, 0.6-0.8 for likely, 0.5 for possible).
If the message is unrelated to any project (e.g. casual chat, weather), return [].
JSON only, no other text."""

    try:
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        # Parse JSON (handle markdown code blocks)
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            return []
        results = []
        for item in parsed:
            pid = item.get("project_id")
            if pid and pid in eligible_project_ids:
                conf = float(item.get("confidence", 0.7))
                conf = max(0.0, min(1.0, conf))
                results.append((
                    pid,
                    "ai_classify",
                    conf,
                    (item.get("rationale") or "AI classification")[:200],
                ))
        return results
    except Exception:
        return []


def ai_extract_status_from_jira(
    text: str,
    event_kind: str,
    actor_display: Optional[str] = None,
    issue_key: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Use LLM to classify a Jira comment or status change into a snapshot section.
    Returns (section, summary_text) or None if unclassified.
    section: progress | blockers | decisions | next_steps | risks
    """
    if not AI_ENABLED or not OPENAI_API_KEY or not (text or "").strip():
        return None

    client = _client()
    if not client:
        return None

    actor = actor_display or "Unknown"
    issue_ref = f" (issue: {issue_key})" if issue_key else ""

    prompt = f"""Classify this Jira activity into exactly one status section.

Sections:
- progress: completed work, shipped items, delivered, closed, done
- blockers: blocked, waiting, stuck, dependencies
- decisions: decisions made, agreements, we will
- next_steps: planned work, in progress, PRs, tickets to create
- risks: risks, delays, dependencies, may delay

Jira {event_kind}{issue_ref} (by {actor}):
---
{(text or "")[:2000]}
---

Respond with JSON: {{"section": "progress|blockers|decisions|next_steps|risks", "summary": "1-2 sentence trimmed summary"}}
If the content is not project-relevant status (e.g. trivial "LGTM"), return {{"section": null, "summary": null}}.
JSON only."""

    try:
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        parsed = json.loads(content)
        section = (parsed.get("section") or "").lower()
        summary = (parsed.get("summary") or "").strip()
        if section not in ("progress", "blockers", "decisions", "next_steps", "risks") or not summary:
            return None
        return (section, summary[:500])
    except Exception:
        return None


def ai_extract_status_from_slack(
    message_text: str,
    actor_display: Optional[str] = None,
    linked_project_ids: Optional[List[str]] = None,
    projects_info: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """
    Use LLM to extract trimmed progress/blockers/decisions/next_steps/risks from a Slack message.
    When linked_project_ids and projects_info are provided, assigns each item to the relevant project(s).
    Returns: [{"section": "...", "text": "...", "owner": "...", "project_ids": ["proj_xxx", ...]}, ...]
    """
    if not AI_ENABLED or not OPENAI_API_KEY:
        return []

    client = _client()
    if not client:
        return []

    actor = actor_display or "Unknown"

    if linked_project_ids and projects_info:
        projects_str = "\n".join(
            f"- {p['project_id']}: {p.get('name', '')} - {(p.get('description') or '')[:60]}"
            for p in projects_info if p["project_id"] in linked_project_ids
        )
        project_instruction = f"""
This message is linked to these projects. Assign each extracted item to the project(s) it relates to using "project_ids": ["proj_xxx", ...].
Projects:
{projects_str}

Include "project_ids" in each output item. Use [] if item is generic/cross-cutting."""
        output_schema = '[{"section": "progress"|"blockers"|"decisions"|"next_steps"|"risks", "text": "trimmed summary", "owner": "name or null", "project_ids": ["proj_xxx"]}]'
    else:
        project_instruction = ""
        output_schema = '[{"section": "progress"|"blockers"|"decisions"|"next_steps"|"risks", "text": "trimmed summary", "owner": "name or null"}]'

    prompt = f"""Extract project status updates from this Slack message. Output structured items.

Sections:
- progress: completed work, shipped items, delivered
- blockers: blocked, waiting, stuck
- decisions: decisions made, agreements
- next_steps: planned work, in progress, PRs, tickets to create
- risks: risks, delays, dependencies

Slack message (from {actor}):
---
{message_text[:4000]}
---
{project_instruction}

For each relevant item, output a brief trimmed summary (1-2 sentences max). Preserve owner if message has per-person sections (e.g. *Gururaj* ...).
Respond with JSON array: {output_schema}
Only include items that are clearly project-relevant. Skip casual chat. Return [] if nothing relevant.
JSON only, no other text."""

    try:
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            return []
        results = []
        for item in parsed:
            section = (item.get("section") or "").lower()
            if section not in ("progress", "blockers", "decisions", "next_steps", "risks"):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            owner = item.get("owner") or actor
            project_ids = item.get("project_ids")
            if not isinstance(project_ids, list):
                project_ids = []
            results.append({
                "section": section,
                "text": text[:500],
                "owner": owner,
                "project_ids": project_ids,
            })
        return results
    except Exception:
        return []
