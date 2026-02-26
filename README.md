# ProjectPulse AI

**Hackathon project 2026**

ProjectPulse AI is an AI-driven project intelligence tool designed to provide real-time visibility into project progress by aggregating and summarizing updates from multiple collaboration systems. It securely reads signals from sources such as Slack threads, Jira tickets, GitHub repositories, Confluence pages, and Outlook calendars to build a unified, continuously updated view of each project.

Instead of manually tracking status across tools, ProjectPulse AI automatically synthesizes key updates—recent decisions, blockers, delivery progress, and upcoming milestones—into a single, easy-to-consume summary. Project leads can quickly understand where a project stands, what has changed since the last check-in, and what requires attention, all without chasing updates across platforms.

The tool also supports weekly team check-ins by generating structured summaries of contributions, risks, and progress, reducing the overhead of status meetings. Additionally, individuals can use ProjectPulse AI for self-assessment, reflecting on weekly accomplishments, follow-ups, and alignment with goals.

By turning fragmented project activity into actionable insights, ProjectPulse AI enables better decision-making, improves transparency, and helps teams stay aligned in real time.

---

## Features

- **Unified project view** – Aggregates updates from Slack, Jira, GitHub, Confluence, and Outlook
- **Real-time summaries** – Synthesizes decisions, blockers, progress, and milestones
- **Weekly check-ins** – Generates structured summaries for team status meetings
- **Self-assessment** – Tracks weekly accomplishments and goal alignment

---

## Setup

### 1. Clone and create virtual environment

```bash
cd project-pulse
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create database and sample data

```bash
python createdb-insert-sample-data.py
```

This creates `projectpulse_demo.db` (SQLite) and `projectpulse_schema.sql` in the project root with the full schema and sample data: projects (IncidentOps, CostOptimizer), events, attribution links, status snapshots, and convenience views.

---

## Scripts

| Script | Description |
|--------|--------------|
| `createdb-insert-sample-data.py` | Creates the SQLite database, schema, and sample data |



---

## Database schema

- **projects** / **project_scopes** – Projects and their Slack/Jira scopes
- **events** – Unified event log (messages, comments, status changes)
- **event_project_links** – Project attribution for events
- **project_status_snapshots** / **snapshot_evidence** – Time-based status summaries
- **project_checkpoints** – Last viewed/ingested/snapshot times
- **v_project_latest_snapshot** / **v_project_events** – Convenience views
