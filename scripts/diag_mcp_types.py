"""Validate the current tools/call model surface in mcp.types."""
from __future__ import annotations

import argparse
import inspect

import mcp.types as types


parser = argparse.ArgumentParser()
parser.add_argument("--timeout", type=float, default=10.0)
parser.parse_args()
for model in (types.CallToolRequestParams, types.CallToolRequest,
              types.CallToolResult):
    print(model.__name__, inspect.signature(model))

