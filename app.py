"""ProjectPulse AI — Streamlit Dashboard

Consumes the Flask REST API (default http://127.0.0.1:5050) to render
five core views:
  1. Project Overview  (latest AI-generated snapshot)
  2. Changes           ("What changed since Monday?")
  3. Blockers          (auto-detected blocker signals)
  4. Weekly Summary    (structured weekly report + chart)
  5. Ask ProjectPulse  (Q&A via MCP server, grounded in project data)
"""

from __future__ import annotations

import datetime
import json
import os
import re

import requests
import streamlit as st
import plotly.graph_objects as go

from mcp.client import MCPClient, format_response

API_BASE = os.environ.get("PROJECTPULSE_API_URL", "http://127.0.0.1:5050")
MCP_URL = os.environ.get("PROJECTPULSE_MCP_URL", "http://127.0.0.1:8000/sse")

SECTION_COLORS = {
    "progress": "#10b981",
    "blockers": "#ef4444",
    "decisions": "#6366f1",
    "next_steps": "#f59e0b",
    "risks": "#f97316",
    "newly_completed": "#10b981",
    "new_blockers": "#ef4444",
    "new_decisions": "#6366f1",
    "other_activity": "#64748b",
}

SECTION_LABELS = {
    "progress": "Progress",
    "blockers": "Blockers",
    "decisions": "Decisions",
    "next_steps": "Next Steps",
    "risks": "Risks",
    "newly_completed": "Newly Completed",
    "new_blockers": "New Blockers",
    "new_decisions": "New Decisions",
    "other_activity": "Other Activity",
}


def api_get(path: str, params: dict | None = None) -> dict | None:
    try:
        resp = requests.get(f"{API_BASE}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        st.error(
            f"Cannot reach the Flask API at **{API_BASE}**. "
            "Make sure it is running (`python run.py` or `python api/app.py`)."
        )
        st.stop()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        st.error(f"API error: {exc}")
        return None


def source_badge(source_type: str) -> str:
    if source_type == "slack":
        return ":violet[Slack]"
    if source_type == "jira":
        return ":blue[Jira]"
    return f":gray[{source_type}]"


def kind_label(kind: str) -> str:
    return kind.replace("_", " ").title()


# ── Pages ────────────────────────────────────────────────────────────


def page_overview(project_id: str, project_name: str):
    st.header(f"Project Overview — {project_name}")

    data = api_get("/api/pulse", {"project_id": project_id})
    if not data:
        st.warning("Project not found.")
        return

    if data.get("snapshot_at") is None:
        st.info(data.get("message", "No status snapshot available yet."))
        return

    headline = data.get("headline", "")
    window = data.get("window", {})
    w_start = (window.get("start") or "")[:10]
    w_end = (window.get("end") or "")[:10]

    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
                    padding: 24px 28px; border-radius: 12px; margin-bottom: 24px;">
            <p style="color:#94a3b8; font-size:0.85rem; margin:0 0 6px 0;">
                Snapshot &nbsp;·&nbsp; {w_start} → {w_end}
            </p>
            <p style="color:#f8fafc; font-size:1.25rem; font-weight:600; margin:0;">
                {headline}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    sections = data.get("sections", {})
    for key in ("progress", "blockers", "decisions", "next_steps", "risks"):
        items = sections.get(key, [])
        label = SECTION_LABELS.get(key, key)
        color = SECTION_COLORS.get(key, "#64748b")

        st.markdown(
            f"<h4 style='color:{color}; margin-bottom:4px;'>{label}</h4>",
            unsafe_allow_html=True,
        )

        if not items:
            st.caption("No items in this section.")
        else:
            for item in items:
                owner = item.get("owner", "")
                owner_md = f" — **{owner}**" if owner else ""
                st.markdown(f"- {item['text']}{owner_md}")
                for ev in item.get("evidence", []):
                    icon = "Slack" if ev["source_type"] == "slack" else "Jira"
                    st.caption(
                        f"&nbsp;&nbsp;→ [{icon}]({ev['permalink']}) — _{ev['snippet']}_"
                    )
        st.divider()


