#!/usr/bin/env python3
"""
concurrent_demo.py
============================
Proves the HTTP/SSE transport handles multiple concurrent clients.

Spins up 5 clients simultaneously, each running a different tool call.
All 5 complete independently without blocking each other.

Usage:
    # Start the server first:
    python server.py --http --port 8765

    # Then run this:
    python concurrent_demo.py
"""

import json
import queue
import threading
import time
import urllib.request

BASE_URL = "http://127.0.0.1:8765"


def make_client() -> tuple[str, queue.Queue]:
    """Connect to SSE and return (client_id, response_queue)."""
    q = queue.Queue()

    def stream():
        try:
            req = urllib.request.Request(f"{BASE_URL}/sse")
            with urllib.request.urlopen(req, timeout=30) as resp:
                buf = ""
                while True:
                    ch = resp.read(1).decode("utf-8", errors="replace")
                    if not ch:
                        break
                    buf += ch
                    if buf.endswith("\n\n"):
                        for line in buf.strip().splitlines():
                            if line.startswith("data:"):
                                q.put(line[5:].strip())
                        buf = ""
                        if q.qsize() >= 3:
                            break
        except Exception as e:
            q.put(json.dumps({"_error": str(e)}))

    t = threading.Thread(target=stream, daemon=True)
    t.start()

    first     = json.loads(q.get(timeout=5))
    client_id = first["client_id"]
    return client_id, q


def rpc(client_id: str, method: str, params: dict, req_id: int,
        q: queue.Queue) -> dict:
    """Send JSON-RPC and return response."""
    msg  = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    body = json.dumps(msg).encode("utf-8")
    url  = f"{BASE_URL}/message?client_id={client_id}"
    req  = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)
    return json.loads(q.get(timeout=10))


def client_worker(client_num: int, results: dict, lock: threading.Lock) -> None:
    """One client running its own sequence of tool calls."""
    start = time.time()

    client_id, q = make_client()

    # Initialize
    rpc(client_id, "initialize", {}, 1, q)

    # Each client calls a different tool
    tool_map = {
        1: ("list_directory", {"path": "."}),
        2: ("get_file_info",  {"path": "."}),
        3: ("list_directory", {"path": ".", "show_hidden": True}),
        4: ("search_files",   {"pattern": "*.py", "max_results": 5}),
        5: ("search_files",   {"pattern": "*.md", "max_results": 5}),
    }

    tool_name, tool_args = tool_map.get(client_num, ("list_directory", {"path": "."}))
    resp = rpc(client_id, "tools/call",
               {"name": tool_name, "arguments": tool_args}, 2, q)

    elapsed = time.time() - start
    success = "result" in resp and not resp["result"].get("isError")

    with lock:
        results[client_num] = {
            "tool":    tool_name,
            "success": success,
            "elapsed": round(elapsed, 3),
        }


def main() -> None:
    print("\n" + "=" * 60)
    print("  Concurrent Client Demo — 5 clients, 5 simultaneous calls")
    print("=" * 60)

    # Check server is up
    try:
        urllib.request.urlopen(f"{BASE_URL}/health", timeout=2)
    except Exception:
        print(f"\n  ERROR: Server not running at {BASE_URL}")
        print("  Start it first:  python server.py --http --port 8765\n")
        return

    results : dict = {}
    lock            = threading.Lock()
    threads         = []

    print("\n  Launching 5 concurrent clients...\n")
    start = time.time()

    for i in range(1, 6):
        t = threading.Thread(
            target=client_worker,
            args=(i, results, lock),
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=15)

    total = time.time() - start

    print(f"  {'Client':<10} {'Tool':<20} {'Result':<10} {'Time':>8}")
    print(f"  {'-'*10} {'-'*20} {'-'*10} {'-'*8}")
    for i in range(1, 6):
        r = results.get(i, {})
        status = "OK" if r.get("success") else "FAIL"
        print(f"  {i:<10} {r.get('tool','?'):<20} {status:<10} {r.get('elapsed',0):>7}s")

    all_ok = all(r.get("success") for r in results.values())
    print(f"\n  Total wall time: {total:.3f}s for 5 concurrent clients")
    print(f"  Result: {'ALL PASSED' if all_ok else 'SOME FAILED'}")
    print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    main()
