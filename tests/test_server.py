"""
Tests for local-mcp-server.

Covers: security, all 4 tools, stdio protocol, HTTP/SSE transport.

Run with:
    python -m pytest tests/test_server.py -v
"""

import base64
import json
import sys
import time
import threading
import urllib.request
import subprocess
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import (
    tool_list_directory,
    tool_read_file,
    tool_search_files,
    tool_get_file_info,
    is_safe_path,
    resolve_safe,
    MCPDispatcher,
    TOOLS,
    SERVER_NAME,
    SERVER_VERSION,
    MCP_VERSION,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n")
    (tmp_path / "src" / "utils.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sample.json").write_text('{"key": "value"}\n')
    (tmp_path / ".hidden").write_text("secret\n")
    (tmp_path / "README.md").write_text("# My Project\n\nA cool project.\n")
    (tmp_path / "notes.txt").write_text("Some notes here.\n")
    return tmp_path


@pytest.fixture
def dispatcher(workspace):
    return MCPDispatcher(workspace)


# ─── Security ─────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_safe_path_inside_root(self, workspace):
        assert is_safe_path(workspace, workspace / "src" / "main.py")

    def test_safe_path_exact_root(self, workspace):
        assert is_safe_path(workspace, workspace)

    def test_path_traversal_blocked(self, workspace):
        assert not is_safe_path(workspace, workspace / ".." / ".." / "etc" / "passwd")

    def test_resolve_safe_empty_path(self, workspace):
        path, err = resolve_safe("", workspace)
        assert path is None
        assert err is not None

    def test_resolve_safe_traversal(self, workspace):
        path, err = resolve_safe("../../etc/passwd", workspace)
        assert path is None
        assert "Access denied" in err

    def test_resolve_safe_valid(self, workspace):
        path, err = resolve_safe("README.md", workspace)
        assert err is None
        assert path is not None

    def test_list_dir_traversal_blocked(self, workspace):
        result = tool_list_directory({"path": "../../etc"}, workspace)
        assert result.get("isError")

    def test_read_file_traversal_blocked(self, workspace):
        result = tool_read_file({"path": "../../etc/passwd"}, workspace)
        assert result.get("isError")

    def test_search_traversal_blocked(self, workspace):
        result = tool_search_files({"pattern": "*.py", "path": "../../"}, workspace)
        assert result.get("isError")


# ─── tool_list_directory ──────────────────────────────────────────────────────

class TestListDirectory:
    def test_lists_root(self, workspace):
        result = tool_list_directory({"path": "."}, workspace)
        data   = json.loads(result["content"][0]["text"])
        names  = [e["name"] for e in data["entries"]]
        assert "src" in names
        assert "README.md" in names
        assert "notes.txt" in names

    def test_hidden_excluded(self, workspace):
        result = tool_list_directory({"path": "."}, workspace)
        names  = [e["name"] for e in json.loads(result["content"][0]["text"])["entries"]]
        assert ".hidden" not in names

    def test_hidden_included(self, workspace):
        result = tool_list_directory({"path": ".", "show_hidden": True}, workspace)
        names  = [e["name"] for e in json.loads(result["content"][0]["text"])["entries"]]
        assert ".hidden" in names

    def test_entry_fields(self, workspace):
        result = tool_list_directory({"path": "."}, workspace)
        entry  = next(e for e in json.loads(result["content"][0]["text"])["entries"]
                      if e["name"] == "README.md")
        assert entry["type"]     == "file"
        assert entry["size"]     > 0
        assert "modified"        in entry
        assert entry["path"]     == "README.md"

    def test_subdirectory(self, workspace):
        result = tool_list_directory({"path": "src"}, workspace)
        names  = [e["name"] for e in json.loads(result["content"][0]["text"])["entries"]]
        assert "main.py" in names

    def test_nonexistent_error(self, workspace):
        assert tool_list_directory({"path": "ghost"}, workspace).get("isError")

    def test_file_as_dir_error(self, workspace):
        assert tool_list_directory({"path": "README.md"}, workspace).get("isError")

    def test_dirs_sorted_before_files(self, workspace):
        result  = tool_list_directory({"path": "."}, workspace)
        entries = json.loads(result["content"][0]["text"])["entries"]
        types   = [e["type"] for e in entries]
        # All directories should appear before files
        last_dir = max((i for i, t in enumerate(types) if t == "directory"), default=-1)
        first_file = min((i for i, t in enumerate(types) if t == "file"), default=999)
        assert last_dir < first_file


# ─── tool_read_file ───────────────────────────────────────────────────────────

class TestReadFile:
    def test_reads_text(self, workspace):
        result = tool_read_file({"path": "README.md"}, workspace)
        assert "My Project" in result["content"][0]["text"]

    def test_reads_nested(self, workspace):
        result = tool_read_file({"path": "src/main.py"}, workspace)
        assert "print" in result["content"][0]["text"]

    def test_empty_path_error(self, workspace):
        assert tool_read_file({}, workspace).get("isError")

    def test_missing_file_error(self, workspace):
        assert tool_read_file({"path": "ghost.txt"}, workspace).get("isError")

    def test_directory_error(self, workspace):
        assert tool_read_file({"path": "src"}, workspace).get("isError")

    def test_large_file_error(self, workspace):
        (workspace / "big.bin").write_bytes(b"x" * (1_048_577))
        result = tool_read_file({"path": "big.bin"}, workspace)
        assert result.get("isError")
        assert "too large" in result["content"][0]["text"]

    def test_binary_returns_base64(self, workspace):
        (workspace / "data.bin").write_bytes(bytes(range(256)))
        result = tool_read_file({"path": "data.bin"}, workspace)
        data   = json.loads(result["content"][0]["text"])
        assert data["binary"]   is True
        assert data["encoding"] == "base64"
        decoded = base64.b64decode(data["data"])
        assert decoded == bytes(range(256))


# ─── tool_search_files ────────────────────────────────────────────────────────

class TestSearchFiles:
    def test_shallow_finds_root_files(self, workspace):
        result = tool_search_files({"pattern": "*.md"}, workspace)
        data   = json.loads(result["content"][0]["text"])
        names  = [Path(m["path"]).name for m in data["matches"]]
        assert "README.md" in names

    def test_shallow_misses_nested(self, workspace):
        result = tool_search_files({"pattern": "*.py"}, workspace)
        data   = json.loads(result["content"][0]["text"])
        assert data["total"] == 0

    def test_recursive_finds_nested(self, workspace):
        result = tool_search_files({"pattern": "*.py", "recursive": True}, workspace)
        data   = json.loads(result["content"][0]["text"])
        names  = [Path(m["path"]).name for m in data["matches"]]
        assert "main.py"  in names
        assert "utils.py" in names

    def test_recursive_json(self, workspace):
        result = tool_search_files({"pattern": "*.json", "recursive": True}, workspace)
        data   = json.loads(result["content"][0]["text"])
        assert data["total"] == 1

    def test_max_results(self, workspace):
        for i in range(10):
            (workspace / f"f{i}.txt").write_text("x")
        result = tool_search_files({"pattern": "*.txt", "max_results": 3}, workspace)
        data   = json.loads(result["content"][0]["text"])
        assert len(data["matches"]) <= 3
        assert data["truncated"] is True

    def test_max_results_ceiling(self, workspace):
        result = tool_search_files({"pattern": "*", "max_results": 9999}, workspace)
        assert len(json.loads(result["content"][0]["text"])["matches"]) <= 200

    def test_empty_pattern_error(self, workspace):
        assert tool_search_files({"pattern": ""}, workspace).get("isError")

    def test_missing_pattern_error(self, workspace):
        assert tool_search_files({}, workspace).get("isError")


# ─── tool_get_file_info ───────────────────────────────────────────────────────

class TestGetFileInfo:
    def test_file_info(self, workspace):
        result = tool_get_file_info({"path": "README.md"}, workspace)
        data   = json.loads(result["content"][0]["text"])
        assert data["name"]      == "README.md"
        assert data["type"]      == "file"
        assert data["extension"] == ".md"
        assert data["size"]      > 0
        assert data["readable"]  is True
        assert "modified"        in data
        assert "created"         in data

    def test_directory_info(self, workspace):
        result = tool_get_file_info({"path": "src"}, workspace)
        data   = json.loads(result["content"][0]["text"])
        assert data["type"]      == "directory"
        assert data["extension"] is None

    def test_empty_path_error(self, workspace):
        assert tool_get_file_info({}, workspace).get("isError")

    def test_nonexistent_error(self, workspace):
        assert tool_get_file_info({"path": "no_such"}, workspace).get("isError")


# ─── MCPDispatcher (protocol) ─────────────────────────────────────────────────

class TestDispatcher:
    def test_initialize(self, dispatcher):
        resp = dispatcher.dispatch('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
        assert resp["result"]["serverInfo"]["name"]    == SERVER_NAME
        assert resp["result"]["serverInfo"]["version"] == SERVER_VERSION
        assert resp["result"]["protocolVersion"]       == MCP_VERSION

    def test_tools_list(self, dispatcher):
        resp  = dispatcher.dispatch('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
        names = [t["name"] for t in resp["result"]["tools"]]
        assert set(names) == {"list_directory", "read_file", "search_files", "get_file_info"}

    def test_tools_list_has_schemas(self, dispatcher):
        resp = dispatcher.dispatch('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
        for tool in resp["result"]["tools"]:
            assert "inputSchema"  in tool
            assert "description"  in tool

    def test_tools_call_list_directory(self, dispatcher):
        req  = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "list_directory", "arguments": {"path": "."}}})
        resp = dispatcher.dispatch(req)
        assert "result"  in resp
        assert not resp["result"].get("isError")

    def test_tools_call_unknown(self, dispatcher):
        req  = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "does_not_exist", "arguments": {}}})
        resp = dispatcher.dispatch(req)
        assert "error" in resp

    def test_ping(self, dispatcher):
        resp = dispatcher.dispatch('{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}')
        assert resp["result"] == {}

    def test_unknown_method(self, dispatcher):
        resp = dispatcher.dispatch('{"jsonrpc":"2.0","id":1,"method":"unknown","params":{}}')
        assert "error" in resp

    def test_notification_returns_none(self, dispatcher):
        resp = dispatcher.dispatch('{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}')
        assert resp is None

    def test_invalid_json(self, dispatcher):
        resp = dispatcher.dispatch("not json at all")
        assert "error" in resp
        assert resp["error"]["code"] == -32700

    def test_all_tools_registered(self):
        assert len(TOOLS) == 4


