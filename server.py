#!/usr/bin/env python3
"""muse MCP bridge — exposes a personal AI assistant ("muse") to an
external agent platform (built for Rabbit OS3) as an MCP server
(Streamable HTTP).

Security model: the endpoint path contains an unguessable token
(/mcp-<token>/mcp). OS3's "add by URL" flow takes a bare URL with no
auth-header option, so the path token is the only barrier. Tools are
read-only except ask_muse (queues a task for the assistant's judgment)
and muse_store_media (writes only into the configured media directory).
No secrets live in this file; the token comes from the environment and
the Home Assistant helper reads its own token file.

Configuration is via environment variables; see README.md and .env.example.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from pathlib import Path

_BACKEND_PATH = os.environ.get("MUSE_MCP_BACKEND_PATH", "")
if _BACKEND_PATH:
    sys.path.insert(0, _BACKEND_PATH)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

TOKEN = os.environ.get("MUSE_MCP_TOKEN", "").strip()
if not TOKEN or len(TOKEN) < 16:
    sys.stderr.write("FATAL: MUSE_MCP_TOKEN missing or too short; refusing to start\n")
    sys.exit(1)

PORT = int(os.environ.get("MUSE_MCP_PORT", "8903"))
DATA_DIR = os.environ.get("MUSE_MCP_DATA_DIR", str(Path.home() / "data" / "muse-mcp-bridge"))
QUEUE_DIR = os.path.join(DATA_DIR, "queue")
DONE_DIR = os.path.join(DATA_DIR, "done")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
MEDIA_MAX_BYTES = 25 * 1024 * 1024  # decoded-size cap per stored file

HA_API = os.environ.get("HA_API_BIN", str(Path.home() / "bin" / "ha-api"))
ACTING_USER = {"user_key": os.environ.get("MUSE_MCP_USER_KEY", "owner"), "role": "admin"}

GUIDE = """# muse operator guide

muse is the owner's personal AI assistant. This MCP server exposes the
assistant's world to an external agent client. Direct tools below are
READ-ONLY and answer immediately. Anything needing judgment, drafting,
decisions, or writes goes through ask_muse — the assistant picks it up
and answers via muse_status.

