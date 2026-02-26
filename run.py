#!/usr/bin/env python3
"""ProjectPulse — Master Launcher

Starts both services in parallel:
  1. Flask API   on port 5050  (serves project data from SQLite)
  2. MCP Server  on port 8000  (LLM-facing tools over SSE, calls Flask API)

Usage:
    python run.py
    python run.py --flask-port 5050 --mcp-port 8000

Press Ctrl+C to stop both.
"""

import argparse
import os
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


def wait_for_flask(port: int, timeout: int = 15):
    """Block until Flask health endpoint responds."""
    url = f"http://127.0.0.1:{port}/api/health"
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
    args = parser.parse_args()

    flask_port = args.flask_port
    mcp_port = args.mcp_port

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

    print(f"[run.py] Starting Flask API on port {flask_port}...")
    flask_env = os.environ.copy()
    flask_env["FLASK_RUN_PORT"] = str(flask_port)
    flask_proc = subprocess.Popen(
        [FLASK_VENV_PYTHON, FLASK_APP, "--port", str(flask_port)],
        env=flask_env,
        cwd=ROOT,
    )
    procs.append(flask_proc)

    print(f"[run.py] Waiting for Flask API to be ready...")
    if not wait_for_flask(flask_port):
        print("[run.py] ERROR: Flask API did not start in time. Aborting.")
        cleanup()
        return

    print(f"[run.py] Flask API ready at http://127.0.0.1:{flask_port}")

    print(f"[run.py] Starting MCP Server (SSE) on port {mcp_port}...")
    mcp_env = os.environ.copy()
    mcp_env["PROJECTPULSE_API_URL"] = f"http://127.0.0.1:{flask_port}"
    mcp_proc = subprocess.Popen(
        [
            MCP_VENV_PYTHON, MCP_SERVER,
            "--transport", "sse",
            "--host", "127.0.0.1",
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

    print()
    print("=" * 60)
    print("  ProjectPulse is running!")
    print(f"  Flask API : http://127.0.0.1:{flask_port}")
    print(f"  MCP Server: http://127.0.0.1:{mcp_port}/sse")
    print("=" * 60)
    print("  Press Ctrl+C to stop both services.")
    print()

    while True:
        if flask_proc.poll() is not None:
            print("[run.py] Flask API exited. Shutting down.")
            cleanup()
        if mcp_proc.poll() is not None:
            print("[run.py] MCP Server exited. Shutting down.")
            cleanup()
        time.sleep(1)


if __name__ == "__main__":
    main()