def page_changes(project_id: str, project_name: str):
    st.header(f"Changes — {project_name}")
    st.caption("Filter events by date to see what changed.")

    col1, col2 = st.columns(2)
    with col1:
        preset = st.selectbox(
            "Quick filter",
            ["Since Monday", "Last 7 days", "Last 24 hours", "Custom"],
            index=0,
        )
    with col2:
        today = datetime.date.today()
        if preset == "Custom":
            since_date = st.date_input(
                "Since date", value=today - datetime.timedelta(days=7)
            )
        elif preset == "Since Monday":
            since_date = today - datetime.timedelta(days=today.weekday())
        elif preset == "Last 7 days":
            since_date = today - datetime.timedelta(days=7)
        else:
            since_date = today - datetime.timedelta(days=1)
        st.metric("Showing events since", str(since_date))

    since_iso = f"{since_date}T00:00:00Z"
    data = api_get("/api/changes", {"project_id": project_id, "since": since_iso})
    if not data:
        st.warning("Project not found.")
        return

    total = data.get("total_events", 0)
    if total == 0:
        st.info(f"No events found for **{project_name}** since {since_date}.")
        return

    summary = data.get("activity_summary", {})
    by_source = summary.get("by_source", {})
    by_kind = summary.get("by_kind", {})

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Events", total)
    c2.metric("Sources", ", ".join(f"{k} ({v})" for k, v in by_source.items()))
    c3.metric(
        "Types", ", ".join(f"{kind_label(k)} ({v})" for k, v in by_kind.items())
    )

    sections = data.get("sections", {})
    for key in ("newly_completed", "new_blockers", "new_decisions", "other_activity"):
        items = sections.get(key, [])
        if not items:
            continue
        label = SECTION_LABELS.get(key, key)
        color = SECTION_COLORS.get(key, "#64748b")
        st.markdown(
            f"<h4 style='color:{color};'>{label}</h4>", unsafe_allow_html=True
        )
        for ev in items:
            with st.container(border=True):
                cols = st.columns([1, 3, 2])
                with cols[0]:
                    st.markdown(
                        f"**{source_badge(ev['source_type'])}** · {kind_label(ev['event_kind'])}"
                    )
                with cols[1]:
                    st.markdown(ev["text"])
                with cols[2]:
                    actor = ev.get("actor") or "Unknown"
                    occurred = (ev.get("occurred_at") or "")[:16].replace("T", " ")
                    st.caption(f"{actor} · {occurred}")
                    link = ev.get("permalink")
                    if link:
                        st.markdown(f"[Open source ↗]({link})")


def page_blockers(project_id: str, project_name: str):
    st.header(f"Blockers — {project_name}")
    st.caption(
        "Automatically detected blockers from snapshots and event keyword signals."
    )

    pulse = api_get("/api/pulse", {"project_id": project_id})
    snapshot_blockers = []
    if pulse and pulse.get("sections"):
        snapshot_blockers = pulse["sections"].get("blockers", [])

    events_data = api_get("/api/events", {"project_id": project_id, "limit": 100})
    event_blockers = []
    if events_data:
        kw = {"block", "waiting", "stuck", "dependency", "pending", "flaky"}
        for ev in events_data.get("events", []):
            if any(k in (ev.get("text") or "").lower() for k in kw):
                event_blockers.append(ev)

    total = len(snapshot_blockers) + len(event_blockers)
    if total == 0:
        st.success("No blockers detected. The project is clear!")
        return

    st.error(f"**{total}** potential blocker(s) detected")

    if snapshot_blockers:
        st.subheader("Confirmed Blockers (from latest snapshot)")
        for b in snapshot_blockers:
            with st.container(border=True):
                cols = st.columns([4, 2])
                with cols[0]:
                    st.markdown(f"**{b['text']}**")
                    for ev in b.get("evidence", []):
                        icon = "Slack" if ev["source_type"] == "slack" else "Jira"
                        st.caption(f"→ [{icon}]({ev['permalink']}) — _{ev['snippet']}_")
                with cols[1]:
                    owner = b.get("owner", "Unassigned")
                    st.markdown(f"Owner: **{owner}**")

    if event_blockers:
        st.subheader("Blocker Signals (from event feed)")
        for ev in event_blockers:
            with st.container(border=True):
                cols = st.columns([4, 2, 1])
                with cols[0]:
                    st.markdown(f"{source_badge(ev['source_type'])} {ev['text']}")
                with cols[1]:
                    st.caption(f"Mentioned by **{ev.get('actor', 'Unknown')}**")
                with cols[2]:
                    st.caption((ev.get("occurred_at") or "")[:10])


