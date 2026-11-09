"""Show the current SDK request models used by tools/call."""
from __future__ import annotations

import argparse
import inspect

import mcp.types as types


parser = argparse.ArgumentParser()
parser.add_argument("--timeout", type=float, default=10.0)
parser.parse_args()
print(inspect.getsource(types.CallToolRequestParams))
print(inspect.getsource(types.CallToolRequest))

