# local-mcp-server

A bare-metal MCP server for secure local file system access — stdio for single clients, HTTP/SSE for concurrent connections. One flag switches between them. Zero dependencies.

```
AI Client ── stdin/stdout ──▶ server.py ──▶ Your Files
            or HTTP/SSE
```

> No `pip install`. No API key. No cloud. Just Python 3.8+ and one file.

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-50%20passing-brightgreen)]()
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)]()

Read the full write-up on Towards Data Science → [My AI Was Blind to My Local Machine — I Built the Missing MCP Layer](https://towardsdatascience.com/author/emmimalp-alexander/)

---

## The Problem

Every MCP tutorial that connects an AI to local files either:
- Requires FastAPI, uvicorn, LangChain, or the official MCP SDK
- Only implements one of the two transports the spec defines
- Hangs on Windows because nobody tested it there
- Breaks the moment you point it at a large directory

The MCP spec defines exactly two transports: stdio and HTTP/SSE. This server implements both, from scratch, using only Python's standard library.

---

## What It Does

```
AI Client ──▶ Transport Layer ──▶ Dispatcher ──▶ Tools Layer ──▶ Security Layer ──▶ File System
              (stdio / HTTP/SSE)   (stateless)    (4 tools)       (MCP_ROOT sandbox)
```

| Component | Job |
|---|---|
| StdioTransport | Line-delimited JSON-RPC over stdin/stdout. Single client. Claude Desktop / local AI tools. |
| HTTPSSETransport | HTTP POST for requests, SSE stream for responses. 16 worker threads. Concurrent clients. |
| MCPDispatcher | Stateless JSON-RPC router. No I/O. Shared by both transports. |
| Tools | `list_directory`, `read_file`, `search_files`, `get_file_info` |
| Security | Every path resolved and validated against `MCP_ROOT` before any I/O. |

---

## Quickstart

### 1. Clone

```bash
git clone https://github.com/Emmimal/local-mcp-server.git
cd local-mcp-server
```

No `pip install`. No `requirements.txt`. Done.

### 2. Run the demo

```bash
# stdio transport
python demo.py C:/your/project/folder

# HTTP/SSE transport
python demo.py C:/your/project/folder --http
```

Expected output:

```
============================================================
  local-mcp-server demo  [stdio transport]
  Root: C:/your/project/folder
============================================================

[1] Initialize
  Server  : local-mcp-server v1.0.0
  Protocol: 2024-11-05

[2] Available tools
  [list_directory     ] List files and directories...
  [read_file          ] Read a file's contents. Max 1 MB...
  [search_files       ] Search files by glob pattern...
  [get_file_info      ] Get metadata for a file or directory...

[3] list_directory     8 entries
[4] get_file_info      readable: True  writable: True
[5] read_file          first small file read successfully
[6] search_files       Found 5 .py files

============================================================
  All checks passed. Ready to connect Local Desktop.
============================================================
```

### 3. Run the concurrent demo

```bash
# Terminal 1 — start the HTTP/SSE server
python server.py --http --port 8765

# Terminal 2 — launch 5 simultaneous clients
python concurrent_demo.py
```

```
============================================================
  Concurrent Client Demo — 5 clients, 5 simultaneous calls
============================================================

  Client     Tool                 Result         Time
  ---------- -------------------- ---------- --------
  1          list_directory       OK           ~0.034s
  2          get_file_info        OK           ~0.021s
  3          list_directory       OK           ~0.038s
  4          search_files         OK           ~0.023s
  5          search_files         OK           ~0.021s

  Total wall time: ~0.04s for 5 concurrent clients
  Result: ALL PASSED
============================================================
```

### 4. Connect to a local AI client

**macOS:** `~/Library/Application Support/Claude/local_desktop_config.json`

**Windows:** `%APPDATA%\Claude\local_desktop_config.json`

```json
{
  "mcpServers": {
    "local-desktop": {
      "command": "python",
      "args": ["/absolute/path/to/local-mcp-server/server.py"],
      "env": {
        "MCP_ROOT": "/absolute/path/to/your/workspace"
      }
    }
  }
}
```

Restart your AI client. You'll see `local-desktop` in the tools menu.

---

## Transports

### stdio — single client

```bash
python server.py
# or with explicit root
python server.py --root C:/your/project
```

One client. Line-delimited JSON-RPC over stdin/stdout. The MCP standard transport for desktop AI tools.

Windows note: binary mode is applied automatically to stdin/stdout. Without it, Python's default text mode translates `\n` to `\r\n`, which corrupts JSON. This is handled in `_setup_windows_io()`.

### HTTP/SSE — concurrent clients

```bash
python server.py --http --port 8765
```

```
GET  /sse          Open a persistent SSE stream → receive client_id
POST /message      Send JSON-RPC (include ?client_id=<uuid>)
GET  /health       Server status and connected client count
GET  /             Info page
```

Each client gets its own SSE stream and message queue. The POST handler returns 202 immediately — it does not wait for SSE delivery. That decoupling is what makes concurrency work.

---

## The Four Tools

### list_directory

```json
{
  "name": "list_directory",
  "arguments": { "path": "src", "show_hidden": false }
}
```

Returns name, type, size, modified timestamp, and relative path for every entry. Directories sorted before files. Hidden entries excluded by default.

### read_file

```json
{
  "name": "read_file",
  "arguments": { "path": "src/main.py" }
}
```

Returns file contents as UTF-8 text. Binary files returned as `{ "binary": true, "data": "<base64>" }`. Hard cap: 1 MB. Change `MAX_FILE_BYTES` in `server.py` if needed.

### search_files

```json
{
  "name": "search_files",
  "arguments": { "pattern": "*.py", "recursive": false, "max_results": 20 }
}
```

**Shallow by default.** Pass `recursive: true` for deep search. The first version used `rglob()` by default — pointing it at `C:\Users\Admin` ran for ten minutes. `recursive=False` is now the default and cannot be changed by the server itself.

The `truncated` flag in the response tells the client when results were cut off at `max_results`.

### get_file_info

```json
{
  "name": "get_file_info",
  "arguments": { "path": "README.md" }
}
```

Returns name, type, size, extension, modified, created, readable, writable. Uses `os.access()` for real permission checks, not just existence.

---

## Security Model

`MCP_ROOT` is the **only** directory the server can access.

```python
def is_safe_path(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
```

Every path is:
1. Joined with `MCP_ROOT`
2. Fully resolved — symlinks expanded, `..` collapsed
3. Checked with `relative_to()` — raises `ValueError` if outside root

| Attack | Result |
|---|---|
| `../../etc/passwd` | Access denied |
| Symlink pointing outside root | Access denied |
| Windows UNC path `\\server\share` | Access denied |
| `src/main.py` inside root | Allowed |

**Set `MCP_ROOT` to the smallest directory your workflow actually needs.**

---

## CLI Options

```
python server.py [options]

  --http          Use HTTP/SSE transport (default: stdio)
  --host HOST     HTTP bind host (default: 127.0.0.1)
  --port PORT     HTTP port (default: 8765)
  --root PATH     Override MCP_ROOT environment variable
  --debug         Enable verbose debug logging to mcp_server.log
```

---

## Run the Tests

```bash
pip install pytest
python -m pytest tests/test_server.py -v

# Skip HTTP integration tests
python -m pytest tests/test_server.py -v -m "not integration"
```

50 tests across seven classes:

| Class | What it covers |
|---|---|
| TestSecurity | Traversal attacks, symlink escapes, empty paths |
| TestListDirectory | Hidden files, sort order, locked entries, errors |
| TestReadFile | Text, binary/base64, 1 MB cap, permission errors |
| TestSearchFiles | Shallow vs recursive, max_results, truncation flag |
| TestGetFileInfo | File vs directory, permissions, timestamps |
| TestDispatcher | All methods, notifications, parse errors, unknown methods |
| TestHTTPTransport | Health endpoint, SSE connection, 400/404 error codes |

---

## Project Structure

```
local-mcp-server/
├── server.py                          # Everything. Both transports, all tools, security.
├── demo.py                            # Verify stdio or HTTP/SSE before connecting
├── concurrent_demo.py                 # Prove 5 clients run simultaneously
├── http_client.py                     # Minimal stdlib HTTP/SSE client example
├── tests/
│   └── test_server.py                 # 50 tests
└── local_desktop_config.example.json  # Config template
```

---

## Performance (Windows 11, Python 3.12.6, CPU only)

| Operation | Latency |
|---|---|
| list_directory (8 entries) | < 5ms |
| read_file (< 1 MB text) | < 5ms |
| search_files (shallow, 5 results) | < 30ms |
| get_file_info | < 5ms |
| 5 concurrent HTTP/SSE clients | ~40ms total wall time |

Search is the only tool with meaningful latency variation. Shallow search on a project folder is fast. `recursive=True` on a large directory tree will be slow — that is expected and intentional.

---

## What Broke During Development

Three things only appeared when the server ran against a real machine:

**The ten-minute hang.** `rglob()` on `C:\Users\Admin` scanned tens of thousands of files. Fixed by making search shallow by default.

**The Windows `\r\n` problem.** Python's default text mode on Windows translates `\n` to `\r\n`, corrupting JSON. Fixed with `msvcrt.setmode(O_BINARY)` on stdin and stdout.

**The binary file crash.** `read_text()` on a `.pyc` file raised `UnicodeDecodeError`. Fixed with a base64 fallback.

**The 200 MB database freeze.** No file size cap meant one accidental SQLite read froze the process for thirty seconds. Fixed with `MAX_FILE_BYTES = 1_048_576`.

---

## Known Limitations

- **16 worker threads** is enough for local use. Not designed for a shared server with hundreds of simultaneous connections — use `asyncio` for that.
- **1 MB file cap** is a constant. Change `MAX_FILE_BYTES` in `server.py` if your workflow needs larger files.
- **Token counting is not included.** Raw file contents are returned. Token budget management belongs in the layer between this server and the model.
- **Memory is not persistent.** The server has no session state. Each connection starts fresh.


---

## Related

Same series — production layers for AI systems:

- [My AI Was Blind to My Local Machine — I Built the Missing MCP Layer](https://towardsdatascience.com) — this article. bare-metal MCP server, stdio + HTTP/SSE, zero dependencies.
- [RAG Isn't Enough — I Built the Missing Context Layer That Makes LLM Systems Work](https://towardsdatascience.com) — context management layer: retrieval, re-ranking, memory decay, token budgets. [GitHub →](https://github.com/Emmimal/context-engine)
- [RAG Is Blind to Time — I Built a Temporal Layer to Fix It in Production](https://towardsdatascience.com/rag-is-blind-to-time-i-built-a-temporal-layer-to-fix-it-in-production/) — temporal awareness layer that treats document freshness as a first-class retrieval signal.
- [LLM Evals Are Based on Vibes — I Built the Missing Layer That Decides What Ships](https://towardsdatascience.com/llm-evals-are-based-on-vibes-i-built-the-missing-layer-that-decides-what-ships/) — evaluation layer that replaces gut-feel shipping decisions with measurable output quality gates.
- [Prompt Engineering Isn't Enough — I Built a Control Layer That Works in Production](https://towardsdatascience.com/prompt-engineering-isnt-enough-i-built-a-control-layer-that-works-in-production/) — prompt control layer for production LLM systems.
- [RAG Is Burning Money — I Built a Cost Control Layer to Fix It](https://towardsdatascience.com/rag-is-burning-money-i-built-a-cost-control-layer-to-fix-it/) — cost control layer that cuts RAG inference spend without degrading output quality.

---

Built by [Emmimal P Alexander](https://emitechlogic.com/) — writing about production AI systems and the layers that make them work.

---

## License

MIT. Do whatever you want with it.

---

## Contributing

PRs welcome. The hard rule: **no new dependencies.** If your feature needs a package, it belongs in a fork.