## Data layout (all under $MUSE_MCP_DATA_DIR)
- Task queue (this bridge): queue/*.json
- Task answers: done/*.json
- Stored media: media/YYYY-MM-DD/

## Household backend
- Todos and chores come from a pluggable backend module
  ($MUSE_MCP_BACKEND_MODULE) exposing a DashboardService class with
  list_todos(user) / chores(user). An example backend ships in this
  repository (example_backend.py) returning sample data.

## Home Assistant
- Reads go through a small helper ($HA_API_BIN) that calls the HA REST
  API with a bearer token read from a mode-600 file; the token is never
  logged or returned. Reads are always safe.
- Service calls (changing devices) are NOT exposed here — use ask_muse.

## Media storage
- EVERY picture or recording the client device captures should be stored
  via muse_store_media(filename, mime_type, data_base64), so nothing
  lives only in the client platform's cloud.
- Use muse_list_media(date) to find stored files.

## Routing rule for clients
- Direct tools execute code and answer immediately.
- On ambiguity, missing data, or any failure: do NOT guess — call
  ask_muse and tell the user the assistant has been asked. Poll
  muse_status for the answer.
"""

_PUBLIC_HOST = os.environ.get("MUSE_MCP_PUBLIC_HOST", "")
_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
if _PUBLIC_HOST:
    _ALLOWED_HOSTS.append(_PUBLIC_HOST)
    _ALLOWED_ORIGINS.append("https://" + _PUBLIC_HOST)

mcp = FastMCP(
    "muse",
    instructions=(
        "muse — the owner's personal AI assistant. "
        "Household operations, home automation state, and judgment calls. "
        "When the owner says 'check with muse', use ask_muse. "
        "Direct tools are read-only and answer immediately; "
        "ask_muse queues anything needing Seb's judgment, drafting, or decisions, "
        "and muse_status polls for the answer. Media the client device captures is stored on the home server via muse_store_media."
    ),
    # Keep DNS-rebinding protection ON, but allow the public tunnel hostname
    # ($MUSE_MCP_PUBLIC_HOST) in addition to localhost. Without this,
    # requests arriving via the public host are rejected with
    # "Invalid Host header".
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_ALLOWED_ORIGINS,
    ),
)

# Stateless HTTP mode: Rabbit OS3's agent backend caches the MCP
# session ID and never re-initializes after a Session-not-found (-32600),
# so any bridge restart permanently broke its connection. Stateless mode
# issues and tracks no sessions, making restarts harmless. All tools here
# are stateless anyway.
mcp.settings.stateless_http = True


def _log(*parts: object) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), *parts, flush=True)


def _fail(tool: str, err: Exception) -> dict:
    _log("ERROR", tool, type(err).__name__, str(err)[:200])
    return {
        "error": f"{tool} failed: {type(err).__name__}",
        "hint": "Do not guess. Use ask_muse to ask muse (Seb) instead.",
    }


_svc_lock = threading.Lock()
_svc = None


def _dashboard():
    global _svc
    with _svc_lock:
        if _svc is None:
            module_name = os.environ.get(
                "MUSE_MCP_BACKEND_MODULE", "household.dashboard_service")
            module = importlib.import_module(module_name)
            _svc = module.DashboardService()
        return _svc


@mcp.tool(
    description=(
        "Ask muse (Seb) anything needing judgment, drafting, decisions, or "
        "actions not covered by the direct tools (e.g. 'check with muse about "
        "…'). Writes the task to Seb's queue and nudges him. Returns a task_id; "
        "poll muse_status(task_id) for his answer. Use this instead of guessing."
    )
)
def ask_muse(message: str) -> dict:
    try:
        task_id = uuid.uuid4().hex
        os.makedirs(QUEUE_DIR, exist_ok=True)
        payload = {
            "task_id": task_id,
            "message": message,
            "source": "os3-mcp",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "queued",
        }
        tmp = os.path.join(QUEUE_DIR, task_id + ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, os.path.join(QUEUE_DIR, task_id + ".json"))
        _log("ask_muse queued", task_id, "msg_len=%d" % len(message))
        return {
            "task_id": task_id,
            "status": "queued",
            "hint": "Poll muse_status with this task_id for muse (Seb)'s answer.",
        }
    except Exception as e:  # noqa: BLE001
        return _fail("ask_muse", e)


@mcp.tool(
    description=(
        "Check muse (Seb)'s answer to a task queued with ask_muse. "
        "Returns {status: pending} while Seb is working, "
        "{status: done, result: …} when he has answered."
    )
)
def muse_status(task_id: str) -> dict:
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id or ""):
            return {"status": "error", "error": "invalid task_id"}
        done_f = os.path.join(DONE_DIR, task_id + ".json")
        if os.path.exists(done_f):
            with open(done_f) as f:
                data = json.load(f)
            return {"status": "done", "result": data.get("result")}
        if os.path.exists(os.path.join(QUEUE_DIR, task_id + ".json")):
            return {"status": "pending"}
        return {"status": "error", "error": "unknown task_id"}
    except Exception as e:  # noqa: BLE001
        return _fail("muse_status", e)


@mcp.tool(
    description=(
        "the assistant's operator guide: data layout, the household backend, "
        "household DB canonical access rules, and the Home Assistant bridge. "
        "Also available as the muse://guide resource."
    )
)
def muse_guide(topic: str = "") -> dict:
    try:
        t = (topic or "").strip().lower()
        if t:
            # Return the section whose heading matches the topic, else the full guide.
            sections = GUIDE.split("\n## ")
            for s in sections[1:]:
                if t in s.split("\n", 1)[0].lower():
                    return {"guide": "## " + s}
        return {"guide": GUIDE}
    except Exception as e:  # noqa: BLE001
        return _fail("muse_guide", e)


@mcp.resource("muse://guide", description="muse (Seb)'s operator guide (same as the muse_guide tool).")
def guide_resource() -> str:
    return GUIDE


def _slim_todo(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "status": item.get("status"),
        "priority": item.get("priority"),
        "due_at": item.get("due_at"),
        "category": item.get("category"),
    }


@mcp.tool(
    description=(
        "READ-ONLY: open household to-dos, via the assistant's household "
        "system. Writes are not exposed — use ask_muse if something needs to "
        "be added, changed, or completed."
    )
)
def household_todos() -> dict:
    try:
        svc = _dashboard()
        items = svc.list_todos(ACTING_USER, include_all=True)
        open_items = [i for i in items if not i.get("completed")]
        _log("household_todos open=%d total=%d" % (len(open_items), len(items)))
        return {
            "open": [_slim_todo(i) for i in open_items[:100]],
            "open_count": len(open_items),
            "completed_count": len(items) - len(open_items),
        }
    except Exception as e:  # noqa: BLE001
        return _fail("household_todos", e)


@mcp.tool(
    description=(
        "READ-ONLY: household chores and their current completion state, via "
        "muse (Seb)'s household system. Writes are not exposed — use ask_muse "
        "if something needs to be completed, skipped, or changed."
    )
)
def household_chores() -> dict:
    try:
        svc = _dashboard()
        items = svc.chores(ACTING_USER)
        out = [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "completed_current_interval": c.get("completed_current_interval"),
                "skipped_current_interval": c.get("skipped_current_interval"),
                "next_due_at": c.get("next_due_at"),
            }
            for c in items[:100]
        ]
        _log("household_chores count=%d" % len(items))
        return {"chores": out, "count": len(items)}
    except Exception as e:  # noqa: BLE001
        return _fail("household_chores", e)


@mcp.tool(
    description=(
        "READ-ONLY: Home Assistant entity state via muse (Seb)'s HA bridge. "
        "With no arguments, lists entity ids. Service calls (changing devices) "
        "are not exposed — use ask_muse."
    )
)
def ha_state(entity_id: str = "") -> dict:
    try:
        eid = (entity_id or "").strip()
        if eid and not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", eid):
            return {"error": "invalid entity_id format"}
        path = "/api/states/" + eid if eid else "/api/states"
        proc = subprocess.run(
            [HA_API, "GET", path], capture_output=True, text=True, timeout=25
        )
        if proc.returncode != 0:
            raise RuntimeError("ha-api exited %d" % proc.returncode)
        data = json.loads(proc.stdout)
        if eid:
            return {
                "entity_id": data.get("entity_id"),
                "state": data.get("state"),
                "attributes": data.get("attributes", {}),
            }
        ids = [d.get("entity_id") for d in data if d.get("entity_id")]
        _log("ha_state list count=%d" % len(ids))
        return {"count": len(ids), "entity_ids": sorted(ids)}
    except Exception as e:  # noqa: BLE001
        return _fail("ha_state", e)


def _safe_media_name(name: str) -> str:
    base = os.path.basename((name or "").strip())
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    if not base:
        base = "upload.bin"
    if "." not in base:
        base += ".bin"
    return base[:120]


@mcp.tool(
    description=(
        "Store a picture, recording, or other media file captured by the R1 on "
        "the home server, where the assistant can retrieve it later. "
        "Args: filename (original name), mime_type, data_base64 (base64-encoded "
        "file bytes, max 25 MB decoded). Files land in "
        "$MUSE_MCP_DATA_DIR/media/YYYY-MM-DD/. Call this for EVERY picture "
        "or recording the R1 captures, so nothing lives only in Rabbit's cloud."
    )
)
def muse_store_media(filename: str, mime_type: str, data_base64: str) -> dict:
    try:
        import base64

        raw = base64.b64decode(data_base64 or "", validate=True)
        if not raw:
            return {"error": "empty file data"}
        if len(raw) > MEDIA_MAX_BYTES:
            return {
                "error": "file too large (%d bytes, max %d)"
                % (len(raw), MEDIA_MAX_BYTES)
            }
        day = time.strftime("%Y-%m-%d", time.gmtime())
        dest_dir = os.path.join(MEDIA_DIR, day)
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        safe = _safe_media_name(filename)
        dest = os.path.join(dest_dir, safe)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(safe)
            dest = os.path.join(
                dest_dir, "%s_%s%s" % (stem, uuid.uuid4().hex[:8], ext)
            )
        with open(dest + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(dest + ".tmp", dest)
        os.chmod(dest, 0o600)
        meta = {
            "original_filename": (filename or "")[:200],
            "mime_type": (mime_type or "").strip()[:100],
            "bytes": len(raw),
            "stored_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(dest + ".meta.json", "w") as f:
            json.dump(meta, f)
        _log("muse_store_media stored=%s bytes=%d" % (dest, len(raw)))
        return {"stored_path": dest, "bytes": len(raw), "meta": meta}
    except Exception as e:  # noqa: BLE001
        return _fail("muse_store_media", e)


@mcp.tool(
    description=(
        "READ-ONLY: list media files the client device has stored via "
        "muse_store_media. Args: date (YYYY-MM-DD, default today UTC), limit "
        "(default 50). Returns stored paths, sizes, and capture times."
    )
)
def muse_list_media(date: str = "", limit: int = 50) -> dict:
    try:
        d = (date or "").strip()
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            return {"error": "invalid date format, use YYYY-MM-DD"}
        day = d or time.strftime("%Y-%m-%d", time.gmtime())
        dest_dir = os.path.join(MEDIA_DIR, day)
        if not os.path.isdir(dest_dir):
            return {"date": day, "files": [], "count": 0}
        names = sorted(os.listdir(dest_dir), reverse=True)
        names = [n for n in names if not n.endswith(".meta.json")]
        names = names[: max(1, min(limit, 200))]
        out = []
        for n in names:
            p = os.path.join(dest_dir, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append(
                {
                    "filename": n,
                    "stored_path": p,
                    "bytes": st.st_size,
                    "captured_at_utc": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)
                    ),
                }
            )
        return {"date": day, "files": out, "count": len(out)}
    except Exception as e:  # noqa: BLE001
        return _fail("muse_list_media", e)


async def healthz(scope, receive, send):  # noqa: ANN001, ANN201
    resp = JSONResponse({"ok": True, "server": "muse"})
    await resp(scope, receive, send)


async def not_found(scope, receive, send):  # noqa: ANN001, ANN201
    resp = JSONResponse({"error": "not found"}, status_code=404)
    await resp(scope, receive, send)


def build_app():  # noqa: ANN201
    """Wrap the FastMCP Streamable HTTP app with a token-gated path prefix.

    The session manager's lifespan must run, so the inner app is called
    directly (lifespan scope delegated) with the token prefix stripped from
    the request path. Mounting would skip the inner lifespan and break
    session handling.
    """
    inner = mcp.streamable_http_app()
    prefix = f"/mcp-{TOKEN}"

    async def app_wrapper(scope, receive, send):  # noqa: ANN001, ANN201
        if scope["type"] == "lifespan":
            await inner(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        if path == "/healthz":
            await healthz(scope, receive, send)
            return
        if path == prefix or path.startswith(prefix + "/"):
            scope["path"] = path[len(prefix):] or "/"
            await inner(scope, receive, send)
            return
        await not_found(scope, receive, send)

    return app_wrapper


app = build_app()

if __name__ == "__main__":
    import uvicorn

    _log("muse MCP bridge starting on 127.0.0.1:%d path=/mcp-<token>/mcp" % PORT)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
