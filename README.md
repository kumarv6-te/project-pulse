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

**Option A – Full demo (Slack + Jira sample data):**
```bash
python createdb-insert-sample-data.py
```

**Option B – Bootstrap + live ingest (Jira + Slack):**
```bash
python createdb-bootstrap.py
# Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, then:
python jira_ingest_from_db.py
# Set SLACK_BOT_TOKEN, then:
python slack_ingest_from_db.py
```

For a full refresh including all child issues (e.g. CLOPS-1570, CLOPS-1571, CLOPS-1572), set `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` first:
```bash
python createdb-bootstrap.py && FULL_REFRESH=1 DEBUG=1 python jira_ingest_from_db.py
```

**Option C – Slack ingest (live messages from channels):**

`createdb-bootstrap.py` already adds `slack_channel` scopes. Or use `createdb-insert-sample-data.py` for sample data. Then:
```bash
# Create a Slack app with scopes: channels:history, groups:history, channels:read, users:read
export SLACK_BOT_TOKEN=xoxb-your-bot-token
python slack_ingest_from_db.py
```

Use `INCREMENTAL=1` to fetch only messages after the last run, or `FULL_REFRESH=1` to fetch all messages.

**AI-powered project attribution and status extraction** (optional, uses AWS Bedrock):
```bash
export AI_ENABLED=1
export AWS_REGION=us-east-1
export BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
python slack_ingest_from_db.py      # AI classifies messages to projects (Bedrock)
python generate_status_snapshots.py # AI extracts trimmed progress/blockers/decisions from Slack standups
```

Generate status snapshots from ingested events:
```bash
python generate_status_snapshots.py
```
Use `WINDOW_DAYS=14` to include the last 2 weeks. Run `python run.py` to start the API and MCP server.

This creates `projectpulse_demo.db` (SQLite) and `projectpulse_schema.sql` in the project root with the full schema and sample data: projects (IncidentOps, FedRamp coverage, Langfuse, Cloudability, Puppet migration), events, attribution links, status snapshots, and convenience views.

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

### 6. Run the Streamlit Dashboard

In a separate terminal (with the Flask API already running):

```bash
streamlit run app.py
```

Opens at http://localhost:8501. The dashboard calls the Flask API at `http://127.0.0.1:5050` by default. Override with:

```bash
PROJECTPULSE_API_URL=http://127.0.0.1:6000 streamlit run app.py
```

### Streamlit Dashboard Pages

| Page | Description |
|------|-------------|
| **Overview** | Latest AI-generated snapshot with headline, progress, blockers, decisions, next steps, and risks — with evidence links |
| **Changes** | Time-filtered activity feed ("What changed since Monday?") with source badges and event permalinks |
| **Blockers** | Auto-detected blockers from snapshots and event signals with ownership attribution |
| **Weekly Summary** | Structured weekly report (Shipped / In Progress / Blockers / Decisions / Risks) with activity chart |
| **Ask ProjectPulse** | Natural-language Q&A grounded in real project data |

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
- *"What's the status of Puppet migration?"*
- *"Show blockers for FedRamp coverage"*

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
| `createdb-bootstrap.py` | Creates minimal DB with projects + jira_epic + slack_channel scopes for Jira and Slack ingestion |
| `jira_ingest_from_db.py` | Fetches Jira issues, comments, status changes into events (read-only) |
| `generate_status_snapshots.py` | Synthesizes events into project_status_snapshots (progress, blockers, next_steps) |
| `create-db.py` | Creates empty database with schema only |
| `api/app.py` | Flask REST API |
| `mcp/server.py` | FastMCP server wrapping the Flask API |

---

## Jira ingest options

| Env var | Required | Default | Description |
|---------|----------|---------|-------------|
| `JIRA_BASE_URL` | Yes | — | e.g. `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Yes | — | Atlassian login email |
| `JIRA_API_TOKEN` | Yes | — | API token |
| `DB_PATH` | No | `./projectpulse_demo.db` | Source database path |
| `INCREMENTAL` | No | `0` | If `1`, limits JQL to issues updated since `last_ingested_at` |
| `FULL_REFRESH` | No | `0` | If `1`, ignores `last_ingested_at` and fetches all issues in the epic |
| `DEBUG` | No | `0` | If `1`, prints JQL queries and hit counts for troubleshooting |

---

## Slack ingest options (AI)

| Env var | Default | Description |
|---------|---------|-------------|
| `AI_ENABLED` | `0` | If `1`, use AWS Bedrock to classify messages to projects |
| `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-haiku-20240307-v1:0` | Bedrock model ID |
| `AWS_PROFILE` | — | Optional; for local dev with SSO/profile |

## Snapshot generator options

| Env var | Default | Description |
|---------|---------|-------------|
| `DB_PATH` | `./projectpulse_demo.db` | Database path |
| `WINDOW_DAYS` | `7` | Days of events to include in snapshot (use `14` for 2 weeks) |
| `AI_ENABLED` | `0` | If `1`, use AWS Bedrock to extract status from Slack and Jira |
| `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-haiku-20240307-v1:0` | Bedrock model ID |

---

## Use cases supported

| Use case | Supported | How |
|----------|----------|-----|
| New project lead gets instant context | ✅ | `v_project_latest_snapshot` + `/api/pulse` |
| What changed since Monday? | ✅ | `/api/changes?since=...` or filter `v_project_events` by `occurred_at` |
| Blocker detection & ownership | ✅ | `status_json.blockers` with owner; `/api/blockers` |
| Weekly status auto-generated | ✅ | `generate_status_snapshots.py` (run via cron) |
| Ask ProjectPulse (interactive Q&A) | ✅ | `ask_project` MCP tool + `/api/ask` |
| `create-db.py` | Creates empty database with schema only |
| `API/app.py` | Flask REST API |
| `mcp/server.py` | FastMCP server wrapping the Flask API |
| `app.py` | Streamlit dashboard (calls Flask API) |

---

## Accessing the database with SQLite3

After creating the database (via `createdb-insert-sample-data.py` or `createdb-bootstrap.py` + ingest scripts), you can inspect it from the command line:

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
