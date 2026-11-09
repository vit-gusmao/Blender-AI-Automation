"""Run initialize, tools/list and tools/call through the shared client."""
from __future__ import annotations

import argparse
import json

from mcp_diag_client import MCPDiagnosticClient


parser = argparse.ArgumentParser()
parser.add_argument("--timeout", type=float, default=10.0)
args = parser.parse_args()
with MCPDiagnosticClient(timeout=args.timeout) as client:
    initialized = client.initialize()
    listed = client.request("tools/list")
    called = client.request("tools/call", {
        "name": "blender_get_capabilities", "arguments": {},
    })
    print("initialize keys:", sorted(initialized))
    print("tools/list tools:", len(listed.get("result", {}).get("tools", [])))
    print("tools/call response:")
    print(json.dumps(called, ensure_ascii=False, indent=2)[:1500])

