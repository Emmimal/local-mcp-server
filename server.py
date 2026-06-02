"""
local-mcp-server
================
A production-grade MCP server for secure local file system access.

  - Zero dependencies  — pure Python standard library (3.8+)
  - Two transports     — stdio (Local Desktop) or HTTP/SSE (concurrent clients)
  - Path sandbox       — every request validated against MCP_ROOT
  - Battle-tested      — 40+ tests, Windows/macOS/Linux

Transport selection:
    stdio (default)      python server.py
    HTTP/SSE             python server.py --http --port 8765

Author : Your Name
License: MIT
GitHub : https://github.com/YOUR_USERNAME/local-mcp-server
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import platform
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs


# ─── Constants ────────────────────────────────────────────────────────────────

JSONRPC_VERSION = "2.0"
MCP_VERSION     = "2024-11-05"
SERVER_NAME     = "local-mcp-server"
SERVER_VERSION  = "1.0.0"
MAX_FILE_BYTES  = 1_048_576   # 1 MB
MAX_SEARCH_HITS = 200
DEFAULT_PORT    = 8765


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    log = logging.getLogger("mcp")
    log.setLevel(level)
    if not log.handlers:
        fh = logging.FileHandler("mcp_server.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)
    return log

log = _setup_logging()


# ─── Security ─────────────────────────────────────────────────────────────────

def is_safe_path(base: Path, target: Path) -> bool:
    """
    Block path traversal attacks by verifying the fully-resolved
    target lives strictly inside the allowed base directory.

    Handles:
      - Classic traversal : ../../etc/passwd
      - Symlink escapes   : link -> /etc
      - Windows UNC paths : \\\\server\\share
    """
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def resolve_safe(raw: str, root: Path) -> tuple[Path | None, str | None]:
    """Resolve a user path against root. Returns (path, None) or (None, error)."""
    if not raw:
        return None, "'path' is required."
    target = (root / raw).resolve()
    if not is_safe_path(root, target):
        return None, f"Access denied: '{raw}' is outside the allowed root."
    return target, None


# ─── Tools ────────────────────────────────────────────────────────────────────

def tool_list_directory(params: dict, root: Path) -> dict:
    """
    List a directory's contents.

    Returns name, type, size, modified timestamp, and relative POSIX
    path for every entry. Directories sorted before files, then
    alphabetically. Hidden files excluded by default.

    Args:
        path        Relative path to list. Default: "."
        show_hidden Include dot-files. Default: false
    """
    raw         = params.get("path", ".")
    show_hidden = bool(params.get("show_hidden", False))

    target, err = resolve_safe(raw, root)
    if err:
        return _terror(err)
    if not target.exists():
        return _terror(f"Path not found: '{raw}'")
    if not target.is_dir():
        return _terror(f"Not a directory: '{raw}'")

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for item in items:
            if not show_hidden and item.name.startswith("."):
                continue
            try:
                stat = item.stat()
                entries.append({
                    "name":     item.name,
                    "type":     "directory" if item.is_dir() else "file",
                    "size":     stat.st_size if item.is_file() else None,
                    "modified": int(stat.st_mtime),
                    "path":     item.relative_to(root).as_posix(),
                })
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return _terror(f"Permission denied: '{raw}'")

    log.debug("list_directory %s → %d entries", target, len(entries))
    return _tok({"path": target.relative_to(root).as_posix(), "total": len(entries), "entries": entries})


def tool_read_file(params: dict, root: Path) -> dict:
    """
    Read a file's contents.

    Text files returned as plain UTF-8 (or specified encoding).
    Binary files returned as { binary: true, data: "<base64>" }.
    Hard cap: 1 MB — returns an error for larger files.

    Args:
        path     Relative file path. Required.
        encoding Text encoding. Default: "utf-8"
    """
    raw      = params.get("path", "")
    encoding = params.get("encoding", "utf-8")

    target, err = resolve_safe(raw, root)
    if err:
        return _terror(err)
    if not target.exists():
        return _terror(f"File not found: '{raw}'")
    if not target.is_file():
        return _terror(f"Not a file: '{raw}'")

    try:
        size = target.stat().st_size
    except OSError as e:
        return _terror(f"Cannot stat: {e}")

    if size > MAX_FILE_BYTES:
        return _terror(f"File too large: {size:,} bytes (limit: {MAX_FILE_BYTES:,} bytes).")

    try:
        text = target.read_text(encoding=encoding)
        log.debug("read_file %s (%d bytes)", target, size)
        return {"content": [{"type": "text", "text": text}]}
    except UnicodeDecodeError:
        data = target.read_bytes()
        log.debug("read_file %s (binary, %d bytes)", target, size)
        return _tok({"binary": True, "encoding": "base64", "size": size,
                     "data": base64.b64encode(data).decode("ascii")})
    except PermissionError:
        return _terror(f"Permission denied: '{raw}'")
    except OSError as e:
        return _terror(f"Read error: {e}")


def tool_search_files(params: dict, root: Path) -> dict:
    """
    Search for files matching a glob pattern.

    Shallow by default (current directory only). Pass recursive=true
    or use '**/' prefix for deep search. Results capped at max_results.

    Args:
        pattern     Glob pattern. Required. e.g. "*.py", "*.json"
        path        Search root. Default: "."
        max_results Max results (ceiling: 200). Default: 20
        recursive   Search all subdirectories. Default: false
    """
    pattern     = params.get("pattern", "")
    raw         = params.get("path", ".")
    max_results = min(int(params.get("max_results", 20)), MAX_SEARCH_HITS)
    recursive   = bool(params.get("recursive", False))

    if not pattern:
        return _terror("'pattern' is required.")
    if recursive and not pattern.startswith("**/"):
        pattern = f"**/{pattern}"

    target, err = resolve_safe(raw, root)
    if err:
        return _terror(err)
    if not target.exists():
        return _terror(f"Path not found: '{raw}'")
    if not target.is_dir():
        return _terror(f"Not a directory: '{raw}'")

    matches, truncated = [], False
    try:
        for match in target.glob(pattern):
            if not match.is_file() or not is_safe_path(root, match):
                continue
            try:
                stat = match.stat()
                matches.append({
                    "path":     match.relative_to(root).as_posix(),
                    "name":     match.name,
                    "size":     stat.st_size,
                    "modified": int(stat.st_mtime),
                })
            except (PermissionError, OSError):
                continue
            if len(matches) >= max_results:
                truncated = True
                break
    except (PermissionError, OSError) as e:
        return _terror(f"Search error: {e}")

    log.debug("search_files '%s' in %s → %d", pattern, target, len(matches))
    return _tok({"pattern": pattern, "total": len(matches), "truncated": truncated, "matches": matches})


def tool_get_file_info(params: dict, root: Path) -> dict:
    """
    Return rich metadata for a file or directory.

    Args:
        path  Relative path to inspect. Required.
    """
    raw = params.get("path", "")
    target, err = resolve_safe(raw, root)
    if err:
        return _terror(err)
    if not target.exists():
        return _terror(f"Not found: '{raw}'")

    try:
        stat = target.stat()
    except OSError as e:
        return _terror(f"Cannot stat: {e}")

    log.debug("get_file_info %s", target)
    return _tok({
        "name":      target.name,
        "path":      target.relative_to(root).as_posix(),
        "type":      "directory" if target.is_dir() else "file",
        "size":      stat.st_size,
        "modified":  int(stat.st_mtime),
        "created":   int(stat.st_ctime),
        "extension": target.suffix.lower() if target.is_file() else None,
        "readable":  os.access(target, os.R_OK),
        "writable":  os.access(target, os.W_OK),
    })


# ─── Tool Registry ────────────────────────────────────────────────────────────

TOOLS: dict[str, dict] = {
    "list_directory": {
        "fn": tool_list_directory,
        "description": "List files and directories. Returns name, type, size, modified time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":        {"type": "string",  "description": "Relative path. Default: '.'",     "default": "."},
                "show_hidden": {"type": "boolean", "description": "Include dot-files. Default: false","default": False},
            },
        },
    },
    "read_file": {
        "fn": tool_read_file,
        "description": "Read a file's contents. Max 1 MB. Binary files returned as base64.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":     {"type": "string", "description": "Relative file path. Required."},
                "encoding": {"type": "string", "description": "Text encoding. Default: 'utf-8'", "default": "utf-8"},
            },
            "required": ["path"],
        },
    },
    "search_files": {
        "fn": tool_search_files,
        "description": "Search files by glob pattern. Use recursive=true for deep search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern":     {"type": "string",  "description": "Glob pattern e.g. '*.py'"},
                "path":        {"type": "string",  "description": "Search root. Default: '.'",       "default": "."},
                "max_results": {"type": "integer", "description": "Max results (cap: 200). Default: 20","default": 20},
                "recursive":   {"type": "boolean", "description": "Search subdirs. Default: false",  "default": False},
            },
            "required": ["pattern"],
        },
    },
    "get_file_info": {
        "fn": tool_get_file_info,
        "description": "Get metadata for a file or directory: size, type, timestamps, permissions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path. Required."},
            },
            "required": ["path"],
        },
    },
}


# ─── MCP Request Dispatcher ───────────────────────────────────────────────────

class MCPDispatcher:
    """
    Stateless JSON-RPC 2.0 dispatcher.
    Shared by both transports — stdio and HTTP/SSE.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def dispatch(self, raw: str) -> dict | None:
        """
        Parse a raw JSON-RPC string and return a response dict,
        or None for notifications (which require no response).
        """
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            return _rpc_err(None, -32700, f"Parse error: {e}")

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        log.debug("dispatch %s id=%s", method, req_id)

        # Notifications — no response
        if req_id is None and method.startswith("notifications/"):
            return None

        if method == "initialize":
            return _ok(req_id, {
                "protocolVersion": MCP_VERSION,
                "serverInfo":      {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities":    {"tools": {}},
            })

        if method == "tools/list":
            return _ok(req_id, {
                "tools": [
                    {"name": n, "description": m["description"], "inputSchema": m["inputSchema"]}
                    for n, m in TOOLS.items()
                ]
            })

        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            if name not in TOOLS:
                return _rpc_err(req_id, -32601, f"Unknown tool: '{name}'")
            log.info("tool %s %s", name, args)
            try:
                result = TOOLS[name]["fn"](args, self.root)
                return _ok(req_id, result)
            except Exception as e:
                log.exception("tool '%s' raised", name)
                return _ok(req_id, _terror(f"Internal error: {e}"))

        if method == "ping":
            return _ok(req_id, {})

        if req_id is not None:
            return _rpc_err(req_id, -32601, f"Method not found: '{method}'")

        return None


# ─── Transport 1: stdio ───────────────────────────────────────────────────────

class StdioTransport:
    """
    Line-delimited JSON-RPC over stdin/stdout.
    The MCP standard transport for Local Desktop.

    One message per line. Each response flushed immediately.
    Works on Windows (binary mode fix applied automatically).
    """

    def __init__(self, dispatcher: MCPDispatcher) -> None:
        self.dispatcher = dispatcher
        self._setup_windows_io()

    def _setup_windows_io(self) -> None:
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(),  os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

        self._stdin  = io.TextIOWrapper(sys.stdin.buffer,  encoding="utf-8", newline="\n")
        self._stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n",
                                        write_through=True)

    def run(self) -> None:
        sys.stderr.write(f"[{SERVER_NAME}] stdio transport ready. Root: {self.dispatcher.root}\n")
        sys.stderr.flush()

        for raw in self._stdin:
            raw = raw.strip()
            if not raw:
                continue
            log.debug("stdin → %s", raw[:160])
            response = self.dispatcher.dispatch(raw)
            if response is not None:
                self._send(response)

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, separators=(",", ":"))
        self._stdout.write(line + "\n")
        self._stdout.flush()
        log.debug("stdout ← %s", line[:160])


