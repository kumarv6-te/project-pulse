"""ProjectPulse AI — Flask API

Serves project status, pulse snapshots, and event feeds
from the projectpulse_demo.db SQLite database.
"""

import json
import os
import sqlite3

from flask import Flask, g, jsonify, request

app = Flask(__name__)

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "projectpulse_demo.db"
)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- GET /api/projects ----------

@app.route("/api/projects", methods=["GET"])
def list_projects():
    db = get_db()
    rows = db.execute(
        "SELECT project_id, name, description, created_at, is_active "
        "FROM projects WHERE is_active = 1 ORDER BY name"
    ).fetchall()

    return jsonify({
        "projects": [
            {
                "project_id": r["project_id"],
                "name": r["name"],
                "description": r["description"],
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    })


# ---------- GET /api/pulse?project_id=... ----------

@app.route("/api/pulse", methods=["GET"])
def project_pulse():
    db = get_db()
    project_id = request.args.get("project_id")

    if not project_id:
        return jsonify({"error": "Missing required query parameter: project_id"}), 400

    project = db.execute(
        "SELECT project_id, name, description FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if project is None:
        return jsonify({"error": "Project not found", "project_id": project_id}), 404

    snapshot = db.execute(
        "SELECT * FROM v_project_latest_snapshot WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if snapshot is None:
        return jsonify({
            "project_id": project_id,
            "project_name": project["name"],
            "snapshot_at": None,
            "window": None,
            "headline": None,
            "sections": {},
            "message": "No status snapshot available yet for this project.",
        })

    status = json.loads(snapshot["status_json"])

    evidence_rows = db.execute(
        """SELECT se.section, se.event_id,
                  e.source_type, e.actor_display, e.occurred_at,
                  e.permalink, e.text
           FROM snapshot_evidence se
           JOIN events e ON e.event_id = se.event_id
           WHERE se.snapshot_id = ?""",
        (snapshot["snapshot_id"],),
    ).fetchall()

    evidence_by_id = {}
    for er in evidence_rows:
        evidence_by_id[er["event_id"]] = {
            "event_id": er["event_id"],
            "source_type": er["source_type"],
            "actor": er["actor_display"],
            "occurred_at": er["occurred_at"],
            "permalink": er["permalink"],
            "snippet": er["text"],
        }

    def resolve_section(items):
        """Attach full evidence objects to each status bullet."""
        resolved = []
        for item in items:
            entry = {"text": item["text"]}
            if "owner" in item:
                entry["owner"] = item["owner"]
            entry["evidence"] = [
                evidence_by_id[eid]
                for eid in item.get("event_ids", [])
                if eid in evidence_by_id
            ]
            resolved.append(entry)
        return resolved

    section_keys = ["progress", "blockers", "decisions", "next_steps", "risks"]
    sections = {}
    for key in section_keys:
        if key in status and status[key]:
            sections[key] = resolve_section(status[key])

    return jsonify({
        "project_id": project_id,
        "project_name": project["name"],
        "snapshot_at": snapshot["snapshot_at"],
        "window": {
            "start": snapshot["window_start"],
            "end": snapshot["window_end"],
        },
        "headline": status.get("headline"),
        "sections": sections,
    })


# ---------- GET /api/events?project_id=... ----------

@app.route("/api/events", methods=["GET"])
def project_events():
    db = get_db()
    project_id = request.args.get("project_id")

    if not project_id:
        return jsonify({"error": "Missing required query parameter: project_id"}), 400

    project = db.execute(
        "SELECT project_id, name FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if project is None:
        return jsonify({"error": "Project not found", "project_id": project_id}), 404

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    source_type = request.args.get("source_type")

    base_query = (
        "FROM v_project_events WHERE project_id = ?"
    )
    params = [project_id]

    if source_type in ("slack", "jira"):
        base_query += " AND source_type = ?"
        params.append(source_type)

    total = db.execute(
        f"SELECT COUNT(*) AS cnt {base_query}", params
    ).fetchone()["cnt"]

    rows = db.execute(
        f"""SELECT event_id, source_type, occurred_at, container_name,
                   actor_display, event_kind, title, text, permalink,
                   attribution_type, confidence, rationale
            {base_query}
            ORDER BY occurred_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    return jsonify({
        "project_id": project_id,
        "project_name": project["name"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": [
            {
                "event_id": r["event_id"],
                "source_type": r["source_type"],
                "occurred_at": r["occurred_at"],
                "container_name": r["container_name"],
                "actor": r["actor_display"],
                "event_kind": r["event_kind"],
                "title": r["title"],
                "text": r["text"],
                "permalink": r["permalink"],
                "attribution": {
                    "type": r["attribution_type"],
                    "confidence": r["confidence"],
                    "rationale": r["rationale"],
                },
            }
            for r in rows
        ],
    })



# ---------- GET /api/changes?project_id=...&since=... ----------

_BLOCKER_KEYWORDS = {"block", "waiting", "stuck", "flaky", "regression", "fail", "broken", "down"}
_DECISION_KEYWORDS = {"decision", "decided", "adopt", "switch", "selected", "chose", "agreed"}
_COMPLETION_KEYWORDS = {"done", "completed", "merged", "resolved", "shipped", "closed"}


def _classify_event(row):
    text_lower = (row["text"] or "").lower()
    kind = row["event_kind"]

    if kind == "status_change" and any(kw in text_lower for kw in _COMPLETION_KEYWORDS):
        return "newly_completed"
    if any(kw in text_lower for kw in _BLOCKER_KEYWORDS):
        return "new_blockers"
    if any(kw in text_lower for kw in _DECISION_KEYWORDS):
        return "new_decisions"
    return "other_activity"


def _event_to_dict(r):
    return {
        "event_id": r["event_id"],
        "source_type": r["source_type"],
        "occurred_at": r["occurred_at"],
        "container_name": r["container_name"],
        "actor": r["actor_display"],
        "event_kind": r["event_kind"],
        "title": r["title"],
        "text": r["text"],
        "permalink": r["permalink"],
        "attribution": {
            "type": r["attribution_type"],
            "confidence": r["confidence"],
        },
    }


@app.route("/api/changes", methods=["GET"])
def project_changes():
    db = get_db()
    project_id = request.args.get("project_id")
    since = request.args.get("since")

    if not project_id:
        return jsonify({"error": "Missing required query parameter: project_id"}), 400
    if not since:
        return jsonify({"error": "Missing required query parameter: since (ISO-8601)"}), 400

    project = db.execute(
        "SELECT project_id, name FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if project is None:
        return jsonify({"error": "Project not found", "project_id": project_id}), 404

    rows = db.execute(
        """SELECT event_id, source_type, occurred_at, container_name,
                  actor_display, event_kind, title, text, permalink,
                  attribution_type, confidence
           FROM v_project_events
           WHERE project_id = ? AND occurred_at >= ?
           ORDER BY occurred_at DESC""",
        (project_id, since),
    ).fetchall()

    buckets = {
        "newly_completed": [],
        "new_blockers": [],
        "new_decisions": [],
        "other_activity": [],
    }
    by_source = {}
    by_kind = {}

    for r in rows:
        category = _classify_event(r)
        buckets[category].append(_event_to_dict(r))
        src = r["source_type"]
        by_source[src] = by_source.get(src, 0) + 1
        ek = r["event_kind"]
        by_kind[ek] = by_kind.get(ek, 0) + 1

    return jsonify({
        "project_id": project_id,
        "project_name": project["name"],
        "since": since,
        "total_events": len(rows),
        "sections": {k: v for k, v in buckets.items() if v},
        "activity_summary": {
            "total": len(rows),
            "by_source": by_source,
            "by_kind": by_kind,
        },
    })


# ---------- GET /api/blockers?project_id=... ----------

_BLOCKER_DETECT_KEYWORDS = {
    "block", "blocked", "blocking", "waiting", "stuck", "flaky",
    "regression", "fail", "failed", "broken", "down", "pending",
    "unresolved", "investigate", "investigating",
}


def _is_blocker_event(row):
    text_lower = (row["text"] or "").lower()
    kind = row["event_kind"]
    if kind == "status_change" and "block" in text_lower:
        return True
    return any(kw in text_lower for kw in _BLOCKER_DETECT_KEYWORDS)


@app.route("/api/blockers", methods=["GET"])
def project_blockers():
    db = get_db()
    project_id = request.args.get("project_id")

    if not project_id:
        return jsonify({"error": "Missing required query parameter: project_id"}), 400

    project = db.execute(
        "SELECT project_id, name FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if project is None:
        return jsonify({"error": "Project not found", "project_id": project_id}), 404

    snapshot = db.execute(
        "SELECT * FROM v_project_latest_snapshot WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    snapshot_blockers = []
    if snapshot:
        status = json.loads(snapshot["status_json"])
        for item in status.get("blockers", []):
            evidence_events = []
            for eid in item.get("event_ids", []):
                ev = db.execute(
                    """SELECT event_id, source_type, occurred_at, actor_display,
                              text, permalink, container_name
                       FROM events WHERE event_id = ?""",
                    (eid,),
                ).fetchone()
                if ev:
                    evidence_events.append({
                        "event_id": ev["event_id"],
                        "source_type": ev["source_type"],
                        "occurred_at": ev["occurred_at"],
                        "actor": ev["actor_display"],
                        "text": ev["text"],
                        "permalink": ev["permalink"],
                        "container_name": ev["container_name"],
                    })

            last_activity = max(
                (e["occurred_at"] for e in evidence_events), default=None
            )
            snapshot_blockers.append({
                "summary": item["text"],
                "owner": item.get("owner"),
                "source": "snapshot",
                "last_activity": last_activity,
                "evidence": evidence_events,
            })

    seen_event_ids = {
        eid
        for b in snapshot_blockers
        for e in b["evidence"]
        for eid in [e["event_id"]]
    }

    rows = db.execute(
        """SELECT event_id, source_type, occurred_at, container_name,
                  actor_display, event_kind, title, text, permalink,
                  attribution_type, confidence
           FROM v_project_events
           WHERE project_id = ?
           ORDER BY occurred_at DESC""",
        (project_id,),
    ).fetchall()

    detected_blockers = []
    for r in rows:
        if r["event_id"] in seen_event_ids:
            continue
        if not _is_blocker_event(r):
            continue
        detected_blockers.append({
            "summary": r["text"],
            "owner": r["actor_display"],
            "source": r["source_type"],
            "last_activity": r["occurred_at"],
            "evidence": [{
                "event_id": r["event_id"],
                "source_type": r["source_type"],
                "occurred_at": r["occurred_at"],
                "actor": r["actor_display"],
                "text": r["text"],
                "permalink": r["permalink"],
                "container_name": r["container_name"],
            }],
        })

    all_blockers = snapshot_blockers + detected_blockers
    all_blockers.sort(
        key=lambda b: b.get("last_activity") or "", reverse=True
    )

    return jsonify({
        "project_id": project_id,
        "project_name": project["name"],
        "total_blockers": len(all_blockers),
        "blockers": all_blockers,
    })


# ---------- GET /api/ask?project_id=...&question=... ----------

_RECENT_EVENTS_LIMIT = 30


@app.route("/api/ask", methods=["GET"])
def ask_project():
    """Return a comprehensive project context bundle so an LLM can answer
    any free-form question grounded in real data with citations."""

    db = get_db()
    project_id = request.args.get("project_id")
    question = request.args.get("question", "")

    if not project_id:
        return jsonify({"error": "Missing required query parameter: project_id"}), 400

    project = db.execute(
        "SELECT project_id, name, description FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if project is None:
        return jsonify({"error": "Project not found", "project_id": project_id}), 404

    # --- Snapshot pulse ---------------------------------------------------
    snapshot = db.execute(
        "SELECT * FROM v_project_latest_snapshot WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    pulse = None
    if snapshot:
        status = json.loads(snapshot["status_json"])
        evidence_rows = db.execute(
            """SELECT se.section, se.event_id,
                      e.source_type, e.actor_display, e.occurred_at,
                      e.permalink, e.text
               FROM snapshot_evidence se
               JOIN events e ON e.event_id = se.event_id
               WHERE se.snapshot_id = ?""",
            (snapshot["snapshot_id"],),
        ).fetchall()

        evidence_by_id = {}
        for er in evidence_rows:
            evidence_by_id[er["event_id"]] = {
                "event_id": er["event_id"],
                "source_type": er["source_type"],
                "actor": er["actor_display"],
                "occurred_at": er["occurred_at"],
                "permalink": er["permalink"],
                "snippet": er["text"],
            }

        def _resolve(items):
            resolved = []
            for item in items:
                entry = {"text": item["text"]}
                if "owner" in item:
                    entry["owner"] = item["owner"]
                entry["evidence"] = [
                    evidence_by_id[eid]
                    for eid in item.get("event_ids", [])
                    if eid in evidence_by_id
                ]
                resolved.append(entry)
            return resolved

        sections = {}
        for key in ("progress", "blockers", "decisions", "next_steps", "risks"):
            if key in status and status[key]:
                sections[key] = _resolve(status[key])

        pulse = {
            "snapshot_at": snapshot["snapshot_at"],
            "window": {
                "start": snapshot["window_start"],
                "end": snapshot["window_end"],
            },
            "headline": status.get("headline"),
            "sections": sections,
        }

    # --- Active blockers --------------------------------------------------
    blocker_items = []
    if snapshot:
        snap_status = json.loads(snapshot["status_json"])
        for item in snap_status.get("blockers", []):
            ev_list = []
            for eid in item.get("event_ids", []):
                ev = db.execute(
                    """SELECT event_id, source_type, occurred_at, actor_display,
                              text, permalink FROM events WHERE event_id = ?""",
                    (eid,),
                ).fetchone()
                if ev:
                    ev_list.append({
                        "event_id": ev["event_id"],
                        "source_type": ev["source_type"],
                        "occurred_at": ev["occurred_at"],
                        "actor": ev["actor_display"],
                        "text": ev["text"],
                        "permalink": ev["permalink"],
                    })
            blocker_items.append({
                "summary": item["text"],
                "owner": item.get("owner"),
                "evidence": ev_list,
            })

    # --- Recent events ----------------------------------------------------
    event_rows = db.execute(
        """SELECT event_id, source_type, occurred_at, container_name,
                  actor_display, event_kind, title, text, permalink
           FROM v_project_events
           WHERE project_id = ?
           ORDER BY occurred_at DESC
           LIMIT ?""",
        (project_id, _RECENT_EVENTS_LIMIT),
    ).fetchall()

    recent_events = [
        {
            "event_id": r["event_id"],
            "source_type": r["source_type"],
            "occurred_at": r["occurred_at"],
            "container_name": r["container_name"],
            "actor": r["actor_display"],
            "event_kind": r["event_kind"],
            "title": r["title"],
            "text": r["text"],
            "permalink": r["permalink"],
        }
        for r in event_rows
    ]

    # --- Statistics --------------------------------------------------------
    stats_rows = db.execute(
        """SELECT event_kind, COUNT(*) AS cnt
           FROM v_project_events WHERE project_id = ?
           GROUP BY event_kind""",
        (project_id,),
    ).fetchall()
    event_stats = {r["event_kind"]: r["cnt"] for r in stats_rows}

    total_events = db.execute(
        "SELECT COUNT(*) AS cnt FROM v_project_events WHERE project_id = ?",
        (project_id,),
    ).fetchone()["cnt"]

    return jsonify({
        "project_id": project_id,
        "project_name": project["name"],
        "project_description": project["description"],
        "question": question,
        "pulse": pulse,
        "blockers": blocker_items,
        "recent_events": recent_events,
        "stats": {
            "total_events": total_events,
            "by_kind": event_stats,
        },
    })


# ---------- Health ----------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    app.run(host=args.host, debug=False, port=args.port)
