# muse MCP bridge

An MCP server (Streamable HTTP) that exposes a personal AI assistant —
"muse" — to an external agent platform, so a request like *"check with
muse"* from that platform reaches the assistant's world: household
to-dos and chores, Home Assistant state, media the client device
captures, and the assistant itself for anything that needs judgment.

Built in September 2026 to connect Rabbit OS3 (the Rabbit R1's agent
platform) to my home server, and running in production since. The server
is a single Python file (`server.py`) on the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(FastMCP). The SDK is the framework; everything in `server.py` — the
tools, the routing rule, the security model, the queue design — was
written for this.

## Architecture

```
agent platform (cloud) ──https──▶ public tunnel ──▶ 127.0.0.1:8903
                                                       muse-mcp server
                                                       ├─ direct tools: run code, answer now
                                                       └─ ask_muse: queue file ──▶ assistant
                                                                                  answers ──▶ done file
                                  platform polls muse_status ◀────────────────────┘
```

## Tools

**Direct — execute code, answer immediately, read-only except media store:**

| Tool | What it does |
|---|---|
| `household_todos` | Open household to-dos (via the household backend) |
| `household_chores` | Chores + current completion state |
| `ha_state(entity_id?)` | Home Assistant entity state via a small REST helper; no arg lists entity ids |
| `muse_guide(topic?)` | The operator guide; also the `muse://guide` resource |
| `muse_store_media(filename, mime_type, data_base64)` | **Write-scoped:** stores a media file captured by the client device on the home server (25 MB decoded cap, dated folders, 0600 perms, `.meta.json` sidecar) |
| `muse_list_media(date?, limit?)` | Lists stored media for a date |

**Judgment — the ping path:**

| Tool | What it does |
|---|---|
| `ask_muse(message)` | Queues a task for the assistant and returns a `task_id` |
| `muse_status(task_id)` | `{status: "pending"}` while queued; `{status: "done", result}` when answered |

**Routing rule (structural, enforced in code):** direct tools never
guess — on ambiguity or failure they return an error telling the client
to use `ask_muse`. Which path a request takes is the client's call,
guided by the tool descriptions; the server itself makes no judgment
calls.

## Two design decisions worth noting

**Stateless HTTP mode.** One client caches its MCP session ID and never
re-initializes after a `Session not found` (-32600) error, so any server
restart permanently broke its connection. The bridge runs the SDK's
stateless HTTP mode — no sessions issued or tracked, restarts are
harmless. All of its tools are stateless anyway. (This failure was
reproduced with a deliberately stale session ID before the fix, and
verified fixed the same way after.)

**The queue is the nudge.** There is no push channel into the
assistant's runtime, so `ask_muse` writes a file to a spool directory
and the assistant picks tasks up on its existing wake cycle, writing
answers back as files that `muse_status` serves. Clients should poll
rather than assume a fast answer. The pattern mirrors a Gmail Pub/Sub
push receiver built on the same server.

## Security

- **The path token is the only barrier.** The client platform's URL-add
  flow takes a bare URL with no auth-header option, so the endpoint is
  `/mcp-<unguessable-token>/mcp` and the full URL is a secret. This is
  acceptable here because every tool is read-only except `ask_muse`
  (queues work for the assistant's judgment) and `muse_store_media`
  (writes only into the media directory). Rotate by changing the token,
  restarting, and updating the client's entry.
- **No secrets in code.** The token comes from the environment. The HA
  helper reads its own mode-600 token file; the server never logs or
  returns it.
- **DNS-rebinding protection stays ON**, with an explicit allowlist for
  localhost plus the public hostname (`MUSE_MCP_PUBLIC_HOST`).
- The server binds `127.0.0.1` only; exposure is through a tunnel or
  reverse proxy that terminates TLS.

## Running it

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in, chmod 600
set -a; . ./.env; set +a
./venv/bin/python server.py
python3 verify_bridge.py http://127.0.0.1:8903
```

`verify_bridge.py` runs an end-to-end smoke test against a live server:
initialize, the 8-tool list, an `ask_muse`/`muse_status` round trip,
and a media store-and-list cycle.

The household tools need a backend module exposing a
`DashboardService` class (see `.env.example`). `example_backend.py`
implements the interface with sample data so the server runs standalone;
the production backend is a private household dashboard package and is
not part of this repository.

A systemd unit example is in `muse-mcp.service.example`.

## What changed for publication

This is the production server with configuration externalized:
filesystem paths, the public hostname, the acting user, and the backend
module were literals in the running copy and are environment variables
here; the operator guide text was generalized; the example backend
replaces the private household package. Tool behavior and the routing
and security design are unchanged.

## License

MIT — see `LICENSE`.
