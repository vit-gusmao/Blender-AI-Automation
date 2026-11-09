"""Run a bounded tools/call and include the server stderr tail."""
from __future__ import annotations

import argparse
import json

from mcp_diag_client import MCPDiagnosticClient


parser = argparse.ArgumentParser()
parser.add_argument("--timeout", type=float, default=10.0)
args = parser.parse_args()
client = MCPDiagnosticClient(timeout=args.timeout)
try:
    client.start()
    initialized = client.initialize()
    called = client.request("tools/call", {
        "name": "blender_get_capabilities", "arguments": {},
    })
    print("initialize:", json.dumps(initialized, ensure_ascii=False)[:600])
    print("response:", json.dumps(called, ensure_ascii=False)[:600])
finally:
    client.close()
print("---- server stderr ----")
print(client.stderr_tail())

