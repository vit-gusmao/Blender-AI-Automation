"""Diagnóstico direto: provider payload, bridge auth, mcp stdio."""
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(r"C:\Blender\RPA")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp_server" / "src"))

print("== 1) provider payload ==")
from agent_gateway.providers import OpenAICompatibleProvider, ProviderConfig

MESSAGES = [
    {"role": "user", "content": "crie um cubo"},
    {"role": "assistant", "content": "ok",
     "tool_calls": [{"id": "c1", "name": "object_create",
                     "arguments": {"type": "CUBE"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "object_create",
     "content": '{"success": true}'},
]
cfg = ProviderConfig("test", "openai_compatible", "http://127.0.0.1/v1",
                     "model-x")
payload = OpenAICompatibleProvider(cfg).build_payload("SYS", MESSAGES, [])
for i, m in enumerate(payload["messages"]):
    print(i, json.dumps(m, default=str)[:160])

print()
print("== 2) bridge POST com token errado ==")
from universal_blender_mcp.bridge_server import BridgeServer
from universal_blender_mcp.job_manager import JobManager

import tempfile

jobs = JobManager(Path(tempfile.mkdtemp()) / "jobs.db")
srv = BridgeServer("127.0.0.1", 0, "real-token", jobs)
srv.start(daemon=True)
try:
    req = urllib.request.Request(
        srv.url + "/commands",
        data=json.dumps({"operation": "x"}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Bridge-Token", "wrong")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print("status inesperado:", resp.status)
    except Exception as exc:  # noqa: BLE001
        print("esperado HTTPError:", type(exc).__name__, exc)
finally:
    srv.stop()

print()
print("== 3) mcp stdio com stderr ==")
import os
import subprocess

env = {**os.environ,
       "PYTHONPATH": str(REPO / "mcp_server" / "src"),
       "BLENDER_MCP_ROOT": str(Path(tempfile.mkdtemp()))}
proc = subprocess.run(
    [r"C:\Blender\RPA\.venv\Scripts\python.exe", "-m",
     "universal_blender_mcp", "serve"],
    input=(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "t",
                                                 "version": "0"}}}) + "\n"),
    capture_output=True, text=True, env=env, timeout=60)
print("returncode:", proc.returncode)
print("stdout[:600]:", proc.stdout[:600])
print("stderr[:1500]:", proc.stderr[:1500])
