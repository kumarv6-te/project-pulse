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

### 4. Set up MCP virtual environment (requires Python 3.10+)

```bash
python3.12 -m venv mcp/venv
source mcp/venv/bin/activate
pip install fastmcp requests
deactivate
```

### 5. Run ProjectPulse

```bash
python run.py
```

This starts both services:

| Service | URL | Description |
|---------|-----|-------------|
| Flask API | http://0.0.0.0:5050 | REST API serving project data from SQLite |
| MCP Server | http://0.0.0.0:8000/sse | LLM-facing tools over SSE, calls the Flask API |

Custom ports:

```bash
python run.py --flask-port 6000 --mcp-port 9000
```

Press `Ctrl+C` to stop both services.

---

## Querying with natural language

### Cursor IDE

A `.cursor/mcp.json` is included. After running `python run.py`, reload Cursor (`Cmd+Shift+P` → "Reload Window"). Then ask naturally in chat:

- *"What projects are being tracked?"*
- *"What's the status of IncidentOps?"*
- *"Show me the blockers"*
- *"What Jira updates happened recently?"*
- *"What changed on IncidentOps since Monday?"*
- *"Catch me up on what I missed this week"*
- *"What are the blockers on IncidentOps?"*
- *"Is anything stuck or blocked?"*
- *"Show me blockers and who owns them"*
- *"What is the current status of the MVP?"*
- *"Are we on track for launch?"*
- *"Summarise IncidentOps for the leadership meeting"*

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "projectpulse": {
      "url": "http://0.0.0.0:8000/sse"
    }
  }
}
```

### MCP Inspector (testing)

```bash
npx @modelcontextprotocol/inspector
```

Open http://localhost:6274, select **SSE** transport, connect to `http://0.0.0.0:8000/sse`.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/projects` | List all active projects |
| GET | `/api/pulse?project_id=...` | Structured status pulse with evidence links |
| GET | `/api/events?project_id=...` | Event feed (optional: `source_type`, `limit`, `offset`) |

| GET | `/api/changes?project_id=...&since=...` | Delta changelog — newly completed, new blockers, new decisions, activity summary |
| GET | `/api/blockers?project_id=...` | Active blockers with ownership, last activity, and source evidence |
| GET | `/api/ask?project_id=...&question=...` | Full project context bundle for interactive Q&A (pulse, blockers, events, stats) |
| GET | `/api/health` | Health check |

---

## MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `list_projects` | *(none)* | List all active projects |
| `get_project_pulse` | `project_id` | Status summary with progress, blockers, decisions, risks |
| `get_project_events` | `project_id`, `source_type?`, `limit?` | Raw event feed from Slack and Jira |
| `get_project_changes` | `project_id`, `since` | Delta changelog — what changed since a given date |
| `get_project_blockers` | `project_id` | Active blockers with ownership, last activity, and evidence links |
| `ask_project` | `project_id`, `question` | Interactive Q&A — answers any question grounded in real project data with citations |

---

## Scripts

| Script | Description |
|--------|-------------|
| `run.py` | Master launcher — starts Flask API and MCP Server |
| `createdb-insert-sample-data.py` | Creates the SQLite database, schema, and sample data |
| `create-db.py` | Creates empty database with schema only |
| `API/app.py` | Flask REST API |
| `mcp/server.py` | FastMCP server wrapping the Flask API |

---

## Accessing the database with SQLite3

After running `createdb-insert-sample-data.py`, you can inspect the database from the command line:

```bash
# Open the database (from project root)
sqlite3 projectpulse_demo.db
```

Useful commands inside the `sqlite3` shell:

```sql
-- List all tables
.tables

-- Show schema for a table
.schema projects

-- List projects
SELECT project_id, name, description FROM projects;

-- List events with project attribution (convenience view)
SELECT project_id, event_kind, actor_display, title, text
FROM v_project_events
ORDER BY occurred_at;

-- Events for a specific project (e.g. IncidentOps)
SELECT event_kind, actor_display, text, occurred_at
FROM v_project_events
WHERE project_id = 'proj_incidentops'
ORDER BY occurred_at;

-- Latest status snapshot per project
SELECT project_id, snapshot_at, status_json
FROM v_project_latest_snapshot;

-- Project scopes (Slack channels, Jira epics)
SELECT p.name, ps.source_type, ps.scope_kind, ps.scope_value
FROM projects p
JOIN project_scopes ps ON ps.project_id = p.project_id;
```

Exit the shell with `.quit` or `Ctrl+D`.

---

## Database schema

- **projects** / **project_scopes** – Projects and their Slack/Jira scopes
- **events** – Unified event log (messages, comments, status changes)
- **event_project_links** – Project attribution for events
- **project_status_snapshots** / **snapshot_evidence** – Time-based status summaries
- **project_checkpoints** – Last viewed/ingested/snapshot times
- **v_project_latest_snapshot** / **v_project_events** – Convenience views
