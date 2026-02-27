#!/usr/bin/env python3
"""ProjectPulse — Master Launcher

Starts all services in parallel:
  1. Flask API        on port 5050  (serves project data from SQLite)
  2. MCP Server       on port 8000  (LLM-facing tools over SSE, calls Flask API)
  3. Streamlit UI     on port 8501  (interactive dashboard, calls Flask API)
  4. MCP Inspector    on port 6274  (dev tool to test MCP tools, connects to MCP Server)

Usage:
    python run.py
    python run.py --flask-port 5050 --mcp-port 8000 --ui-port 8501
    python run.py --no-ui              # skip Streamlit UI
    python run.py --no-inspector       # skip MCP Inspector

Press Ctrl+C to stop all services.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))

FLASK_VENV_PYTHON = os.path.join(ROOT, "venv", "bin", "python")
MCP_VENV_PYTHON = os.path.join(ROOT, "mcp", "venv", "bin", "python")

FLASK_APP = os.path.join(ROOT, "API", "app.py")
MCP_SERVER = os.path.join(ROOT, "mcp", "server.py")
STREAMLIT_APP = os.path.join(ROOT, "app.py")

NPX = shutil.which("npx")


def wait_for_http(url: str, timeout: int = 15) -> bool:
    """Block until an HTTP endpoint responds with 200."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser(description="ProjectPulse Master Launcher")
    parser.add_argument("--flask-port", type=int, default=5050)
    parser.add_argument("--mcp-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=8501)
    parser.add_argument("--inspector-port", type=int, default=6274)
    parser.add_argument("--no-ui", action="store_true", help="Skip launching the Streamlit UI")
    parser.add_argument("--no-inspector", action="store_true", help="Skip launching MCP Inspector")
    args = parser.parse_args()

    flask_port = args.flask_port
    mcp_port = args.mcp_port
    ui_port = args.ui_port
    inspector_port = args.inspector_port
    launch_ui = not args.no_ui
    launch_inspector = not args.no_inspector

    procs: list[subprocess.Popen] = []

    def cleanup(signum=None, frame=None):
        print("\n[run.py] Shutting down...")
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # ── 1. Flask API ─────────────────────────────────────────────────
    print(f"[run.py] Starting Flask API on port {flask_port}...")
    flask_env = os.environ.copy()
    flask_env["FLASK_RUN_PORT"] = str(flask_port)
    flask_proc = subprocess.Popen(
        [FLASK_VENV_PYTHON, FLASK_APP, "--host", "0.0.0.0", "--port", str(flask_port)],
        env=flask_env,
        cwd=ROOT,
    )
    procs.append(flask_proc)

    print("[run.py] Waiting for Flask API to be ready...")
    if not wait_for_http(f"http://127.0.0.1:{flask_port}/api/health"):
        print("[run.py] ERROR: Flask API did not start in time. Aborting.")
        cleanup()
        return

    print(f"[run.py] Flask API ready at http://0.0.0.0:{flask_port}")

    # ── 2. MCP Server ───────────────────────────────────────────────
    print(f"[run.py] Starting MCP Server (SSE) on port {mcp_port}...")
    mcp_env = os.environ.copy()
    mcp_env["PROJECTPULSE_API_URL"] = f"http://127.0.0.1:{flask_port}"
    mcp_proc = subprocess.Popen(
        [
            MCP_VENV_PYTHON, MCP_SERVER,
            "--transport", "sse",
            "--host", "0.0.0.0",
            "--port", str(mcp_port),
        ],
        env=mcp_env,
        cwd=ROOT,
    )
    procs.append(mcp_proc)

    time.sleep(2)

    if flask_proc.poll() is not None:
        print("[run.py] ERROR: Flask API exited unexpectedly.")
        cleanup()
        return
    if mcp_proc.poll() is not None:
        print("[run.py] ERROR: MCP Server exited unexpectedly.")
        cleanup()
        return

    # ── 3. Streamlit UI ─────────────────────────────────────────────
    ui_proc = None
    if launch_ui:
        print(f"[run.py] Starting Streamlit UI on port {ui_port}...")
        ui_env = os.environ.copy()
        ui_env["PROJECTPULSE_API_URL"] = f"http://127.0.0.1:{flask_port}"
        ui_proc = subprocess.Popen(
            [
                FLASK_VENV_PYTHON, "-m", "streamlit", "run", STREAMLIT_APP,
                "--server.port", str(ui_port),
                "--server.address", "0.0.0.0",
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
            ],
            env=ui_env,
            cwd=ROOT,
        )
        procs.append(ui_proc)

        print("[run.py] Waiting for Streamlit UI to be ready...")
        if wait_for_http(f"http://127.0.0.1:{ui_port}/_stcore/health", timeout=20):
            print(f"[run.py] Streamlit UI ready at http://0.0.0.0:{ui_port}")
        else:
            print("[run.py] WARNING: Streamlit UI health check timed out (may still be starting).")

    # ── 4. MCP Inspector ─────────────────────────────────────────────
    inspector_proc = None
    if launch_inspector:
        if NPX is None:
            print("[run.py] WARNING: npx not found — skipping MCP Inspector. Install Node.js to enable it.")
        else:
            print(f"[run.py] Starting MCP Inspector on port {inspector_port}...")
            inspector_env = os.environ.copy()
            inspector_env["CLIENT_PORT"] = str(inspector_port)
            inspector_env["SERVER_PORT"] = str(inspector_port + 3)
            inspector_env["HOST"] = "0.0.0.0"
            inspector_env["MCP_AUTO_OPEN_ENABLED"] = "false"
            inspector_env["DANGEROUSLY_OMIT_AUTH"] = "true"
            inspector_proc = subprocess.Popen(
                [NPX, "-y", "@modelcontextprotocol/inspector"],
                env=inspector_env,
                cwd=ROOT,
            )
            procs.append(inspector_proc)

            if wait_for_http(f"http://127.0.0.1:{inspector_port}", timeout=30):
                print(f"[run.py] MCP Inspector ready at http://0.0.0.0:{inspector_port}")
            else:
                print("[run.py] WARNING: MCP Inspector health check timed out (may still be starting).")

    # ── Banner ───────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  ProjectPulse is running!")
    print()
    print(f"  Flask API      : http://0.0.0.0:{flask_port}")
    print(f"  MCP Server     : http://0.0.0.0:{mcp_port}/sse")
    if launch_ui:
        print(f"  Streamlit UI   : http://0.0.0.0:{ui_port}")
    if inspector_proc is not None:
        print(f"  MCP Inspector  : http://0.0.0.0:{inspector_port}")
    print()
    print("=" * 60)
    print("  Press Ctrl+C to stop all services.")
    print()

    # ── Monitor ──────────────────────────────────────────────────────
    while True:
        if flask_proc.poll() is not None:
            print("[run.py] Flask API exited. Shutting down.")
            cleanup()
        if mcp_proc.poll() is not None:
            print("[run.py] MCP Server exited. Shutting down.")
            cleanup()
        if ui_proc is not None and ui_proc.poll() is not None:
            print("[run.py] Streamlit UI exited. Shutting down.")
            cleanup()
        if inspector_proc is not None and inspector_proc.poll() is not None:
            print("[run.py] MCP Inspector exited (non-critical, continuing).")
            inspector_proc = None
        time.sleep(1)


if __name__ == "__main__":
    main()