def page_weekly_summary(project_id: str, project_name: str):
    st.header(f"Weekly Summary — {project_name}")

    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    since_iso = f"{week_start}T00:00:00Z"

    st.info(f"Summary for **{week_start}** to **{today}**")

    changes = api_get("/api/changes", {"project_id": project_id, "since": since_iso})
    pulse = api_get("/api/pulse", {"project_id": project_id})

    sections_data = changes.get("sections", {}) if changes else {}
    pulse_sections = pulse.get("sections", {}) if pulse else {}

    shipped = sections_data.get("newly_completed", [])
    blockers_list = sections_data.get("new_blockers", [])
    decisions_list = sections_data.get("new_decisions", [])
    other = sections_data.get("other_activity", [])

    if not shipped and pulse_sections.get("progress"):
        shipped = [
            {"text": i["text"], "actor": i.get("owner", "")}
            for i in pulse_sections["progress"]
        ]
    if not blockers_list and pulse_sections.get("blockers"):
        blockers_list = [
            {"text": i["text"], "actor": i.get("owner", "")}
            for i in pulse_sections["blockers"]
        ]
    if not decisions_list and pulse_sections.get("decisions"):
        decisions_list = [
            {"text": i["text"], "actor": i.get("owner", "")}
            for i in pulse_sections["decisions"]
        ]

    risks_list = []
    if pulse_sections.get("risks"):
        risks_list = [
            {"text": i["text"], "actor": ""} for i in pulse_sections["risks"]
        ]

    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                    padding: 28px; border-radius: 12px; margin-bottom: 24px;">
            <h3 style="color:#f8fafc; margin:0 0 4px 0;">Week Summary — {project_name}</h3>
            <p style="color:#94a3b8; margin:0;">{week_start} → {today}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    def _render_list(title: str, items: list, color: str):
        st.markdown(f"<h4 style='color:{color};'>{title}</h4>", unsafe_allow_html=True)
        if not items:
            st.caption("None this week.")
            return
        for item in items:
            text = item.get("text", str(item))
            actor = item.get("actor") or item.get("owner", "")
            suffix = f" — *{actor}*" if actor else ""
            st.markdown(f"- {text}{suffix}")

    _render_list("Shipped", shipped, "#10b981")
    _render_list("In Progress", other, "#3b82f6")
    _render_list("Blockers", blockers_list, "#ef4444")
    _render_list("Decisions", decisions_list, "#6366f1")
    _render_list("Risks", risks_list, "#f97316")

    st.divider()

    fig = go.Figure(
        data=[
            go.Bar(
                x=["Shipped", "In Progress", "Blockers", "Decisions", "Risks"],
                y=[
                    len(shipped),
                    len(other),
                    len(blockers_list),
                    len(decisions_list),
                    len(risks_list),
                ],
                marker_color=["#10b981", "#3b82f6", "#ef4444", "#6366f1", "#f97316"],
            )
        ]
    )
    fig.update_layout(
        title="Activity Breakdown",
        yaxis_title="Count",
        template="plotly_dark",
        height=320,
        margin={"l": 40, "r": 20, "t": 50, "b": 40},
    )
    st.plotly_chart(fig, use_container_width=True)


_TOOL_ROUTES: list[tuple[list[str], str, dict]] = [
    (
        ["block", "stuck", "waiting", "impediment"],
        "get_project_blockers",
        {},
    ),
    (
        ["change", "changed", "delta", "missed", "catch me up",
         "since", "happened"],
        "get_project_changes",
        {},
    ),
    (
        ["recent", "latest", "activity", "event", "feed"],
        "get_project_events",
        {"limit": 10},
    ),
    (
        ["who", "people", "team", "contributor"],
        "get_project_events",
        {"limit": 30},
    ),
    (
        ["status", "overview", "summary", "stand", "how is",
         "what's going", "how's", "pulse"],
        "get_project_pulse",
        {},
    ),
    (
        ["risk", "concern", "worry", "threat"],
        "get_project_pulse",
        {},
    ),
    (
        ["decision", "decided", "chose", "selected"],
        "get_project_pulse",
        {},
    ),
    (
        ["next", "upcoming", "plan", "todo", "to do"],
        "get_project_pulse",
        {},
    ),
    (
        ["progress", "done", "completed", "finished"],
        "get_project_pulse",
        {},
    ),
]