# ─── Transport 2: HTTP + SSE ──────────────────────────────────────────────────

class SSEClient:
    """One connected SSE client with its own message queue."""

    def __init__(self, client_id: str) -> None:
        self.id    = client_id
        self.queue: queue.Queue[str | None] = queue.Queue()

    def push(self, data: str) -> None:
        self.queue.put(data)

    def close(self) -> None:
        self.queue.put(None)  # sentinel


class SSEClientRegistry:
    """Thread-safe registry of all connected SSE clients."""

    def __init__(self) -> None:
        self._clients: dict[str, SSEClient] = {}
        self._lock    = threading.Lock()

    def add(self, client: SSEClient) -> None:
        with self._lock:
            self._clients[client.id] = client
        log.info("SSE client connected: %s (total: %d)", client.id, len(self._clients))

    def remove(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)
        log.info("SSE client disconnected: %s (total: %d)", client_id, len(self._clients))

    def get(self, client_id: str) -> SSEClient | None:
        with self._lock:
            return self._clients.get(client_id)

    def count(self) -> int:
        with self._lock:
            return len(self._clients)


class MCPHTTPHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the MCP HTTP/SSE transport.

    Endpoints:
        GET  /sse          Open an SSE stream. Returns a client_id in the first event.
        POST /message      Send a JSON-RPC message for a specific client.
        GET  /health       Health check — returns server status as JSON.
        GET  /             Server info page.
    """

    dispatcher : MCPDispatcher
    registry   : SSEClientRegistry

    def log_message(self, fmt: str, *args) -> None:
        log.debug("HTTP %s", fmt % args)

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/sse":
            self._handle_sse()
        elif parsed.path == "/health":
            self._handle_health()
        elif parsed.path == "/":
            self._handle_index()
        else:
            self._send_error(404, "Not found")

    def _handle_sse(self) -> None:
        """
        Open a persistent SSE stream for one client.

        Flow:
          1. Register a new SSEClient with a unique ID
          2. Send the client_id as the first SSE event
          3. Block on the client's queue, streaming events as they arrive
          4. On disconnect, unregister and exit
        """
        client = SSEClient(str(uuid.uuid4()))
        self.server.registry.add(client)

        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def write_event(event: str, data: str) -> bool:
            try:
                payload = f"event: {event}\ndata: {data}\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        # First event: announce the client ID
        if not write_event("connected", json.dumps({"client_id": client.id,
                                                     "server": SERVER_NAME,
                                                     "version": SERVER_VERSION})):
            self.server.registry.remove(client.id)
            return

        # Stream responses until disconnect
        while True:
            try:
                msg = client.queue.get(timeout=15)
            except queue.Empty:
                # Heartbeat to detect stale connections
                if not write_event("ping", "{}"):
                    break
                continue

            if msg is None:  # close sentinel
                break

            if not write_event("message", msg):
                break

        self.server.registry.remove(client.id)

    def _handle_health(self) -> None:
        body = json.dumps({
            "status":   "ok",
            "server":   SERVER_NAME,
            "version":  SERVER_VERSION,
            "root":     str(self.server.dispatcher.root),
            "clients":  self.server.registry.count(),
            "platform": platform.system(),
        }).encode("utf-8")
        self._send_json(200, body)

    def _handle_index(self) -> None:
        body = (
            f"{SERVER_NAME} v{SERVER_VERSION}\n"
            f"Root   : {self.server.dispatcher.root}\n"
            f"Clients: {self.server.registry.count()}\n\n"
            f"Endpoints:\n"
            f"  GET  /sse      Open SSE stream\n"
            f"  POST /message  Send JSON-RPC message\n"
            f"  GET  /health   Health check\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/message":
            self._handle_message()
        else:
            self._send_error(404, "Not found")

    def _handle_message(self) -> None:
        """
        Receive a JSON-RPC message, dispatch it, and push the
        response onto the target client's SSE queue.

        The client_id must be provided as a query parameter:
            POST /message?client_id=<uuid>
        """
        parsed    = urlparse(self.path)
        qs        = parse_qs(parsed.query)
        client_id = qs.get("client_id", [None])[0]

        if not client_id:
            self._send_error(400, "Missing 'client_id' query parameter.")
            return

        client = self.server.registry.get(client_id)
        if not client:
            self._send_error(404, f"Client '{client_id}' not found or disconnected.")
            return

        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_error(400, "Empty request body.")
            return

        raw = self.rfile.read(length).decode("utf-8")
        log.debug("POST /message client=%s body=%s", client_id, raw[:160])

        response = self.dispatcher.dispatch(raw)

        if response is not None:
            client.push(json.dumps(response, separators=(",", ":")))

        # ACK: 202 Accepted
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    # ── OPTIONS (CORS preflight) ──────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _send_json(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self._send_json(code, body)


class HTTPSSETransport:
    """
    Concurrent HTTP/SSE transport for the MCP server.

    Uses Python's stdlib HTTPServer with a thread-per-request model.
    Each SSE connection holds its own thread; POST /message dispatches
    JSON-RPC and pushes responses back over the client's SSE stream.

    Suitable for multiple concurrent AI clients on a local network.
    Zero external dependencies — stdlib only.
    """

    def __init__(self, dispatcher: MCPDispatcher, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT) -> None:
        self.dispatcher = dispatcher
        self.host       = host
        self.port       = port

    def run(self) -> None:
        registry = SSEClientRegistry()

        # Inject dispatcher and registry into the handler class
        handler             = MCPHTTPHandler
        handler.dispatcher  = self.dispatcher
        handler.registry    = registry

        server = HTTPServer((self.host, self.port), handler)
        server.registry   = registry
        server.dispatcher = self.dispatcher

        # Thread-per-request for concurrent SSE connections
        server.socket.setsockopt(
            __import__("socket").SOL_SOCKET,
            __import__("socket").SO_REUSEADDR,
            1,
        )

        sys.stderr.write(
            f"[{SERVER_NAME}] HTTP/SSE transport ready.\n"
            f"  Root   : {self.dispatcher.root}\n"
            f"  URL    : http://{self.host}:{self.port}\n"
            f"  SSE    : http://{self.host}:{self.port}/sse\n"
            f"  Health : http://{self.host}:{self.port}/health\n"
        )
        sys.stderr.flush()

        def serve_thread():
            while True:
                server.handle_request()

        # Spawn worker threads for concurrent request handling
        workers = []
        for _ in range(16):
            t = threading.Thread(target=serve_thread, daemon=True)
            t.start()
            workers.append(t)

        log.info("HTTP/SSE server on %s:%d (16 workers)", self.host, self.port)

        try:
            for w in workers:
                w.join()
        except KeyboardInterrupt:
            sys.stderr.write(f"\n[{SERVER_NAME}] Shutting down.\n")
            server.server_close()


# ─── JSON-RPC Helpers ─────────────────────────────────────────────────────────

def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}

def _rpc_err(req_id: Any, code: int, msg: str) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": {"code": code, "message": msg}}

def _tok(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}

def _terror(msg: str) -> dict:
    return {"content": [{"type": "text", "text": f"Error: {msg}"}], "isError": True}


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description="Bare-metal MCP server for local file system access.",
    )
    parser.add_argument("--http",   action="store_true", help="Use HTTP/SSE transport (default: stdio)")
    parser.add_argument("--host",   default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port",   default=DEFAULT_PORT, type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--root",   default=None, help="Override MCP_ROOT env var")
    parser.add_argument("--debug",  action="store_true", help="Verbose debug logging")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    root_raw = args.root or os.environ.get("MCP_ROOT", ".")
    root     = Path(root_raw).resolve()

    if not root.exists():
        sys.exit(f"[{SERVER_NAME}] ERROR: root '{root_raw}' does not exist.")
    if not root.is_dir():
        sys.exit(f"[{SERVER_NAME}] ERROR: root '{root_raw}' is not a directory.")

    log.info("Starting %s v%s | root=%s | transport=%s | python=%s | os=%s",
             SERVER_NAME, SERVER_VERSION, root,
             "http" if args.http else "stdio",
             sys.version.split()[0], platform.system())

    dispatcher = MCPDispatcher(root)

    if args.http:
        HTTPSSETransport(dispatcher, host=args.host, port=args.port).run()
    else:
        StdioTransport(dispatcher).run()


if __name__ == "__main__":
    main()
