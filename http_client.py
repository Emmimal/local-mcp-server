#!/usr/bin/env python3
"""
examples/http_client.py
=======================
A minimal Python client for the local-mcp-server HTTP/SSE transport.

Shows how any custom AI client can connect to the server
without Local Desktop — using only Python stdlib.

Usage:
    # Start the server first:
    python server.py --http --port 8765

    # Then run this client:
    python examples/http_client.py
"""

import json
import queue
import threading
import urllib.request
from typing import Any

BASE_URL = "http://127.0.0.1:8765"


class MCPClient:
    """
    Minimal MCP client over HTTP/SSE.

    Connects to the SSE stream, gets a client_id, then sends
    JSON-RPC messages via POST /message and reads responses
    from the SSE queue.

    Zero dependencies — pure Python stdlib.
    """

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url  = base_url
        self.client_id = None
        self._queue    : queue.Queue[str] = queue.Queue()
        self._running  = False
        self._thread   = None

    def connect(self, timeout: float = 5.0) -> None:
        """Open the SSE stream and wait for client_id."""
        self._running = True
        self._thread  = threading.Thread(target=self._stream_sse, daemon=True)
        self._thread.start()

        # Block until we receive the client_id
        first = json.loads(self._queue.get(timeout=timeout))
        if "client_id" not in first:
            raise RuntimeError(f"Unexpected first SSE event: {first}")
        self.client_id = first["client_id"]
        print(f"[client] Connected. client_id={self.client_id[:8]}...")

    def _stream_sse(self) -> None:
        try:
            req = urllib.request.Request(f"{self.base_url}/sse")
            with urllib.request.urlopen(req, timeout=60) as resp:
                buf = ""
                while self._running:
                    ch = resp.read(1).decode("utf-8", errors="replace")
                    if not ch:
                        break
                    buf += ch
                    if buf.endswith("\n\n"):
                        for line in buf.strip().splitlines():
                            if line.startswith("data:"):
                                self._queue.put(line[5:].strip())
                        buf = ""
        except Exception as e:
            self._queue.put(json.dumps({"_error": str(e)}))

    def send(self, method: str, params: dict, req_id: int = 1,
             timeout: float = 10.0) -> dict:
        """Send a JSON-RPC request and return the response."""
        if not self.client_id:
            raise RuntimeError("Not connected. Call connect() first.")

        msg  = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        body = json.dumps(msg).encode("utf-8")
        url  = f"{self.base_url}/message?client_id={self.client_id}"
        req  = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=timeout)
        raw = self._queue.get(timeout=timeout)
        return json.loads(raw)

    def notify(self, method: str, params: dict) -> None:
        """Send a notification (no response expected)."""
        msg  = {"jsonrpc": "2.0", "method": method, "params": params}
        body = json.dumps(msg).encode("utf-8")
        url  = f"{self.base_url}/message?client_id={self.client_id}"
        req  = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)

    def close(self) -> None:
        self._running = False


def main() -> None:
    print("\n" + "=" * 60)
    print("  MCP HTTP/SSE Client Example")
    print("=" * 60)

    client = MCPClient()

    # 1. Connect
    print("\n[1] Connecting to server...")
    client.connect()

    # 2. Initialize
    resp = client.send("initialize", {}, req_id=1)
    info = resp["result"]["serverInfo"]
    print(f"[2] Server: {info['name']} v{info['version']}")
    client.notify("notifications/initialized", {})

    # 3. List tools
    resp  = client.send("tools/list", {}, req_id=2)
    tools = resp["result"]["tools"]
    print(f"[3] Tools: {[t['name'] for t in tools]}")

    # 4. list_directory
    resp   = client.send("tools/call",
                         {"name": "list_directory", "arguments": {"path": "."}},
                         req_id=3)
    data   = json.loads(resp["result"]["content"][0]["text"])
    print(f"[4] Root has {data['total']} entries:")
    for e in data["entries"][:5]:
        icon = "D" if e["type"] == "directory" else "F"
        print(f"    [{icon}] {e['name']}")

    # 5. get_file_info
    resp = client.send("tools/call",
                       {"name": "get_file_info", "arguments": {"path": "."}},
                       req_id=4)
    info = json.loads(resp["result"]["content"][0]["text"])
    print(f"[5] Root info: type={info['type']}, readable={info['readable']}")

    client.close()

    print("\n" + "=" * 60)
    print("  Done. HTTP/SSE transport verified.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
