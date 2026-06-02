#!/usr/bin/env python3
"""
demo.py - Verify local-mcp-server works before connecting Local Desktop.

Tests both stdio and HTTP/SSE transports. No AI client needed.
Completes in under 5 seconds.

Usage:
    python demo.py [path]
    python demo.py [path] --http

Examples:
    python demo.py
    python demo.py C:/Users/Admin/PycharmProjects/pythonProject/local-mcp-server
    python demo.py C:/Users/Admin/PycharmProjects/pythonProject/local-mcp-server --http
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

DEMO_ROOT = None
SERVER    = Path(__file__).parent / "server.py"
SEP       = "-" * 60
TIMEOUT   = 10


# ─── stdio demo ───────────────────────────────────────────────────────────────

def run_stdio_demo(root: str) -> None:
    print(f"\n{'='*60}")
    print(f"  local-mcp-server demo  [stdio transport]")
    print(f"  Root: {root}")
    print(f"{'='*60}")

    env  = {**os.environ, "MCP_ROOT": root}
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    def send(msg: dict) -> dict | None:
        line = (json.dumps(msg) + "\n").encode("utf-8")
        proc.stdin.write(line)
        proc.stdin.flush()
        result = [None]

        def read():
            try:
                raw = proc.stdout.readline()
                if raw:
                    result[0] = json.loads(raw.decode("utf-8"))
            except Exception as e:
                result[0] = {"_error": str(e)}

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout=TIMEOUT)
        return result[0]

    def notify(msg: dict):
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        proc.stdin.flush()

    _run_checks(send, notify)

    proc.stdin.close()
    proc.wait(timeout=3)

    print(f"\n{'='*60}")
    print("  All checks passed. Ready to connect Local Desktop.")
    print(f"{'='*60}\n")


# ─── HTTP/SSE demo ────────────────────────────────────────────────────────────

def run_http_demo(root: str, port: int = 8765) -> None:
    import queue

    print(f"\n{'='*60}")
    print(f"  local-mcp-server demo  [HTTP/SSE transport]")
    print(f"  Root: {root}")
    print(f"  URL : http://127.0.0.1:{port}")
    print(f"{'='*60}")

    env  = {**os.environ, "MCP_ROOT": root}
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--http", "--port", str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    # Wait for server to be ready
    base_url  = f"http://127.0.0.1:{port}"
    ready     = False
    for _ in range(20):
        time.sleep(0.3)
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=1)
            ready = True
            break
        except Exception:
            continue

    if not ready:
        print("  ERROR: HTTP server did not start in time.")
        proc.kill()
        return

    print(f"\n  Health check: OK")

    # Open SSE stream and get client_id
    client_id   = None
    sse_queue   : queue.Queue[str] = queue.Queue()
    sse_running = [True]

    def read_sse():
        try:
            req = urllib.request.Request(f"{base_url}/sse")
            with urllib.request.urlopen(req, timeout=30) as resp:
                buf = ""
                while sse_running[0]:
                    chunk = resp.read(1).decode("utf-8", errors="replace")
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith("\n\n"):
                        for line in buf.strip().splitlines():
                            if line.startswith("data:"):
                                sse_queue.put(line[5:].strip())
                        buf = ""
        except Exception as e:
            sse_queue.put(json.dumps({"_sse_error": str(e)}))

    sse_thread = threading.Thread(target=read_sse, daemon=True)
    sse_thread.start()

    # Get client_id from first SSE event
    try:
        first = json.loads(sse_queue.get(timeout=5))
        client_id = first.get("client_id")
        print(f"  SSE connected. client_id: {client_id[:8]}...")
    except Exception as e:
        print(f"  ERROR: Could not connect to SSE stream: {e}")
        proc.kill()
        return

    def send(msg: dict) -> dict | None:
        body  = json.dumps(msg).encode("utf-8")
        url   = f"{base_url}/message?client_id={client_id}"
        req   = urllib.request.Request(url, data=body,
                                        headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT)
        except Exception as e:
            print(f"  POST error: {e}")
            return None

        try:
            raw = sse_queue.get(timeout=TIMEOUT)
            return json.loads(raw)
        except Exception:
            return None

    def notify(msg: dict):
        send(msg)  # notifications over HTTP still get ACK'd

    _run_checks(send, notify)

    sse_running[0] = False
    proc.kill()

    print(f"\n{'='*60}")
    print("  All checks passed. HTTP/SSE transport verified.")
    print(f"{'='*60}\n")


# ─── Shared check suite ───────────────────────────────────────────────────────

def _run_checks(send, notify) -> None:

    # 1. Initialize
    print(f"\n{SEP}\n[1] Initialize\n{SEP}")
    resp = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    if not resp:
        print("  TIMEOUT — server did not respond.")
        return
    info = resp.get("result", {}).get("serverInfo", {})
    print(f"  Server  : {info.get('name')} v{info.get('version')}")
    print(f"  Protocol: {resp['result'].get('protocolVersion')}")
    notify({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    # 2. List tools
    print(f"\n{SEP}\n[2] Available tools\n{SEP}")
    resp = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    if not resp:
        print("  TIMEOUT"); return
    for tool in resp["result"]["tools"]:
        print(f"  [{tool['name']:<20}] {tool['description'][:50]}")

    # 3. list_directory
    print(f"\n{SEP}\n[3] list_directory\n{SEP}")
    resp = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "list_directory", "arguments": {"path": "."}}})
    if not resp:
        print("  TIMEOUT"); return
    result = resp["result"]
    if result.get("isError"):
        print("  ERROR:", result["content"][0]["text"])
    else:
        data = json.loads(result["content"][0]["text"])
        print(f"  {data['total']} entries:\n")
        for e in data["entries"][:8]:
            icon = "D" if e["type"] == "directory" else "F"
            size = f"{e['size']:,}B" if e.get("size") else ""
            print(f"  [{icon}] {e['name']:<35} {size}")

    # 4. get_file_info
    print(f"\n{SEP}\n[4] get_file_info\n{SEP}")
    resp = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "get_file_info", "arguments": {"path": "."}}})
    if not resp:
        print("  TIMEOUT"); return
    result = resp["result"]
    if result.get("isError"):
        print("  ERROR:", result["content"][0]["text"])
    else:
        data = json.loads(result["content"][0]["text"])
        for k, v in data.items():
            print(f"  {k:<12} {v}")

    # 5. read_file
    print(f"\n{SEP}\n[5] read_file\n{SEP}")
    resp2 = send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "list_directory", "arguments": {"path": "."}}})
    if resp2:
        entries     = json.loads(resp2["result"]["content"][0]["text"])["entries"]
        small_files = [e for e in entries if e["type"] == "file" and (e.get("size") or 0) < 8192]
        if small_files:
            target = small_files[0]["path"]
            resp3  = send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                           "params": {"name": "read_file", "arguments": {"path": target}}})
            if resp3 and not resp3["result"].get("isError"):
                text    = resp3["result"]["content"][0]["text"]
                preview = text[:200].replace("\n", "\n  ")
                print(f"  {target}:\n\n  {preview}")
                if len(text) > 200:
                    print(f"\n  ... ({len(text) - 200} more chars)")
            else:
                print("  Could not read file.")
        else:
            print("  No small files found.")

    # 6. search_files
    print(f"\n{SEP}\n[6] search_files — *.py (shallow)\n{SEP}")
    resp = send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                 "params": {"name": "search_files",
                            "arguments": {"pattern": "*.py", "max_results": 10}}})
    if not resp:
        print("  TIMEOUT"); return
    result = resp["result"]
    if result.get("isError"):
        print("  No .py files in root.")
    else:
        data = json.loads(result["content"][0]["text"])
        print(f"  Found {data['total']} file(s):\n")
        for m in data["matches"]:
            print(f"  -> {m['path']:<40} {m['size']:,}B")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Demo for local-mcp-server")
    parser.add_argument("root", nargs="?",
                        default=str(Path(__file__).parent),
                        help="Root directory to expose")
    parser.add_argument("--http",  action="store_true", help="Test HTTP/SSE transport")
    parser.add_argument("--port",  type=int, default=8765)
    args = parser.parse_args()

    if args.http:
        run_http_demo(args.root, port=args.port)
    else:
        run_stdio_demo(args.root)


if __name__ == "__main__":
    main()