# ── natural-language date parsing ────────────────────────────────────

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _parse_since(question: str) -> str:
    """Extract a 'since' ISO date from a natural-language question."""
    q = question.lower()
    today = datetime.datetime.now()

    if "yesterday" in q:
        return (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    if re.search(r"\btoday\b", q):
        return today.strftime("%Y-%m-%d")

    if "this week" in q:
        return (today - datetime.timedelta(days=today.weekday())).strftime("%Y-%m-%d")

    if re.search(r"last\s+week\b", q) and not re.search(r"last\s+\d+\s+week", q):
        return (today - datetime.timedelta(days=today.weekday() + 7)).strftime("%Y-%m-%d")

    m = re.search(r"(?:last|past)\s+(\d+)\s+hours?", q)
    if m:
        return (today - datetime.timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")

    m = re.search(r"(?:last|past)\s+(\d+)\s+days?", q) or re.search(
        r"(\d+)\s+days?\s+ago", q
    )
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")

    m = re.search(r"(?:last|past)\s+(\d+)\s+weeks?", q) or re.search(
        r"(\d+)\s+weeks?\s+ago", q
    )
    if m:
        return (today - datetime.timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")

    for name, dow in _WEEKDAYS.items():
        if name in q:
            days_back = (today.weekday() - dow) % 7
            if days_back == 0:
                days_back = 7
            return (today - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")

    m = re.search(r"(\d{4}-\d{2}-\d{2})", q)
    if m:
        return m.group(1)

    for name, month in _MONTHS.items():
        m = re.search(rf"\b{name}\s+(\d{{1,2}})\b", q)
        if m:
            try:
                return datetime.datetime(
                    today.year, month, int(m.group(1))
                ).strftime("%Y-%m-%d")
            except ValueError:
                pass

    return (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")


def _resolve_tool(project_id: str, question: str) -> tuple[str, dict]:
    """Pick the MCP tool and build arguments for *question*."""
    q = question.lower()
    for keywords, tool, extra in _TOOL_ROUTES:
        if any(kw in q for kw in keywords):
            args = {"project_id": project_id, **extra}
            if tool == "get_project_changes":
                args["since"] = _parse_since(question)
            return tool, args

    return "ask_project", {"project_id": project_id, "question": question}


def page_ask(project_id: str, project_name: str):
    st.header(f"Ask ProjectPulse — {project_name}")
    st.caption(
        "Ask natural-language questions about this project. "
        "Answers are grounded in real project data via the MCP server."
    )

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    for entry in st.session_state.chat_history:
        with st.chat_message(entry["role"]):
            if entry.get("tool"):
                with st.expander(
                    f"🔧  Called **{entry['tool']}** {entry.get('args', '')}",
                    expanded=False,
                ):
                    st.code(json.dumps(entry["args_full"], indent=2), language="json")
            st.markdown(entry["content"])

    question = st.chat_input("e.g. What is the current status of the MVP?")
    if not question:
        st.markdown("**Try asking:**")
        st.markdown("- *What is the current status?*")
        st.markdown("- *Who is blocked and why?*")
        st.markdown("- *What decisions were made this week?*")
        st.markdown("- *What are the risks?*")
        return

    st.session_state.chat_history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    tool_name, tool_args = _resolve_tool(project_id, question)

    with st.chat_message("assistant"):
        with st.expander(
            f"🔧  Calling **{tool_name}**",
            expanded=True,
        ):
            st.code(json.dumps(tool_args, indent=2), language="json")

        with st.spinner(f"Running {tool_name}…"):
            try:
                with MCPClient(MCP_URL) as mcp:
                    raw = mcp.call_tool(tool_name, tool_args)
                result = format_response(raw)
            except Exception as exc:
                result = f"**Error contacting MCP server:** {exc}"

        st.markdown(result)

    st.session_state.chat_history.append({
        "role": "assistant",
        "content": result,
        "tool": tool_name,
        "args_full": tool_args,
    })


# ── Main ─────────────────────────────────────────────────────────────


def main():
    st.set_page_config(
        page_title="ProjectPulse AI",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
        .block-container { padding-top: 2rem; }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%);
        }
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3, [data-testid="stSidebar"] h4,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] .stRadio label,
        [data-testid="stSidebar"] .stRadio div[role="radiogroup"] label span,
        [data-testid="stSidebar"] [data-testid="stCaption"],
        [data-testid="stSidebar"] svg {
            color: #e2e8f0 !important;
            fill: #e2e8f0 !important;
        }
        [data-testid="stSidebar"] .stSelectbox label { color: #94a3b8 !important; }
        [data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] {
            color: #1e293b !important;
        }
        [data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] * {
            color: #1e293b !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        logo_path = os.path.join(os.path.dirname(__file__), "heart-beat-pulse-logo-free-vector.png")
        if os.path.exists(logo_path):
            st.image(logo_path, width=180)
        st.markdown("## ProjectPulse AI")
        st.caption("Real-time project intelligence")
        st.divider()

        data = api_get("/api/projects")
        projects = data.get("projects", []) if data else []

        if not projects:
            st.warning("No active projects found.")
            st.stop()

        project_map = {p["name"]: p["project_id"] for p in projects}
        selected_name = st.selectbox("Select Project", list(project_map.keys()))
        selected_id = project_map[selected_name]

        st.divider()

        page = st.radio(
            "Navigate",
            [
                "Overview",
                "Changes",
                "Blockers",
                "Weekly Summary",
                "Ask ProjectPulse",
            ],
            index=0,
        )

        st.divider()
        st.caption(f"API: {API_BASE}")
        st.caption(f"MCP: {MCP_URL}")
        st.caption("ProjectPulse AI · Hackathon 2026")

    if page == "Overview":
        page_overview(selected_id, selected_name)
    elif page == "Changes":
        page_changes(selected_id, selected_name)
    elif page == "Blockers":
        page_blockers(selected_id, selected_name)
    elif page == "Weekly Summary":
        page_weekly_summary(selected_id, selected_name)
    elif page == "Ask ProjectPulse":
        page_ask(selected_id, selected_name)


if __name__ == "__main__":
    main()