# ─── HTTP/SSE Transport ───────────────────────────────────────────────────────

class TestHTTPTransport:
    """
    Integration tests for the HTTP/SSE transport.
    Spins up a real server subprocess on port 18765.
    """

    PORT       = 18765
    BASE_URL   = f"http://127.0.0.1:{PORT}"
    _proc      = None
    _root      = None

    @pytest.fixture(autouse=True)
    def server(self, workspace, tmp_path):
        self.__class__._root = workspace
        env  = {**os.environ, "MCP_ROOT": str(workspace)}
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent.parent / "server.py"),
             "--http", "--port", str(self.PORT)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        self.__class__._proc = proc

        # Wait for server ready
        for _ in range(20):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(f"{self.BASE_URL}/health", timeout=1)
                break
            except Exception:
                continue

        yield

        proc.kill()
        proc.wait(timeout=3)

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.BASE_URL}{path}", timeout=5) as r:
            return json.loads(r.read().decode())

    def test_health_endpoint(self):
        data = self._get("/health")
        assert data["status"]  == "ok"
        assert data["server"]  == SERVER_NAME
        assert data["version"] == SERVER_VERSION

    @pytest.mark.integration
    def test_sse_connect_and_message(self):
        import queue as Q

        q          = Q.Queue()
        connected  = [False]
        client_id  = [None]

        def stream_sse():
            try:
                req = urllib.request.Request(f"{self.BASE_URL}/sse")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    buf = ""
                    for _ in range(200):
                        ch = resp.read(1).decode("utf-8", errors="replace")
                        if not ch:
                            break
                        buf += ch
                        if buf.endswith("\n\n"):
                            for line in buf.strip().splitlines():
                                if line.startswith("data:"):
                                    q.put(line[5:].strip())
                            buf = ""
                            if q.qsize() >= 2:
                                break
            except Exception as e:
                q.put(json.dumps({"_error": str(e)}))

        t = threading.Thread(target=stream_sse, daemon=True)
        t.start()

        first = json.loads(q.get(timeout=10))
        assert "client_id" in first
        cid = first["client_id"]

        time.sleep(0.2)  # ensure SSE stream is fully established

        # Send initialize
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
        req  = urllib.request.Request(
            f"{self.BASE_URL}/message?client_id={cid}",
            data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 202

        response = json.loads(q.get(timeout=10))
        assert response["result"]["serverInfo"]["name"] == SERVER_NAME

    def test_post_without_client_id_returns_400(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode()
        req  = urllib.request.Request(
            f"{self.BASE_URL}/message",
            data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_post_unknown_client_returns_404(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode()
        req  = urllib.request.Request(
            f"{self.BASE_URL}/message?client_id=does-not-exist",
            data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 404
