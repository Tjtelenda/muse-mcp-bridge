#!/usr/bin/env python3
"""Smoke-verify a running muse MCP bridge over its HTTP endpoint.

Checks: initialize, tools/list (expects the 8 bridge tools),
ask_muse -> muse_status round trip, and a muse_store_media /
muse_list_media store-and-list cycle. Prints PASS lines; exits non-zero
on the first failure. The token is read from the environment or .env and
is never printed.

Usage:
    MUSE_MCP_TOKEN=... python3 verify_bridge.py [base_url]
    # base_url defaults to http://127.0.0.1:8903
"""
import base64
import json
import os
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8903").rstrip("/")


def get_token() -> str:
    token = os.environ.get("MUSE_MCP_TOKEN", "")
    if not token and os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if line.startswith("MUSE_MCP_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
    if len(token) < 16:
        sys.stderr.write("ERROR: MUSE_MCP_TOKEN missing or too short\n")
        sys.exit(1)
    return token


def rpc(url, method, params, msg_id):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if msg_id is not None:
        msg["id"] = msg_id
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "User-Agent": "muse-bridge-verify/1.0"}
    req = urllib.request.Request(url, data=json.dumps(msg).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode().strip()
    if not raw:
        return {}
    # Streamable HTTP may wrap JSON-RPC in SSE; unwrap if needed.
    if raw.startswith("event:") or "\ndata:" in raw:
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw)


def main():
    url = f"{BASE}/mcp-{get_token()}/mcp"
    init = rpc(url, "initialize",
               {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "verify", "version": "1.0"}}, 1)
    assert "result" in init, f"initialize failed: {init}"
    print("PASS initialize")

    rpc(url, "notifications/initialized", {}, None)
    tools = rpc(url, "tools/list", {}, 2)
    names = sorted(t["name"] for t in tools["result"]["tools"])
    expected = ["ask_muse", "ha_state", "household_chores",
                "household_todos", "muse_guide", "muse_list_media",
                "muse_status", "muse_store_media"]
    assert names == expected, f"tools mismatch: {names}"
    print("PASS tools/list (8 tools)")

    queued = rpc(url, "tools/call",
                 {"name": "ask_muse",
                  "arguments": {"message": "verify_bridge smoke test"}}, 3)
    payload = json.loads(queued["result"]["content"][0]["text"])
    assert payload["status"] == "queued", payload
    status = rpc(url, "tools/call",
                 {"name": "muse_status",
                  "arguments": {"task_id": payload["task_id"]}}, 4)
    payload2 = json.loads(status["result"]["content"][0]["text"])
    assert payload2["status"] in ("pending", "done"), payload2
    print("PASS ask_muse/muse_status round trip")

    blob = base64.b64encode(b"muse-bridge-verify").decode()
    stored = rpc(url, "tools/call",
                 {"name": "muse_store_media",
                  "arguments": {"filename": "verify.txt",
                                "mime_type": "text/plain",
                                "data_base64": blob}}, 5)
    payload3 = json.loads(stored["result"]["content"][0]["text"])
    assert payload3.get("bytes") == len(b"muse-bridge-verify"), payload3
    listed = rpc(url, "tools/call",
                 {"name": "muse_list_media", "arguments": {}}, 6)
    payload4 = json.loads(listed["result"]["content"][0]["text"])
    assert any(f["filename"].startswith("verify") for f in payload4["files"]), payload4
    print("PASS muse_store_media/muse_list_media")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
