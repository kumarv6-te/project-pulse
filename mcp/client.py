"""ProjectPulse MCP Client

Lightweight synchronous MCP client that connects to a FastMCP server
over SSE transport and invokes tools via JSON-RPC 2.0.

Requires only the ``requests`` library (no fastmcp/mcp SDK needed).

Usage::

    from mcp.client import MCPClient, format_response

    with MCPClient("http://127.0.0.1:8000/sse") as client:
        raw = client.call_tool("get_project_pulse", {"project_id": "proj_x"})
        print(format_response(raw))
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Optional

import requests


_DEFAULT_URL = "http://127.0.0.1:8000/sse"


class MCPClient:
    """Synchronous MCP-over-SSE client.

    Each instance opens one SSE session, performs the MCP handshake, and
    can then call tools repeatedly until closed.
    """

    def __init__(self, sse_url: str | None = None, *, timeout: int = 60):
        self._sse_url = (
            sse_url
            or os.environ.get("PROJECTPULSE_MCP_URL")
            or _DEFAULT_URL
        )
        self._base_url = self._sse_url.replace("/sse", "").rstrip("/")
        self._timeout = timeout
        self._sse_resp: Optional[requests.Response] = None
        self._events = None
        self._message_url: Optional[str] = None
        self._next_id = 1

    # ── context manager ──────────────────────────────────────────────

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    # ── lifecycle ────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open SSE stream and complete the MCP initialize handshake."""
        self._sse_resp = requests.get(
            f"{self._base_url}/sse",
            stream=True,
            headers={"Accept": "text/event-stream", "Cache-Control": "no-store"},
            timeout=self._timeout,
        )
        self._sse_resp.raise_for_status()
        self._events = _iter_sse(self._sse_resp)

        for etype, edata in self._events:
            if etype == "endpoint":
                path = edata
                self._message_url = (
                    f"{self._base_url}{path}" if path.startswith("/") else path
                )
                break

        if not self._message_url:
            raise RuntimeError("MCP server did not provide a message endpoint")

        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ProjectPulse-UI", "version": "1.0"},
        })

        self._post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })

    def close(self) -> None:
        if self._sse_resp is not None:
            self._sse_resp.close()
            self._sse_resp = None
        self._events = None
        self._message_url = None

    # ── public API ───────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke an MCP tool and return the text result."""
        result = self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        content = result.get("content", [])
        return "\n".join(c["text"] for c in content if c.get("type") == "text")

    # ── internals ────────────────────────────────────────────────────

    def _post(self, body: dict) -> None:
        requests.post(
            self._message_url,
            json=body,
            timeout=self._timeout,
        )

    def _rpc(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for the matching response."""
        msg_id = self._next_id
        self._next_id += 1

        self._post({
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        })

        for etype, edata in self._events:
            if etype == "message":
                msg = json.loads(edata)
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        err = msg["error"]
                        raise RuntimeError(
                            f"MCP error {err.get('code')}: {err.get('message')}"
                        )
                    return msg.get("result", {})

        raise RuntimeError("No response received from MCP server")


def _iter_sse(response: requests.Response):
    """Yield (event_type, data) tuples from a streaming requests response."""
    event_type = None
    data_lines: list[str] = []
    for raw in response.iter_lines(decode_unicode=True):
        if raw == "":
            if data_lines:
                yield (event_type or "message", "\n".join(data_lines))
            event_type = None
            data_lines = []
        elif raw.startswith("event:"):
            event_type = raw[len("event:"):].strip()
        elif raw.startswith("data:"):
            data_lines.append(raw[len("data:"):].strip())


# ── response formatting ─────────────────────────────────────────────


def _humanize_ts(raw: str) -> str:
    """Convert an ISO-8601 timestamp to a human-friendly relative string."""
    try:
        clean = raw.split(".")[0].replace("Z", "+00:00")
        if "+" not in clean and "-" not in clean[10:]:
            clean += "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = delta.total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        if delta.days < 7:
            return f"{delta.days}d ago"
        return dt.strftime("%b %d")
    except (ValueError, TypeError):
        return raw


def format_response(text: str) -> str:
    """Clean up raw MCP tool Markdown for human-friendly display.

    * Converts ISO timestamps to relative times ("3h ago").
    * Collapses duplicate evidence snippets into compact source links.
    * Removes internal attribution metadata.
    * Reformats the event feed into a cleaner layout.
    """

    # ── strip attribution metadata ───────────────────────────────────
    text = re.sub(r"\n\s*_Attribution:.*_", "", text)

    # ── pulse / changes evidence lines ───────────────────────────────
    # "  → [Slack](url) — _duplicate snippet_"  →  "  [📎 Slack](url)"
    text = re.sub(
        r"  → \[(\w+)\]\(([^)]+)\) — _.*_",
        r"  [📎 \1](\2)",
        text,
    )

    # ── blocker evidence blocks ──────────────────────────────────────
    # "- **Source:** [Slack — actor](url)\n  _dup text_"  →  single line
    text = re.sub(
        r"(- \*\*Source:\*\* \[[^\]]+\]\([^)]+\))\n\s+_.*_",
        r"\1",
        text,
    )

    # ── event feed header ────────────────────────────────────────────
    # "- **[Slack]** 2026-02-27T... | actor | kind — Title"
    #  →  "- **actor** · 3h ago · Slack — Title"
    def _fmt_event(m: re.Match) -> str:
        source, ts, actor = m.group(1), m.group(2), m.group(3)
        rest = (m.group(5) or "").strip()
        rest = f" {rest}" if rest else ""
        return f"- **{actor}** · {_humanize_ts(ts)} · {source}{rest}"

    text = re.sub(
        r"- \*\*\[(\w+)\]\*\* (\S+) \| ([^ |]+) \| (\w+)(.*)",
        _fmt_event,
        text,
    )

    # ── standalone [Link](url) → inline  ─────────────────────────────
    text = re.sub(r"\n\s+\[Link\]\(([^)]+)\)", r" [📎](\1)", text)

    # ── humanize "Last activity" timestamps ──────────────────────────
    text = re.sub(
        r"(\*\*Last activity:\*\* )(\S+)",
        lambda m: m.group(1) + _humanize_ts(m.group(2)),
        text,
    )

    # ── humanize snapshot / window timestamps ────────────────────────
    text = re.sub(
        r"\*Snapshot: (\S+)\*",
        lambda m: f"*Snapshot: {_humanize_ts(m.group(1))}*",
        text,
    )

    def _fmt_window(m: re.Match) -> str:
        start = datetime.fromisoformat(
            m.group(1).split(".")[0].replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(
            m.group(2).split(".")[0].replace("Z", "+00:00")
        )
        fmt = "%b %d"
        return f"*Window: {start.strftime(fmt)} → {end.strftime(fmt)}*"

    text = re.sub(r"\*Window: (\S+) → (\S+)\*", _fmt_window, text)

    # ── collapse excessive blank lines ───────────────────────────────
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
